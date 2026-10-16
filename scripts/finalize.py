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
        # Our own ledger first: it is the work this round produced, and pushing it
        # cannot be blocked by anything about the consumer-facing repository. (If
        # that one fails -- a token without access, say -- the index is still
        # there for the next round to build on.)
        index_branch = (config.get("repos", {}).get("index") or {}).get("branch", "index")
        gitops.commit_and_push(
            ctx.index_root,
            f"index: {', '.join(sorted({r['package'] for r in results}))}",
            push=True,
        )
        util.log(f"index pushed to branch '{index_branch}'")
        gitops.commit_and_push(
            package_repo,
            f"recipes+manifests: {', '.join(sorted({r['package'] for r in results}))}",
        )
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
        package = result["package"]
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
                if artifact.get("package") == package
            ]
            record = dict(result, artifacts=own)
            # The ledger is keyed by the package's own name, because that is what
            # `discover`, `verify` and the release notes read -- and what a
            # consumer resolves. Our repository carries the manifest only, so a
            # project requiring the upstream package is served by our archive
            # without a copy of the recipe ever being involved.
            index_store.write_entry(ctx.index_root, package, version, variant, record)
            for artifact in result.get("artifacts", []):
                name = artifact["package"]
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
            # A project's package has *entries* (LeviLamina builds a server and a
            # client SDK), and its artifact belongs to whichever of them matches
            # what it was built with -- a dependency record under "default" would
            # put an entry the project does not have next to its own, in the ledger
            # and in every release body that shows the column.
            entry = _owning_entry(ctx, package, version, record.get("configs") or {})
            if entry:
                index_store.write_single(ctx.index_root, package, version, entry, record)
            else:
                # No entry of the owner matches it: the owner's own build produces
                # this artifact later, and until then nothing here is ours to say.
                util.log(
                    f"{package} {version}: no entry of the owning plugin matches "
                    f"{record.get('configs') or '{}'}; not recorded from this build"
                )
            continue
        index_store.write_single(ctx.index_root, package, version, "default", record)
    return entries


def _owning_entry(ctx, package: str, version: str, configs: dict) -> str:
    """The entry of the package's owning plugin that these configs are."""
    for plugin in plugins.load_all(ctx.config):
        metadata = plugin.call("metadata") or {}
        if (metadata.get("package") or "").lower() != package.lower():
            continue
        commit = ""
        for item in plugin.call("list_versions", ctx) or []:
            if item["version"] == version:
                commit = item["commit"]
                break
        for entry in plugin.entries(ctx, version, commit):
            declared = entry.get("configs") or {}
            if all(configs.get(key) == value for key, value in declared.items()):
                return entry["id"]
    return ""


def refresh_release_notes(config: dict, ctx) -> None:
    """Keep each release body listing what that shard currently holds."""
    from lib import gh

    cache_cfg = config["repos"]["cache"]
    owner, repo = cache_cfg["owner"], cache_cfg["repo"]
    # The core columns come from the framework, the rest from the project's own
    # plugin; a column that is empty in every row of a shard is not rendered.
    table = reports.for_plugins(config, ctx, plugins.load_all(config))
    shards = release_rows(config, ctx)
    failed = []
    for shard, rows in sorted(shards.items()):
        written = gh.update_release_notes(
            owner,
            repo,
            shard,
            gh.release_notes(
                shard,
                rows,
                table=table,
            ),
            config=config,
        )
        if not written:
            failed.append(shard)
    if shards:
        # A body that could not be rewritten describes an older shard content, and
        # nothing else in the round would ever correct it: say so, and keep the
        # rest of the round's work.
        util.log(
            f"refreshed release notes for {len(shards) - len(failed)} shard(s)"
            + (f", {len(failed)} left stale: {', '.join(failed)}" if failed else "")
        )


def release_rows(config: dict, ctx) -> dict[str, list[dict]]:
    """The table rows of every release shard."""
    from lib import index_store

    own = plugins.project_packages(config, ctx)
    shards: dict[str, dict[str, dict]] = {}
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
    return {
        shard: reports.newest_first(list(rows.values()))
        for shard, rows in shards.items()
    }


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

    The manifests themselves come from :func:`lib.manifest.write_repo`, the same
    generator a build uses for the repository it hands to xmake, so the artifacts
    a consumer can install and the ones a build reuses cannot drift apart.
    """
    index_root = Path(index_root)
    indexed = manifest_lib.write_repo(index_root, package_repo)
    _drop_stale_manifests(package_repo, indexed)


def _drop_stale_manifests(
    package_repo: Path,
    indexed: dict[str, set[str]],
) -> None:
    """Delete the manifest directories of packages the cache no longer holds.

    The repository holds manifests and nothing else, so a directory that carries
    one is ours to remove -- the recipes come from the official repositories, and
    a package without a manifest simply builds from source there.
    """
    root = Path(package_repo) / "packages"
    if not root.is_dir():
        return

    dropped: list[str] = []
    for version_dir in sorted(root.glob("*/*/*")):
        if not version_dir.is_dir():
            continue
        package = version_dir.parent.name
        # Only a directory that holds nothing but a manifest is ours: a recipe
        # would keep its own files (xmake.lua, versions/versions.txt) there.
        if {item.name for item in version_dir.iterdir()} != {"manifest.txt"}:
            continue
        if version_dir.name in indexed.get(package, set()):
            continue
        util.rmtree(version_dir)
        dropped.append(f"{package} {version_dir.name}")
    # A package that had nothing but manifests disappears with its last one.
    for package_dir in sorted(root.glob("*/*")):
        if package_dir.is_dir() and not any(package_dir.iterdir()):
            package_dir.rmdir()
    if dropped:
        util.log(
            f"dropped {len(dropped)} manifest(s) the index does not back: "
            + ", ".join(sorted(dropped)[:3])
            + (f" (+{len(dropped) - 3})" if len(dropped) > 3 else "")
        )


if __name__ == "__main__":
    raise SystemExit(main())
