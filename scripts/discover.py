"""Decide which SDK versions still need to be built."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib import index_store, plan, plugins, semver, util  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plugin", default="", help="only build this plugin")
    parser.add_argument("--version", default="", help="only build this exact version")
    parser.add_argument("--batch", type=int, default=None, help="versions per plugin (newest first)")
    parser.add_argument("--first-only", action="store_true", help="only the newest version")
    parser.add_argument("--rebuild-all", action="store_true", help="ignore what is already published")
    parser.add_argument("--report-skipped", action="store_true", help="log upstream tags missing from the recipe repo")
    parser.add_argument("--proxy", default=None, help="proxy URL for this run (never persisted)")
    parser.add_argument("--out", default="", help="also write the matrix to this file")
    return parser.parse_args()


def _line_of(version: str) -> str:
    """The release line of a version: ``26.51.6`` -> ``26.51``, ``1.0.0-rc.1`` -> ``1.0``."""
    head = str(version).partition("-")[0]
    parts = head.split(".")
    return ".".join(parts[:-1]) if len(parts) > 1 else str(version)


def cover_lines_first(versions: list[str], built=()) -> list[str]:
    """Order versions so that every release line gets a build before any line gets two.

    Patch levels of one line differ in which upstream commits they carry, while
    lines differ in what the SDK is built from -- so covering the lines first is
    what turns a backlog into broad coverage quickly, and the remaining patches
    then only fill in what is still missing. Inside a line the representative is
    the middle patch level, biased towards the newest: neither the oldest (usually
    the least useful) nor the newest, which upstream may still be moving.

    ``built`` is what the ledger already holds for this package, because "before
    any line gets two" is a statement about the *ledger*, not about the order of
    the pending list. A line that already has a build -- 26.51 with 26.51.4 and
    26.51.6, say -- must not come before one that has none, or every round would
    pick 26.51.x and 26.40.x again and never reach 26.32, 26.20, ..., 1.9 at all.

    ``versions`` arrives newest first and the order of the rest is preserved, so
    picking the next few from the front of the result is stable across runs.
    """
    lines: dict[str, list[str]] = {}
    for version in versions:
        lines.setdefault(_line_of(version), []).append(version)
    started_lines = {_line_of(version) for version in built}
    fresh = [members for line, members in lines.items() if line not in started_lines]
    started = [members for line, members in lines.items() if line in started_lines]
    picks = [members[(len(members) - 1) // 2] for members in fresh + started]
    chosen = set(picks)
    return picks + [version for version in versions if version not in chosen]


def main() -> int:
    args = parse_args()
    config = util.load_config()
    proxy = args.proxy or util.proxy_url()
    ctx = plugins.Context(config, proxy=proxy)
    ctx.ensure_index()
    batch = args.batch if args.batch is not None else int(config.get("batch", {}).get("defaultSize", 20))

    available = plugins.load_all(config)
    matched_version = False
    matrix: list[dict] = []
    for plugin in available:
        if args.plugin and plugin.name != args.plugin:
            continue
        metadata = plugin.call("metadata")
        package = metadata["package"]
        entries = plugin.call("list_versions", ctx) or []
        if not entries:
            util.warn(f"[{plugin.name}] no versions discovered")
            continue
        if args.report_skipped:
            skipped = plugin.call("report_skipped", ctx, entries) or []
            if skipped:
                util.log(f"[{plugin.name}] upstream tags not tracked by the recipe repo (skipped): "
                         + ", ".join(skipped))
        built = set() if args.rebuild_all else index_store.built_versions(ctx.index_root, package)
        if args.version:
            pending = [item for item in entries if item["version"] == args.version]
            if not pending:
                # One plugin not tracking a version is normal: a version input is
                # routinely meant for exactly one of them (`-f version=26.20.7`
                # with plugins that have no 26.20 line at all).
                util.log(f"[{plugin.name}] does not track {args.version}")
                continue
            matched_version = True
        else:
            pending = [item for item in entries if item["version"] not in built]
        # A release line the ledger has nothing for comes before one it already
        # covers: see cover_lines_first().
        covered = (
            set()
            if args.rebuild_all
            else index_store.versions_with_artifacts(
                ctx.index_root, package, int(config.get("revision", 1))
            )
        )
        pending = cover_lines_first(
            semver.sort_desc([item["version"] for item in pending]), covered
        )
        by_version = {item["version"]: item for item in entries}
        if args.first_only:
            pending = pending[:1]
        else:
            pending = pending[:batch]
        for version in pending:
            if not args.rebuild_all and version in built and not args.version:
                continue
            item = by_version[version]
            profiles = [
                found["id"]
                for found in (
                    plugin.call("toolchain_aliases", ctx, version, item["commit"]) or []
                )
            ]
            done = (
                set()
                if args.rebuild_all
                else index_store.built_entries_with_profiles(
                    ctx.index_root,
                    package,
                    version,
                    int(config.get("revision", 1)),
                    profiles,
                )
            )
            for entry in plugin.entries(ctx, version, item["commit"]):
                if entry["id"] in done:
                    continue
                matrix.append(plan.job(config, ctx, plugin, version, item["commit"], entry))

    limit = int(config.get("batch", {}).get("maxMatrix", 200))
    matrix = matrix[:limit]
    if args.version and not matched_version:
        util.die(f"version {args.version} is not tracked by any plugin")
    # A plugin's own SDK dependency (LeviLamina, for each of its plugins) is built
    # ahead of it when the ledger has no artifact for the version it resolves: see
    # lib/plan.py. Everything that cannot be resolved that way stays in one stage.
    revision = int(config.get("revision", 1))
    first, dependencies, waiting, notes = plan.split(
        config, ctx, available, matrix,
        lambda package, version: index_store.has_artifacts(
            ctx.index_root, package, version, revision
        ),
    )
    for note in notes:
        util.log(f"plan: {note}")

    payload = json.dumps(first)
    dependency_payload = json.dumps(dependencies)
    dependents = json.dumps(waiting)
    util.log(
        f"discover: {len(first)} job(s), {len(dependencies)} built for the jobs that "
        f"wait ({len(waiting)})"
    )
    for item in first + dependencies + waiting:
        util.log(f"  {item['package']}@{item['version']}")
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as handle:
            handle.write(f"matrix={payload}\n")
            handle.write(f"dependencies={dependency_payload}\n")
            handle.write(f"dependents={dependents}\n")
    if args.out:
        # The local runner walks this in order, so the stage that produces the
        # dependencies comes first and the results directory is ready for the ones
        # that come after it.
        util.write_text(Path(args.out), json.dumps(first + dependencies + waiting) + "\n")
    if not output and not args.out:
        util.log(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
