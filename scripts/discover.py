"""Decide which SDK versions still need to be built."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib import index_store, plugins, semver, util  # noqa: E402


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


def cover_lines_first(versions: list[str]) -> list[str]:
    """Order versions so that every release line gets a build before any line gets two.

    Patch levels of one line differ in which upstream commits they carry, while
    lines differ in what the SDK is built from -- so covering the lines first is
    what turns a backlog into broad coverage quickly, and the remaining patches
    then only fill in what is still missing. Inside a line the representative is
    the middle patch level, biased towards the newest: neither the oldest (usually
    the least useful) nor the newest, which upstream may still be moving.

    ``versions`` arrives newest first and the order of the rest is preserved, so
    picking the next few from the front of the result is stable across runs.
    """
    lines: dict[str, list[str]] = {}
    for version in versions:
        lines.setdefault(_line_of(version), []).append(version)
    picks = [members[(len(members) - 1) // 2] for members in lines.values()]
    chosen = set(picks)
    return picks + [version for version in versions if version not in chosen]


def main() -> int:
    args = parse_args()
    config = util.load_config()
    proxy = args.proxy or util.proxy_url()
    ctx = plugins.Context(config, proxy=proxy)
    ctx.ensure_index()
    batch = args.batch if args.batch is not None else int(config.get("batch", {}).get("defaultSize", 20))

    matrix: list[dict] = []
    for plugin in plugins.load_all(config):
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
                util.die(f"version {args.version} is not tracked by the recipe repository")
        else:
            pending = [item for item in entries if item["version"] not in built]
        pending = cover_lines_first(semver.sort_desc([item["version"] for item in pending]))
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
                plan = plugin.call("toolchain_plan", ctx, version, item["commit"]) or {}
                matrix.append(
                    {
                        "plugin": plugin.name,
                        "package": package,
                        "recipe_package": metadata["package"],
                        "version": version,
                        "entry": entry["id"],
                        "image": plan.get("image") or "windows-latest",
                        "vs_toolset": plan.get("vs_toolset") or "",
                        "commit": item["commit"],
                    }
                )

    limit = int(config.get("batch", {}).get("maxMatrix", 200))
    matrix = matrix[:limit]
    payload = json.dumps(matrix)
    util.log(f"discover: {len(matrix)} job(s)")
    for item in matrix:
        util.log(f"  {item['package']}@{item['version']}")
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as handle:
            handle.write(f"matrix={payload}\n")
    if args.out:
        util.write_text(Path(args.out), payload + "\n")
    if not output and not args.out:
        util.log(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
