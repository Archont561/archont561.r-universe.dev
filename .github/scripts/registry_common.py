"""Shared helpers for the r-universe registry scripts.

Used by discover.py (scan an owner and regenerate packages.json),
add_package.py (add a single package to packages.json) and
update_readme.py (rebuild the released-packages table in README.md).

Stdlib only. Token resolution: GH_TOKEN / GITHUB_TOKEN, then
`gh auth token`, then unauthenticated (public repos only, low rate limit).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API_ROOT = "https://api.github.com"
# R package names: start with a letter, letters/digits/dots, no trailing dot.
PACKAGE_NAME_RE = re.compile(r"^[A-Za-z](?:[A-Za-z0-9.]*[A-Za-z0-9])?$")
LINK_NEXT_RE = re.compile(r'<([^>]+)>;\s*rel="next"')
# A DESCRIPTION under any of these directories is test fixture data,
# examples, or check output -- never a package to register.
SKIP_SEGMENTS = {
    "example", "examples", "fixture", "fixtures", "inst",
    "test", "tests", "testdata", "vignettes",
}
KNOWN_KEYS = ("package", "url", "branch", "subdir")
DEFAULT_CONFIG = Path(".github/registry-discovery.json")
DEFAULT_REGISTRY = Path("packages.json")


class DiscoveryError(RuntimeError):
    """Fatal registry failure (bad config, API down, duplicate packages)."""


def log(message: str) -> None:
    print(message, file=sys.stderr)


def warn(warnings: list[str], message: str) -> None:
    warnings.append(message)
    log(f"warning: {message}")
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::warning::{message}")


def get_token() -> str | None:
    for name in ("GH_TOKEN", "GITHUB_TOKEN"):
        token = os.environ.get(name)
        if token:
            return token
    try:
        out = subprocess.run(
            ["gh", "auth", "token"], capture_output=True, text=True, timeout=10
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return None


class GitHub:
    def __init__(self, token: str | None) -> None:
        self.token = token

    def _request(self, path_or_url: str, accept: str) -> tuple[bytes, object]:
        url = path_or_url if path_or_url.startswith("http") else API_ROOT + path_or_url
        request = urllib.request.Request(url, headers={
            "Accept": accept,
            "User-Agent": "r-universe-registry-scripts",
            "X-GitHub-Api-Version": "2022-11-28",
            **({"Authorization": f"Bearer {self.token}"} if self.token else {}),
        })
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.read(), response.headers
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", "replace")[:200].strip()
            raise DiscoveryError(f"GET {url} -> HTTP {error.code}: {detail}") from error

    def get_json(self, path_or_url: str) -> tuple[object, object]:
        body, headers = self._request(path_or_url, "application/vnd.github+json")
        return json.loads(body), headers

    def get_text(self, path: str) -> str:
        body, _ = self._request(path, "application/vnd.github.raw+json")
        return body.decode("utf-8-sig", "replace")

    def paginate(self, path: str) -> list[dict]:
        items: list[dict] = []
        url: str | None = path
        while url:
            page, headers = self.get_json(url)
            items.extend(page)
            match = LINK_NEXT_RE.search(headers.get("Link", ""))
            url = match.group(1) if match else None
        return items


def make_github_client() -> GitHub:
    gh = GitHub(get_token())
    if gh.token is None:
        log("note: no GitHub token found; unauthenticated API access "
            "(public repos only, 60 requests/hour)")
    return gh


# --------------------------------------------------------------------------
# DESCRIPTION files
# --------------------------------------------------------------------------

def parse_dcf(text: str) -> dict[str, str]:
    """Parse the first record of a Debian Control File (DESCRIPTION)."""
    fields: dict[str, str] = {}
    current: str | None = None
    for line in text.splitlines():
        if not line.strip():
            if fields:  # blank line terminates the first record
                break
            continue
        if line[0].isspace() and current is not None:
            fields[current] = f"{fields[current]} {line.strip()}".strip()
        elif ":" in line:
            current, value = line.split(":", 1)
            current = current.strip()
            fields[current] = value.strip()
    return fields


def description_subdir(path: str, max_depth: int) -> str | None:
    """Return the package subdir for a candidate DESCRIPTION path, else None.

    Root DESCRIPTION -> "" ; r/DESCRIPTION -> "r". Paths that are too deep,
    hidden, or inside test/fixture/check directories are rejected.
    """
    parts = path.split("/")
    if parts[-1] != "DESCRIPTION":
        return None
    dirs = parts[:-1]
    if len(dirs) > max_depth:
        return None
    lowered = [d.lower() for d in dirs]
    if any(d.startswith(".") or d.endswith(".rcheck") for d in lowered):
        return None
    if any(d in SKIP_SEGMENTS for d in lowered):
        return None
    return "/".join(dirs)


def fetch_description(
    gh: GitHub, repo_owner: str, repo: str, subdir: str, ref: str
) -> dict[str, str]:
    """Fetch and parse DESCRIPTION at <subdir>/DESCRIPTION at the given ref."""
    quote = urllib.parse.quote
    path = f"{subdir}/DESCRIPTION" if subdir else "DESCRIPTION"
    text = gh.get_text(
        f"/repos/{quote(repo_owner, safe='')}/{quote(repo, safe='')}"
        f"/contents/{quote(path)}?ref={quote(ref, safe='')}"
    )
    return parse_dcf(text)


def find_packages_in_repo(
    gh: GitHub, repo: dict, warnings: list[str],
    ref: str | None = None, max_depth: int = 2,
) -> list[dict]:
    """Find every R package in one repository by its DESCRIPTION file(s).

    `repo` is a GitHub API repository object. Returns dicts with package,
    url, subdir, repo, repo_owner, version, title and the ref scanned.
    Raises DiscoveryError if the git tree cannot be read at all.
    """
    quote = urllib.parse.quote
    name = repo["name"]
    repo_owner = (repo.get("owner") or {}).get("login") \
        or str(repo.get("full_name", "/")).split("/")[0]
    scan_ref = ref or repo.get("default_branch") or "HEAD"

    tree, _ = gh.get_json(
        f"/repos/{quote(repo_owner, safe='')}/{quote(name, safe='')}"
        f"/git/trees/{quote(scan_ref, safe='')}?recursive=1"
    )
    if tree.get("truncated"):
        warn(warnings, f"{name}: git tree truncated by the API; "
                       "a DESCRIPTION may have been missed")

    candidates = sorted(
        subdir for item in tree.get("tree", [])
        if item.get("type") == "blob"
        and (subdir := description_subdir(item.get("path", ""), max_depth)) is not None
    )
    found: list[dict] = []
    for subdir in candidates:
        location = f"{name}/{subdir}" if subdir else name
        try:
            fields = fetch_description(gh, repo_owner, name, subdir, scan_ref)
        except DiscoveryError as error:
            warn(warnings, f"{location}: could not read DESCRIPTION ({error})")
            continue
        package = fields.get("Package", "").strip()
        if not PACKAGE_NAME_RE.match(package):
            warn(warnings, f"{location}: invalid/missing Package: field, skipped")
            continue
        if "Version" not in fields:
            warn(warnings, f"{location}: DESCRIPTION for {package} has no Version:")
        found.append({
            "package": package,
            "url": repo["html_url"],
            "subdir": subdir,
            "repo": name,
            "repo_owner": repo_owner,
            "version": fields.get("Version", "").strip(),
            "title": fields.get("Title", "").strip(),
            "ref": scan_ref,
        })
    return found


# --------------------------------------------------------------------------
# GitHub repositories and releases
# --------------------------------------------------------------------------

def get_repo(gh: GitHub, repo_owner: str, repo: str) -> dict:
    quote = urllib.parse.quote
    data, _ = gh.get_json(
        f"/repos/{quote(repo_owner, safe='')}/{quote(repo, safe='')}"
    )
    return data


def latest_release(gh: GitHub, repo_owner: str, repo: str) -> dict | None:
    """Newest published release, preferring stable over prerelease.

    The API lists releases newest first. Drafts never count; a prerelease
    counts only when no stable release exists.
    """
    quote = urllib.parse.quote
    releases = gh.paginate(
        f"/repos/{quote(repo_owner, safe='')}/{quote(repo, safe='')}"
        "/releases?per_page=100"
    )
    published = [release for release in releases if not release.get("draft")]
    for release in published:
        if not release.get("prerelease"):
            return release
    return published[0] if published else None


def _strip_git_suffix(repo: str) -> str:
    return repo[:-4] if repo.lower().endswith(".git") else repo


def parse_github_repo(value: str) -> tuple[str, str] | None:
    """Parse owner/repo, a github.com URL, or an SSH remote into (owner, repo)."""
    value = value.strip().rstrip("/")
    scp = re.match(r"^(?:[^@/]+@)?github\.com:(.+)$", value)
    if scp:
        value = scp.group(1)
    elif "://" in value:
        parsed = urllib.parse.urlparse(value)
        if (parsed.hostname or "").lower() != "github.com":
            return None
        value = parsed.path.lstrip("/")
    parts = value.split("/")
    if len(parts) < 2 or not parts[0] or not parts[1]:
        return None
    owner, repo = parts[0], _strip_git_suffix(parts[1])
    if not repo:
        return None
    return owner, repo


def is_github_repo_of(url: str, owner: str) -> bool:
    parsed = parse_github_repo(url) if "github.com" in url.lower() else None
    return bool(parsed and parsed[0].lower() == owner.lower())


# --------------------------------------------------------------------------
# Config and registry IO
# --------------------------------------------------------------------------

def load_json(path: Path, default: object, label: str) -> object:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise DiscoveryError(f"{label} at {path} is not valid JSON: {error}") from error


def default_config_path() -> Path:
    candidates = [
        DEFAULT_CONFIG,
        Path(__file__).resolve().parent.parent / "registry-discovery.json",
    ]
    return next((path for path in candidates if path.exists()), candidates[0])


def load_config(path: Path | None) -> dict:
    config_path = path or default_config_path()
    config = load_json(config_path, {}, "discovery config")
    if not isinstance(config, dict):
        raise DiscoveryError(f"discovery config at {config_path} must be a JSON object")
    return config


def resolve_owner(cli_owner: str | None, config: dict) -> str:
    return (
        cli_owner or config.get("owner")
        or os.environ.get("GITHUB_REPOSITORY_OWNER") or "Archont561"
    )


def load_registry(path: Path) -> tuple[list[dict], str]:
    entries = load_json(path, [], "registry")
    if not isinstance(entries, list):
        raise DiscoveryError(f"registry at {path} must be a JSON array")
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    return entries, text


def render_registry(entries: list[dict]) -> str:
    return json.dumps(entries, indent=2, ensure_ascii=False) + "\n"


def ordered_entry(
    package: str, url: str, branch: object = None, subdir: str = "",
    extras: dict | None = None,
) -> dict:
    """Registry entry with the canonical key order: package, url, branch, subdir."""
    entry: dict = {"package": package, "url": url}
    if branch:
        entry["branch"] = branch
    if subdir:
        entry["subdir"] = subdir
    for key in sorted(extras or {}):
        entry[key] = extras[key]
    return entry


def run_main(main) -> None:
    """Shared CLI entry point: turn DiscoveryError into exit code 1."""
    try:
        sys.exit(main())
    except DiscoveryError as error:
        print(f"error: {error}", file=sys.stderr)
        if os.environ.get("GITHUB_ACTIONS") == "true":
            print(f"::error::{error}")
        sys.exit(1)
