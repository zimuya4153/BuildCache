"""Publish what the build jobs produced: manifests, index and git pushes."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib import gitops, index_store, manifest as manifest_lib, plugins, util  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", default="results")
    parser.add_argument("--dry-run", action="store_true", help="write nothing, push nothing")
    parser.add_argument("--push", action="store_true", help="commit and push the generated repos")
    parser.add_argument("--proxy", default=None)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = util.load_config()
    proxy = args.proxy or util.proxy_url()
    ctx = plugins.Context(config, proxy=proxy)

    results = []
    for path in sorted(Path(args.results).glob("build-result-*.json")):
        data = util.read_json(path, None)
        if data:
            results.append(data)
    if not results:
        util.log("no build results found")
        return 0

    package_repo = Path(ctx.package_repo)
    if not args.dry_run:
        ctx.ensure_index(create=True)
        package_cfg = config["repos"]["package"]
        url = f"https://github.com/{package_cfg['owner']}/{package_cfg['repo']}.git"
        gitops.ensure_checkout(config, url, package_repo, branch=package_cfg.get("branch"))

    entries = 0
    for result in results:
        version = result["version"]
        variant = result.get("variant", "default")
        if not args.dry_run:
            index_store.write_variant(
                ctx.index_root, result["package"], version, variant, result
            )
            # Also index every artifact under its own package so that each
            # component can be queried (and consumed) on its own.
            per_package: dict[str, list] = {}
            for artifact in result.get("artifacts", []):
                per_package.setdefault(artifact["package"], []).append(artifact)
            for package, artifacts in per_package.items():
                for artifact in artifacts:
                    index_store.write_variant(
                        ctx.index_root,
                        package,
                        artifact["version"],
                        variant,
                        {
                            "package": package,
                            "version": artifact["version"],
                            "prefix": result.get("prefix"),
                            "built_at": result.get("built_at"),
                            "xmake_version": result.get("xmake_version"),
                            "toolchain": result.get("toolchain"),
                            "vs_toolset": result.get("vs_toolset"),
                            "configs": result.get("configs"),
                            "produced_for": f"{result['package']}@{version}",
                            "artifacts": [artifact],
                        },
                    )
        for artifact in result.get("artifacts", []):
            entries += 1
        util.log(
            f"{result['package']}@{version} [{variant}]: "
            f"{len(result.get('artifacts', []))} artifact(s)"
        )

    if not args.dry_run:
        regenerate_manifests(config, ctx, index_root=ctx.index_root, package_repo=package_repo)

    util.log(f"indexed {entries} artifact entr(ies)")
    if args.dry_run:
        util.log("dry run: manifests/index left untouched")
        return 0

    util.log(index_store.summary(ctx.index_root))
    refresh_release_notes(config, ctx)
    if args.push:
        gitops.commit_and_push(
            package_repo,
            f"recipes+manifests: {', '.join(sorted({r['package'] for r in results}))}",
        )
        index_branch = (config.get("repos", {}).get("index") or {}).get("branch", "index")
        gitops.commit_and_push(
            ctx.index_root,
            f"index: {', '.join(sorted({r['package'] for r in results}))}",
            push=True,
        )
        util.log(f"index pushed to branch '{index_branch}'")
        _remove_legacy_index(config)
    return 0


def refresh_release_notes(config: dict, ctx) -> None:
    """Keep each release body listing what that shard currently holds."""
    from lib import gh, index_store

    cache_cfg = config["repos"]["cache"]
    owner, repo = cache_cfg["owner"], cache_cfg["repo"]
    shards: dict[str, set[str]] = {}
    prefix = ""
    for directory in sorted(Path(ctx.index_root).iterdir()):
        if not directory.is_dir():
            continue
        for meta in index_store.all_version_meta(ctx.index_root, directory.name):
            for artifact in index_store.all_artifacts(meta):
                if not artifact.get("release"):
                    continue
                shards.setdefault(artifact["release"], set()).add(artifact["version"])
                prefix = prefix or str(meta.get("prefix") or "")
    for shard, versions in sorted(shards.items()):
        gh.update_release_notes(
            owner,
            repo,
            shard,
            gh.release_notes(shard, sorted(versions), prefix=prefix),
            config=config,
        )
    if shards:
        util.log(f"refreshed release notes for {len(shards)} shard(s)")


def _remove_legacy_index(config: dict) -> None:
    """Drop the old in-tree ``index/`` directory from the framework branch."""
    legacy = util.ROOT / "index"
    if not legacy.is_dir():
        return
    tracked = util.run(
        ["git", "-C", str(util.ROOT), "ls-files", "index"],
        capture=True,
        check=False,
        quiet=True,
    ).stdout.strip()
    if not tracked:
        return
    util.run(["git", "-C", str(util.ROOT), "rm", "-r", "--quiet", "index"], quiet=True)
    util.run(
        ["git", "-C", str(util.ROOT), "commit", "--quiet", "-m", "index: move to the 'index' branch"],
        quiet=True,
    )
    util.run(["git", "-C", str(util.ROOT), "push", "origin", "HEAD"], quiet=True)
    util.log("moved the generated index out of the framework branch")


def regenerate_manifests(config: dict, ctx, index_root: Path, package_repo: Path) -> None:
    """Rewrite every manifest from the index so the repo is always reproducible."""
    index_root = Path(index_root)
    if not index_root.is_dir():
        return
    for directory in sorted(index_root.iterdir()):
        if not directory.is_dir():
            continue
        package = directory.name
        for meta in index_store.all_version_meta(index_root, package):
            version = str(meta.get("version"))
            entries = {}
            for artifact in index_store.all_artifacts(meta):
                entries[artifact["key"]] = manifest_lib.entry(
                    artifact["url"], artifact["sha256"], artifact.get("toolset")
                )
            manifest_lib.write_all(package_repo, package, version, entries)


if __name__ == "__main__":
    raise SystemExit(main())
