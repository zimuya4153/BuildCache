"""Publish what the build jobs produced: manifests, index and git pushes."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib import (  # noqa: E402
    gitops,
    index_store,
    manifest as manifest_lib,
    plugins,
    reports,
    util,
    verify,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", default="results")
    parser.add_argument("--dry-run", action="store_true", help="write nothing, push nothing")
    parser.add_argument("--push", action="store_true", help="commit and push the generated repos")
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="only check that every indexed artifact matches its published release asset",
    )
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
    if args.verify_only:
        ctx.ensure_index()
        verify.verify_or_die(config, ctx.index_root)
        return 0
    if not results:
        util.log("no build results found")
        return 0

    package_repo = Path(ctx.package_repo)
    if not args.dry_run:
        ctx.ensure_index(create=True)
        package_cfg = config["repos"]["package"]
        url = f"https://github.com/{package_cfg['owner']}/{package_cfg['repo']}.git"
        gitops.ensure_checkout(config, url, package_repo, branch=package_cfg.get("branch"))
        # CI may have published since this checkout was made; the manifests are
        # regenerated below anyway, and the recipes stay as they are.
        gitops.sync_branch(package_repo)

    entries = write_ledger(config, ctx, results, dry_run=args.dry_run)

    if not args.dry_run:
        # A dependency shared by two entries is packed by both jobs; the digest
        # of the archive that lost the upload race would make every consumer
        # fall back to a source build, so the index adopts what is online.
        adopted = verify.adopt_published_digests(config, ctx.index_root)
        if adopted:
            # One line, not one warning per artifact: two jobs sharing a
            # dependency is the normal case (server and client have the same
            # closure), and their archives are never byte identical. Naming a
            # couple of them is enough to tell a real mistake from this.
            names = sorted(item["asset"] for item in adopted)
            shown = ", ".join(names[:3]) + (f", … (+{len(names) - 3})" if len(names) > 3 else "")
            util.log(
                f"{len(adopted)} artifact(s) were packed by two jobs; the index now "
                f"carries the digest of the published file ({shown})"
            )
        regenerate_manifests(config, ctx, index_root=ctx.index_root, package_repo=package_repo)
        problems = verify.check_index_entries(config, ctx.index_root)
        if problems:
            for item in problems:
                util.warn(f"{item['asset']}: {item['problem']}")
            requeued = verify.requeue(config, ctx.index_root, problems)
            util.log(
                f"verify: {len(problems)} artifact(s) are stale; "
                f"requeued {len(requeued)} entry/entries for the next round: "
                + ", ".join(sorted(requeued))
            )
        else:
            util.log("verify: every indexed artifact matches its published release asset")

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


def _component_record(result: dict, package: str, version: str) -> dict:
    """The ledger record for a package that came in as a dependency.

    It carries what the build knows about it (how it was built, with what, and
    for which project), but no entry id: the entry dimension belongs to the
    project that has one, not to everything that project pulled in.
    """
    return {
        "package": package,
        "version": version,
        "prefix": result.get("prefix"),
        "built_at": result.get("built_at"),
        "xmake_version": result.get("xmake_version"),
        "toolchain": result.get("toolchain"),
        "vs_toolset": result.get("vs_toolset"),
        "revision": result.get("revision"),
        "configs": result.get("configs"),
        "alias_profiles": result.get("alias_profiles") or [],
        "compiled_with": result.get("toolchain"),
        # The project and *its* version; ``version`` here is the dependency's own,
        # which the record already carries on its own.
        "produced_for": f"{result['package']}@{result.get('version') or ''}",
        "artifacts": [],
    }


def write_ledger(config: dict, ctx, results: list[dict], dry_run: bool = False) -> int:
    """Write the index records for one round and return how many artifacts were seen.

    The *entry* dimension (LeviLamina's server/client) belongs to the project that
    has one, so only its own package is recorded per entry. A dependency is the
    same artifact whichever entry pulled it in: recording it per entry would claim
    two builds for something built once and would put an entry name, and an
    implied ``target_type``, on packages that know nothing about either.
    """
    own_packages = plugins.project_packages(config, ctx)
    components: dict[tuple[str, str], dict] = {}
    entries = 0
    for result in results:
        version = result["version"]
        variant = result.get("entry", "default")
        published = ctx.package_name(result["package"])
        if not dry_run:
            # What compiled the sources, which is not the toolchain the package
            # was resolved with: LeviLamina's own xmake.lua forces clang-cl while
            # the package (and therefore the build hash) uses msvc.
            result["compiled_with"] = result.get("upstream_toolchain") or result.get(
                "toolchain"
            )
            # The record of the project is about the project's own package; the
            # rest of the closure is recorded per dependency further down.
            own = [
                artifact
                for artifact in result.get("artifacts", [])
                if artifact.get("package") == published
            ]
            record = dict(result, artifacts=own)
            # The ledger is keyed by the published name, because that is what
            # `discover`, `verify` and the release notes read. The same build is
            # also recorded under the upstream name, which is what makes
            # `packages/l/levilamina/<version>/manifest.txt` appear in the
            # consumer repository: a project that requires the upstream package
            # from its own repository can then be served by our artifacts too.
            index_store.write_entry(ctx.index_root, published, version, variant, record)
            if result["package"] != published:
                index_store.write_entry(
                    ctx.index_root, result["package"], version, variant, record
                )
            for artifact in result.get("artifacts", []):
                # A dependency is published under the name its own project asks
                # for as well as under our prefix. xmake resolves a manifest by
                # the name it is resolving, searching *every* repository
                # (``repository.artifacts_manifest``), so the recipe can come from
                # the official repository while the archive comes from ours: a
                # project that never heard of the prefix gets the same prebuilt
                # package and compiles nothing.
                names = [artifact["package"]]
                upstream = artifact.get("upstream_package") or artifact["package"]
                if upstream != artifact["package"] and artifact["package"] != published:
                    names.append(upstream)
                for name in names:
                    record = components.setdefault(
                        (name, artifact["version"]),
                        _component_record(result, name, artifact["version"]),
                    )
                    # The same dependency comes out of every entry of the project;
                    # it is one artifact, so it is listed once.
                    if artifact["key"] not in {item["key"] for item in record["artifacts"]}:
                        record["artifacts"].append(artifact)
        entries += len(result.get("artifacts", []))
        util.log(
            f"{result['package']}@{version} [{variant}]: "
            f"{len(result.get('artifacts', []))} artifact(s)"
        )
    if dry_run:
        return entries
    for (package, version), record in sorted(components.items()):
        known = {
            artifact.get("key")
            for artifact in index_store.all_artifacts(
                index_store.version_meta(ctx.index_root, package, version)
            )
        }
        if package in own_packages and {item["key"] for item in record["artifacts"]} <= known:
            # Another plugin's build owns this package and records it per entry,
            # and it already knows exactly this artifact.
            continue
        if package in own_packages:
            util.warn(
                f"{package} {version}: this build produced an artifact the owning "
                f"plugin does not have; recorded it as well"
            )
        index_store.write_single(ctx.index_root, package, version, "default", record)
    return entries


def refresh_release_notes(config: dict, ctx) -> None:
    """Keep each release body listing what that shard currently holds."""
    from lib import gh

    cache_cfg = config["repos"]["cache"]
    owner, repo = cache_cfg["owner"], cache_cfg["repo"]
    # The core columns come from the framework, the rest from the project's own
    # plugin; a column that is empty in every row of a shard is not rendered.
    table = reports.for_plugins(config, ctx, plugins.load_all(config))
    shards, prefix = release_rows(config, ctx)
    for shard, rows in sorted(shards.items()):
        gh.update_release_notes(
            owner,
            repo,
            shard,
            gh.release_notes(
                shard,
                rows,
                table=table,
                prefix=prefix,
            ),
            config=config,
        )
    if shards:
        util.log(f"refreshed release notes for {len(shards)} shard(s)")


def release_rows(config: dict, ctx) -> tuple[dict[str, list[dict]], str]:
    """The table rows of every release shard, and the package prefix."""
    from lib import index_store

    own = plugins.project_packages(config, ctx)
    shards: dict[str, dict[str, dict]] = {}
    prefix = ""
    for directory in sorted(Path(ctx.index_root).iterdir()):
        if not directory.is_dir() or directory.name.startswith("."):
            continue
        for meta in index_store.all_version_meta(ctx.index_root, directory.name):
            for variant, entry in index_store.entries(meta).items():
                for artifact in entry.get("artifacts") or []:
                    if not artifact.get("release"):
                        continue
                    row = reports.artifact_row(
                        artifact,
                        version=artifact.get("version") or meta.get("version") or "",
                        entry_id=variant,
                        entry=entry,
                        own=directory.name in own,
                    )
                    shards.setdefault(artifact["release"], {})[row["asset"]] = row
                    prefix = prefix or str(meta.get("prefix") or "")
    return (
        {
            shard: reports.newest_first(list(rows.values()))
            for shard, rows in shards.items()
        },
        prefix,
    )


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
    """Rewrite every manifest from the index so the repo is always reproducible.

    A version the index no longer has also loses its manifest: a manifest points
    at a released asset, so one left behind would send a consumer to a file that
    is gone. The recipe stays, and that is enough -- a package without a manifest
    simply builds from source.
    """
    index_root = Path(index_root)
    if not index_root.is_dir():
        return
    indexed: dict[str, set[str]] = {}
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
                # A derived alias is the same archive under another consumer
                # toolchain: same url, same checksum, different build id.
                for alias in artifact.get("aliases") or []:
                    entries[alias["key"]] = manifest_lib.entry(
                        artifact["url"],
                        artifact["sha256"],
                        alias.get("toolset") or artifact.get("toolset"),
                    )
            manifest_lib.write_all(package_repo, package, version, entries)
            indexed.setdefault(package, set()).add(version)
    # A project is published under two names: the prefixed one and the upstream
    # one (so a project requiring the upstream package is served as well), and
    # only the plugin knows the second -- after a full wipe the index is empty.
    upstream = {name.removeprefix(ctx.prefix) for name in plugins.project_packages(config, ctx)}
    _drop_stale_manifests(package_repo, indexed, ctx.prefix, upstream)


def _drop_stale_manifests(
    package_repo: Path,
    indexed: dict[str, set[str]],
    prefix: str,
    upstream_names: set[str],
) -> None:
    """Delete the manifest directories of packages the cache no longer holds.

    The scan looks for manifest directories, not for recipes: a project is also
    published under its upstream name, and that directory (``packages/l/levilamina``)
    holds manifests only -- the recipe lives under the prefixed name.
    """
    root = Path(package_repo) / "packages"
    if not root.is_dir():
        return

    def ours(package: str) -> bool:
        return (
            package.startswith(prefix) or package in upstream_names or package in indexed
        )

    dropped: list[str] = []
    for version_dir in sorted(root.glob("*/*/*")):
        if not version_dir.is_dir():
            continue
        package = version_dir.parent.name
        if not ours(package):
            continue
        # Only a directory that holds nothing but a manifest is ours: a recipe
        # keeps its own files (xmake.lua, versions/versions.txt) where they are.
        if {item.name for item in version_dir.iterdir()} != {"manifest.txt"}:
            continue
        if version_dir.name in indexed.get(package, set()):
            continue
        util.rmtree(version_dir)
        dropped.append(f"{package} {version_dir.name}")
    # A package that had nothing but manifests disappears with its last one.
    for package_dir in sorted(root.glob("*/*")):
        if package_dir.is_dir() and ours(package_dir.name) and not any(package_dir.iterdir()):
            package_dir.rmdir()
    if dropped:
        util.log(
            f"dropped {len(dropped)} manifest(s) the index does not back: "
            + ", ".join(sorted(dropped)[:3])
            + (f" (+{len(dropped) - 3})" if len(dropped) > 3 else "")
        )


if __name__ == "__main__":
    raise SystemExit(main())
