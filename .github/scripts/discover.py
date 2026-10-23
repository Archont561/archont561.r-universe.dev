#!/usr/bin/env python3
"""Regenerate packages.json by discovering R packages under a GitHub owner.

A repository (or a subdirectory of it) counts as an R package when it
contains a DESCRIPTION file with a valid Package: field. Nothing else --
GitHub's detected language, topics, repo names -- is trusted: they all
misclassify polyglot repos or non-package R code.

For each discovered DESCRIPTION an entry is derived, never guessed:
  * package -> Package: field of DESCRIPTION
  * url     -> the repo's html_url (so renames fix themselves)
  * subdir  -> directory containing DESCRIPTION (omitted at repo root)

Fields discovery cannot know about (e.g. branch) are preserved from the
current packages.json, package by package. Explicit per-package overrides
in the config file win over the preserved values; setting an override to
null removes the field. Owner entries that no longer exist are dropped --
the workflow opens a pull request, so removals get reviewed. Entries
pointing outside the scanned owner (added deliberately, e.g. with
add_package.py) are preserved untouched: discovery only manages the
namespace it scans.

Usage:
    python3 .github/scripts/discover.py            # rewrite packages.json
    python3 .github/scripts/discover.py --dry-run  # print it instead
"""

from __future__ import annotations

import argparse
import os
import sys
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from registry_common import (  # noqa: E402
    DEFAULT_REGISTRY,
    DiscoveryError,
    GitHub,
    find_packages_in_repo,
    is_github_repo_of,
    latest_release,
    load_config,
    load_registry,
    log,
    make_github_client,
    ordered_entry,
    parse_github_repo,
    render_registry,
    resolve_owner,
    run_main,
    warn,
)


def discover_packages(
    gh: GitHub, owner: str, config: dict, warnings: list[str]
) -> list[dict]:
    """Scan every eligible repo of `owner`; return raw discovered entries."""
    quote = urllib.parse.quote
    exclude_repos = {str(r).lower() for r in config.get("exclude_repos", [])}
    exclude_packages = {str(p).lower() for p in config.get("exclude_packages", [])}
    max_depth = int(config.get("max_description_depth", 2))

    repos = gh.paginate(
        f"/users/{quote(owner, safe='')}/repos?per_page=100&type=owner&sort=full_name"
    )
    log(f"scanning {len(repos)} repositories owned by {owner}")

    discovered: list[dict] = []
    seen: dict[str, str] = {}  # lower(package) -> "repo[/subdir] that claimed it"
    duplicates: list[str] = []

    for repo in repos:
        name = repo["name"]
        if repo.get("fork") or repo.get("archived") or repo.get("private") \
                or repo.get("disabled"):
            continue
        if name.lower().endswith(".r-universe.dev"):  # never register the registry
            continue
        if name.lower() in exclude_repos or f"{owner}/{name}".lower() in exclude_repos:
            log(f"  {name}: excluded by config")
            continue
        if int(repo.get("size") or 0) == 0:
            continue

        try:
            found = find_packages_in_repo(gh, repo, warnings, max_depth=max_depth)
        except DiscoveryError as error:
            warn(warnings, f"{name}: could not read git tree, skipped ({error})")
            continue

        for package_entry in found:
            package = package_entry["package"]
            subdir = package_entry["subdir"]
            location = f"{name}/{subdir}" if subdir else name
            if package.lower() in exclude_packages:
                log(f"  {location}: package {package} excluded by config")
                continue
            if package.lower() in seen:
                duplicates.append(
                    f"package {package!r} found in both "
                    f"{seen[package.lower()]} and {location}"
                )
                continue
            seen[package.lower()] = location
            discovered.append(package_entry)
            log(f"  {location}: R package {package}")

    if duplicates:
        raise DiscoveryError("duplicate package names:\n  " + "\n  ".join(duplicates))
    return discovered


def _apply_overrides(
    package: str, extras: dict, overrides_by_package: dict, warnings: list[str]
) -> dict:
    """Apply a package's config overrides; a null value deletes the field."""
    extras = dict(extras)
    for key, value in (overrides_by_package.get(package.lower()) or {}).items():
        if key in ("package", "url", "subdir"):
            warn(warnings, f"{package}: override of derived field {key!r} ignored")
        elif value is None:
            extras.pop(key, None)
        else:
            extras[key] = value
    return extras


def merge_entries(
    discovered: list[dict], existing: list[dict], overrides: dict,
    warnings: list[str], owner: str | None = None,
    exclude_packages: set[str] | None = None,
) -> list[dict]:
    """Build the new registry from discovered packages + preserved fields.

    Fields discovery can't derive (branch, ...) are taken from the current
    packages.json, then config overrides are applied. Undiscovered entries
    under the scanned owner are pruned with a warning; undiscovered entries
    pointing elsewhere are manual/external additions and are kept as-is.
    """
    existing_by_package = {
        str(entry.get("package", "")).lower(): entry for entry in existing
    }
    overrides_by_package = {str(k).lower(): v for k, v in overrides.items()}
    exclude_packages = {p.lower() for p in (exclude_packages or set())}

    entries: list[dict] = []
    for found in discovered:
        package, url, subdir = found["package"], found["url"], found["subdir"]
        extras: dict = {}
        old = existing_by_package.get(package.lower())
        if old:
            extras.update({k: v for k, v in old.items()
                           if k not in ("package", "url", "subdir")})
            if old.get("url") and old["url"] != url:
                log(f"  {package}: url changed {old['url']} -> {url}")
            if (old.get("subdir") or "") != subdir:
                log(f"  {package}: subdir changed "
                    f"{old.get('subdir') or '(root)'} -> {subdir or '(root)'}")
        extras = _apply_overrides(package, extras, overrides_by_package, warnings)
        entries.append(ordered_entry(
            package, url, extras.pop("branch", None), subdir, extras
        ))

    discovered_names = {e["package"].lower() for e in entries}
    for name, old in existing_by_package.items():
        if not name or name in discovered_names:
            continue
        package, url = old.get("package"), old.get("url") or ""
        if name in exclude_packages:
            warn(warnings, f"removing {package}: excluded by config")
        elif owner and url and not is_github_repo_of(url, owner):
            extras = {k: v for k, v in old.items()
                      if k not in ("package", "url", "subdir")}
            extras = _apply_overrides(
                str(package), extras, overrides_by_package, warnings
            )
            entries.append(ordered_entry(
                str(package), url, extras.pop("branch", None),
                old.get("subdir") or "", extras,
            ))
            log(f"  {package}: outside scanned owner, entry preserved")
        else:
            warn(warnings, f"removing {package}: no longer discovered (was {url})")

    entries.sort(key=lambda e: str(e["package"]).lower())
    return entries


def check_release_branches(
    gh: GitHub, entries: list[dict], warnings: list[str]
) -> None:
    """branch: *release resolves to the newest GitHub release; flag if none."""
    for entry in entries:
        if entry.get("branch") != "*release":
            continue
        package = entry["package"]
        parsed = parse_github_repo(str(entry.get("url", "")))
        if not parsed:
            warn(warnings, f"{package} tracks branch *release but "
                           f"{entry.get('url')} is not a GitHub repository -- "
                           "r-universe cannot resolve a release there")
            continue
        repo_owner, repo = parsed
        try:
            release = latest_release(gh, repo_owner, repo)
        except DiscoveryError as error:
            warn(warnings, f"{package}: could not check releases ({error})")
            continue
        if release is None:
            warn(warnings, f"{package} tracks branch *release but "
                           f"{repo_owner}/{repo} has no published GitHub release -- "
                           "r-universe has nothing to build; cut a release or "
                           "drop the branch override to track the default branch")


def write_step_outputs(entries: list[dict], changed: bool, warnings: list[str]) -> None:
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as handle:
            handle.write(f"changed={'true' if changed else 'false'}\n")
            handle.write(f"packages={len(entries)}\n")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        lines = ["## R-universe registry discovery", "",
                 f"**{len(entries)} package(s)** discovered"
                 f" -- packages.json {'changed' if changed else 'unchanged'}.", "",
                 "| Package | URL | Branch | Subdir |", "|---|---|---|---|"]
        lines += [
            f"| {e['package']} | {e['url']} | {e.get('branch', '(default)')} "
            f"| {e.get('subdir', '(root)')} |"
            for e in entries
        ]
        if warnings:
            lines += ["", "### Warnings"] + [f"- {text}" for text in warnings]
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Regenerate an r-universe packages.json from GitHub discovery."
    )
    parser.add_argument("--owner", help="GitHub owner to scan "
                        "(default: config owner, else $GITHUB_REPOSITORY_OWNER)")
    parser.add_argument("--config", type=Path, default=None,
                        help="path to registry-discovery.json")
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY,
                        help="packages.json to read (for preserved fields) and write")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the resulting packages.json, write nothing")
    args = parser.parse_args(argv)

    config = load_config(args.config)
    owner = resolve_owner(args.owner, config)
    existing, existing_text = load_registry(args.registry)
    gh = make_github_client()

    warnings: list[str] = []
    discovered = discover_packages(gh, owner, config, warnings)
    entries = merge_entries(
        discovered, existing, config.get("overrides", {}), warnings,
        owner=owner,
        exclude_packages={str(p).lower()
                          for p in config.get("exclude_packages", [])},
    )
    check_release_branches(gh, entries, warnings)

    rendered = render_registry(entries)
    changed = rendered != existing_text
    if args.dry_run:
        print(rendered, end="")
        log(f"dry run: {len(entries)} package(s); packages.json "
            f"{'would change' if changed else 'already up to date'}")
    elif changed:
        args.registry.write_text(rendered, encoding="utf-8")
        log(f"wrote {args.registry} with {len(entries)} package(s)")
    else:
        log(f"{args.registry} already up to date ({len(entries)} package(s))")

    write_step_outputs(entries, changed, warnings)
    return 0


if __name__ == "__main__":
    run_main(main)
