"""Regenerate the prefixed recipes inside the package repository working tree."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib import gitops, plugins, util, vendoring  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plugin", default="ll")
    parser.add_argument("--push", action="store_true", help="commit and push the package repository")
    parser.add_argument("--refresh", action="store_true", help="re-fetch the mirrors first")
    parser.add_argument("--proxy", default=None)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = util.load_config()
    proxy = args.proxy or util.proxy_url()
    ctx = plugins.Context(config, proxy=proxy)
    plugin = next((item for item in plugins.load_all(config) if item.name == args.plugin), None)
    if plugin is None:
        util.die(f"unknown plugin: {args.plugin}")
    if args.refresh:
        for source in config["recipes"]["sources"]:
            ctx.mirror(source["name"], source["url"])
    if args.push:
        # Vendoring writes into the checkout, and a rebase cannot run over
        # unstaged changes -- so get onto the remote's tip first, otherwise the
        # push below is rejected as non-fast-forward.
        gitops.sync_branch(Path(ctx.package_repo))
    marker = vendoring.vendor(config, ctx, plugin, refresh=args.refresh)
    util.log(f"vendored {len(marker['packages'])} packages under '{marker['prefix']}'")
    if args.push:
        gitops.commit_and_push(Path(ctx.package_repo), "recipes: regenerate prefixed packages")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
