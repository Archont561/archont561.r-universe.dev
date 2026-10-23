#!/usr/bin/env python3
"""Add a single package to packages.json.

For a GitHub repository the package name and subdirectory are derived
from its DESCRIPTION file -- nothing is guessed from the repo name:

    python3 .github/scripts/add_package.py Archont561/playtest
    python3 .github/scripts/add_package.py https://github.com/ropensci/curl
    python3 .github/scripts/add_package.py owner/monorepo --package foo
    python3 .github/scripts/add_package.py owner/pkg --branch "*release"
    python3 .github/scripts/add_package.py owner/pkg --dry-run

A non-GitHub Git URL cannot be inspected, so its name must be given:

    python3 .github/scripts/add_package.py https://gitlab.com/o/pkg --package pkg

Adding only edits packages.json. Refresh the README table afterwards with
update_readme.py (the daily workflow runs both). Packages under the
discovery owner are then maintained by discover.py; packages from
elsewhere are preserved by it as manual entries.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from registry_common import (  # noqa: E402
    DEFAULT_REGISTRY,
    PACKAGE_NAME_RE,
    DiscoveryError,
    fetch_description,
    find_packages_in_repo,
    get_repo,
    latest_release,
    load_config,
    load_registry,
    log,
    make_github_client,
    ordered_entry,
    parse_github_repo,
    render_registry,
    run_main,
    warn,
)

SHORTHAND_RE = re.compile(r"^[A-Za-z0-9-]+/[A-Za-z0-9._-]+(?:\.git)?$")


def _looks_like_github(value: str) -> bool:
    return "github.com" in value.lower() or bool(SHORTHAND_RE.match(value.strip()))


def _normalize_url(url: str) -> str:
    normalized = url.strip().rstrip("/").lower()
    return normalized[:-4] if normalized.endswith(".git") else normalized


def apply_entry(
    existing: list[dict], new_entry: dict, update: bool = False
) -> list[dict]:
    """Insert (or with update=True, replace) an entry; keep the list sorted."""
    result = [dict(entry) for entry in existing]
    package = str(new_entry["package"]).lower()
    location = (_normalize_url(str(new_entry["url"])),
                str(new_entry.get("subdir") or ""))
    replaced = False
    for index, entry in enumerate(result):
        same_package = str(entry.get("package", "")).lower() == package
        same_location = (
            _normalize_url(str(entry.get("url", ""))),
            str(entry.get("subdir") or ""),
        ) == location
        if same_location and not same_package:
            raise DiscoveryError(
                f"{new_entry['url']} (subdir {location[1] or '(root)'}) is already "
                f"registered as package {entry.get('package')!r}"
            )
        if same_package:
            if not update:
                raise DiscoveryError(
                    f"package {new_entry['package']!r} is already in the registry; "
                    "pass --update to replace its entry"
                )
            result[index] = dict(new_entry)
            replaced = True
    if not replaced:
        result.append(dict(new_entry))
    result.sort(key=lambda e: str(e["package"]).lower())
    return result


def _resolve_github_entry(
    gh, repo_owner: str, repo: str, args, config: dict, warnings: list[str]
) -> dict:
    repo_data = get_repo(gh, repo_owner, repo)
    if repo_data.get("private"):
        raise DiscoveryError(
            f"{repo_owner}/{repo} is private; r-universe can only clone public Git URLs"
        )
    url = repo_data["html_url"]
    default_ref = repo_data.get("default_branch") or "HEAD"

    # Scan the ref that r-universe will actually build, when we can know it.
    scan_ref = default_ref
    if args.branch and args.branch != "*release":
        scan_ref = args.branch
    elif args.branch == "*release":
        release = latest_release(gh, repo_owner, repo)
        if release:
            scan_ref = release["tag_name"]
        else:
            warn(warnings, f"{repo_owner}/{repo} has no published GitHub release; "
                           "branch *release will not resolve until one exists -- "
                           "registering from the default branch")

    if args.subdir is not None:  # explicit subdir: check it directly (any depth)
        fields = fetch_description(gh, repo_owner, repo, args.subdir, scan_ref)
        package = fields.get("Package", "").strip()
        if not PACKAGE_NAME_RE.match(package):
            raise DiscoveryError(
                f"no valid Package: field in DESCRIPTION at {args.subdir or '(root)'}"
            )
        candidates = [{"package": package, "subdir": args.subdir}]
    else:
        max_depth = int(config.get("max_description_depth", 2))
        candidates = find_packages_in_repo(
            gh, repo_data, warnings, ref=scan_ref, max_depth=max_depth
        )
    if not candidates:
        raise DiscoveryError(
            f"no R package (DESCRIPTION file) found in {repo_owner}/{repo}"
            + (f" at ref {scan_ref}" if scan_ref != default_ref else "")
        )
    if args.package:
        found_names = ", ".join(c["package"] for c in candidates)
        candidates = [
            c for c in candidates if c["package"].lower() == args.package.lower()
        ]
        if not candidates:
            raise DiscoveryError(
                f"package {args.package!r} not found in {repo_owner}/{repo}; "
                f"found: {found_names}"
            )
    if len(candidates) > 1:
        listing = ", ".join(
            f"{c['package']} (subdir: {c['subdir'] or '(root)'})" for c in candidates
        )
        raise DiscoveryError(
            f"{repo_owner}/{repo} contains several R packages: {listing} -- "
            "select one with --package or --subdir"
        )
    selected = candidates[0]
    return ordered_entry(
        selected["package"], url, args.branch or None, selected["subdir"]
    )


def _resolve_external_entry(args) -> dict:
    if not args.package:
        raise DiscoveryError(
            "a non-GitHub URL cannot be inspected for its DESCRIPTION; "
            "pass --package with the Package: field value"
        )
    if not PACKAGE_NAME_RE.match(args.package):
        raise DiscoveryError(f"invalid R package name: {args.package!r}")
    return ordered_entry(
        args.package, args.repo.strip().rstrip("/"),
        args.branch or None, args.subdir or "",
    )


def _preserve_on_update(
    entry: dict, old: dict, branch_arg: str | None
) -> dict:
    """Keep a replaced entry's extra fields; --branch "" clears the branch."""
    extras = {k: v for k, v in old.items()
              if k not in ("package", "url", "branch", "subdir")}
    branch = old.get("branch") if branch_arg is None else (branch_arg or None)
    return ordered_entry(
        entry["package"], entry["url"], branch, entry.get("subdir") or "", extras
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Add one R package to an r-universe packages.json registry.",
        epilog="Then run update_readme.py to refresh the README table.",
    )
    parser.add_argument("repo", help="GitHub owner/repo, github.com URL, "
                                     "SSH remote, or any public Git URL")
    parser.add_argument("--package", help="package name (required for non-GitHub "
                        "URLs; selects one package in a monorepo)")
    parser.add_argument("--subdir", default=None,
                        help="subdirectory containing DESCRIPTION "
                             "(default: discovered for GitHub repos, repo root otherwise)")
    parser.add_argument("--branch", default=None,
                        help='Git branch or tag to track, e.g. "*release" '
                             "(default: the repo's default branch; "
                             'on --update, "" clears a preserved branch)')
    parser.add_argument("--update", action="store_true",
                        help="replace the entry if the package is already registered")
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY,
                        help="packages.json to update")
    parser.add_argument("--config", type=Path, default=None,
                        help="path to registry-discovery.json")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the resulting packages.json, write nothing")
    args = parser.parse_args(argv)

    config = load_config(args.config)
    existing, existing_text = load_registry(args.registry)
    warnings: list[str] = []

    if _looks_like_github(args.repo):
        parsed = parse_github_repo(args.repo)
        if not parsed:
            raise DiscoveryError(f"could not parse a GitHub owner/repo from {args.repo!r}")
        entry = _resolve_github_entry(
            make_github_client(), parsed[0], parsed[1], args, config, warnings
        )
    else:
        entry = _resolve_external_entry(args)

    if entry["package"].lower() in {
        str(p).lower() for p in config.get("exclude_packages", [])
    }:
        raise DiscoveryError(
            f"package {entry['package']!r} is listed in exclude_packages in the "
            "discovery config; remove it there first, or discovery will drop it"
        )

    if args.update:
        for old in existing:
            if str(old.get("package", "")).lower() == entry["package"].lower():
                entry = _preserve_on_update(entry, old, args.branch)
                break

    entries = apply_entry(existing, entry, update=args.update)
    rendered = render_registry(entries)

    if args.dry_run:
        print(rendered, end="")
        log(f"dry run: {'would update' if args.update else 'would add'} "
            f"{entry['package']}; packages.json "
            f"{'would change' if rendered != existing_text else 'already up to date'}")
    elif rendered == existing_text:
        log(f"{args.registry} already up to date ({entry['package']} unchanged)")
    else:
        args.registry.write_text(rendered, encoding="utf-8")
        log(f"{'updated' if args.update else 'added'} {entry['package']} "
            f"in {args.registry} ({len(entries)} package(s)); "
            "run update_readme.py to refresh the README table")
    return 0


if __name__ == "__main__":
    run_main(main)
