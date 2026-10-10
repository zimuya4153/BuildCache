"""Show what the cache currently contains."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib import index_store, plugins, util  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--versions", action="store_true", help="list every cached version")
    args = parser.parse_args()
    config = util.load_config()
    ctx = plugins.Context(config)
    ctx.ensure_index()
    util.log(index_store.summary(ctx.index_root))
    if args.versions:
        for directory in sorted(Path(ctx.index_root).iterdir()):
            if not directory.is_dir():
                continue
            for meta in index_store.all_version_meta(ctx.index_root, directory.name):
                util.log(
                    f"{meta.get('package')}@{meta.get('version')}: "
                    f"{len(meta.get('artifacts', []))} artifact(s), "
                    f"xmake {meta.get('xmake_version')}, {meta.get('toolchain')}"
                )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
