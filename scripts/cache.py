"""Manage the prebuilt SDK cache: inspect, prune and trigger builds.

Examples:

    python scripts/cache.py list
    python scripts/cache.py list --package levilamina
    python scripts/cache.py remove levilamina@26.51.6 --dry-run
    python scripts/cache.py remove levilamina --include-dependents --yes
    python scripts/cache.py remove '*' --all --yes
    python scripts/cache.py trigger --plugin ll --first-only --local
    python scripts/cache.py trigger --plugin ll --workflow --batch 5
    python scripts/cache.py verify

Destructive commands print their plan and do nothing unless ``--yes`` is given
(``--dry-run`` is the implicit default).
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib import gh, gitops, index_store, plugins, reports, util, verify  # noqa: E402

_SPEC = re.compile(r'"([A-Za-z0-9][A-Za-z0-9_.\-]*)(?:\[[^\]]*\])?(?:\s+[^"]*)?"')


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--proxy", default=None)
    sub = parser.add_subparsers(dest="command", required=True)

    listing = sub.add_parser("list", help="show what is cached")
    listing.add_argument("--package", default="")
    listing.add_argument("--versions", action="store_true", help="include per-version detail")

    graph = sub.add_parser("graph", help="show the dependency graph of cached packages")
    graph.add_argument("--package", default="", help="only this package and its neighbourhood")

    remove = sub.add_parser("remove", help="delete cached artifacts")
    remove.add_argument("targets", nargs="+", help="package, package@version or *")
    remove.add_argument("--include-deps", action="store_true", help="also delete its dependencies")
    remove.add_argument(
        "--include-dependents",
        action="store_true",
        help="also delete the packages that depend on it",
    )
    remove.add_argument(
        "--recursive",
        action="store_true",
        help="repeat dependency traversal until nothing new is reachable",
    )
    remove.add_argument("--all", action="store_true", help="delete every cached package")
    remove.add_argument("--prune-releases", action="store_true", help="also delete empty release shards")
    remove.add_argument("--dry-run", action="store_true", help="only print the plan (default)")
    remove.add_argument("--yes", action="store_true", help="actually delete")
    remove.add_argument("--no-push", action="store_true", help="do not push the regenerated repos")

    trigger = sub.add_parser("trigger", help="run one build round")
    trigger.add_argument("--plugin", default="")
    trigger.add_argument("--version", default="")
    trigger.add_argument("--entry", default="")
    trigger.add_argument("--batch", default="")
    trigger.add_argument("--first-only", action="store_true")
    trigger.add_argument("--rebuild-all", action="store_true")
    trigger.add_argument("--local", action="store_true", help="build on this machine")
    trigger.add_argument("--workflow", action="store_true", help="dispatch the GitHub workflow")
    trigger.add_argument("--no-push", action="store_true", help="local only: skip finalize push")
    trigger.add_argument("--dry-run", action="store_true", help="print what would be built")

    verify_cmd = sub.add_parser("verify", help="check published artifacts against the index")
    verify_cmd.add_argument(
        "--push",
        action="store_true",
        help="push the index after requeueing stale entries",
    )

    orphans = sub.add_parser(
        "orphan-assets",
        help="assets no manifest and no index entry points at any more",
    )
    orphans.add_argument("--package", default="", help="only assets of this package")
    orphans.add_argument("--dry-run", action="store_true", help="only print the plan (default)")
    orphans.add_argument("--yes", action="store_true", help="actually delete")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = util.load_config()
    proxy = args.proxy or util.proxy_url()
    ctx = plugins.Context(config, proxy=proxy)
    if args.command == "verify":
        ctx.ensure_index()
        problems = verify.check_index_entries(config, ctx.index_root)
        if not problems:
            util.log("verify: every indexed artifact matches its published release asset")
            return 0
        for item in problems:
            util.warn(f"{item['asset']}: {item['problem']}")
        requeued = verify.requeue(config, ctx.index_root, problems)
        util.log(f"requeued {len(requeued)} entry/entries: " + ", ".join(sorted(requeued)))
        if requeued and args.push:
            gitops.commit_and_push(ctx.index_root, "index: requeue stale entries")
        return 0
    if args.command == "list":
        return cmd_list(ctx, args)
    if args.command == "graph":
        return cmd_graph(ctx, args)
    if args.command == "remove":
        return cmd_remove(config, ctx, args)
    if args.command == "trigger":
        return cmd_trigger(config, ctx, args)
    if args.command == "orphan-assets":
        return cmd_orphan_assets(config, ctx, args)
    return 1


# --------------------------------------------------------------------------- list
def cmd_list(ctx, args) -> int:
    ctx.ensure_index()
    # Same columns as a release body: the core ones plus whatever the project's
    # plugin registers, so the two views cannot drift apart.
    table = reports.for_plugins(ctx.config, ctx, plugins.load_all(ctx.config))
    own = plugins.project_packages(ctx.config, ctx)
    total = 0
    for package, version, variant, artifacts in _inventory(ctx, args.package):
        size = sum(int(item.get("size") or 0) for item in artifacts)
        total += size
        util.log(
            f"{package}@{version} [{variant}]  {len(artifacts)} artifact(s)  {size / 1e6:.1f} MB"
        )
        if args.versions:
            rows = [
                reports.artifact_row(
                    item, version=version, entry_id=variant, own=package in own
                )
                for item in artifacts
            ]
            for line in table.render(rows):
                util.log("  " + line)
    util.log(f"total: {total / 1e6:.1f} MB")
    return 0


def _inventory(ctx, package_filter: str = ""):
    root = ctx.index_root
    if not root.is_dir():
        return
    for directory in sorted(root.iterdir()):
        if not directory.is_dir() or directory.name.startswith("."):
            continue
        if package_filter and directory.name != package_filter:
            continue
        for meta in index_store.all_version_meta(root, directory.name):
            for variant, entry in sorted(index_store.entries(meta).items()):
                yield directory.name, str(meta.get("version")), variant, entry.get("artifacts") or []


# -------------------------------------------------------------------------- graph
def _recipe_sources(ctx) -> list[Path]:
    """The recipe mirrors the projects are built from (cloned on demand)."""
    return [
        ctx.mirror(source["name"], source["url"])
        for source in ctx.config.get("recipes", {}).get("sources", [])
    ]


def _read_deps(roots: list[Path]) -> dict[str, set[str]]:
    """Dependency edges between packages, from the recipe sources.

    The repository we publish holds manifests only, so the edges come from the
    recipes the projects are built with (the mirrors); every package is named as
    the upstream repository names it, which is also how it appears in the ledger.
    """
    graph: dict[str, set[str]] = {}
    for root in sorted(Path(directory) for directory in roots):
        for recipe in sorted(root.glob("packages/*/*/xmake.lua")):
            package = recipe.parent.name
            edges: set[str] = set()
            files = sorted((recipe.parent / "versions").glob("*.lua")) + [recipe]
            for path in files:
                for match in _SPEC.finditer(util.read_text(path)):
                    name = match.group(1)
                    if name != package:
                        edges.add(name)
            graph[package] = edges
    return graph


def cmd_graph(ctx, args) -> int:
    graph = _read_deps(_recipe_sources(ctx))
    if args.package:
        focus = args.package
        util.log(f"{focus} depends on: {', '.join(sorted(graph.get(focus, []))) or '-'}")
        dependents = sorted(name for name, edges in graph.items() if focus in edges)
        util.log(f"depended on by : {', '.join(dependents) or '-'}")
        return 0
    for name in sorted(graph):
        util.log(f"{name}: {', '.join(sorted(graph[name])) or '-'}")
    return 0


def _closure(roots: set[str], graph: dict[str, set[str]], reverse: bool, recursive: bool) -> set[str]:
    result = set(roots)
    frontier = set(roots)
    while frontier:
        nxt: set[str] = set()
        for name in frontier:
            if reverse:
                nxt |= {other for other, edges in graph.items() if name in edges}
            else:
                nxt |= graph.get(name, set())
        nxt -= result
        result |= nxt
        frontier = nxt if recursive else set()
    return result


def orphan_assets(referenced: set[str], assets: dict[str, int], package: str = "") -> list[str]:
    """Release assets nothing points at any more, newest name first.

    ``referenced`` is every asset name the ledger and the consumer manifests name;
    ``assets`` is what the release shards actually hold, with sizes. An asset that
    only the *ledger* names is kept as well -- `finalize` regenerates the consumer
    manifests from it, so deleting one would take the file out from under an entry
    that is still meant to be reachable.
    """
    found = [name for name in assets if name not in referenced]
    if package:
        found = [name for name in found if name.split("-", 1)[0] == package]
    return sorted(found, key=lambda name: (-assets.get(name, 0), name))


def cmd_orphan_assets(config: dict, ctx, args) -> int:
    """Delete release assets no longer referenced by the index or a manifest.

    An artifact replaced by a newer build of the same version (another MSVC
    generation, or the same build published again after a revision bump) stops
    being addressed by anything, but its file stays in the shard: the release then
    lists downloads that no manifest will ever ask for.
    """
    from lib import manifest as manifest_lib

    cache_cfg = config["repos"]["cache"]
    owner, repo = cache_cfg["owner"], cache_cfg["repo"]
    referenced = _referenced_assets(config, ctx, manifest_lib)
    assets = _release_assets(owner, repo, config, ctx.proxy)
    found = orphan_assets(referenced, assets, args.package)
    if not found:
        util.log(f"no orphan assets ({len(assets)} asset(s) checked, all referenced)")
        return 0
    total = sum(assets[name] for name in found)
    util.log(f"plan: {len(found)} orphan asset(s), {total / 1e6:.1f} MB of {len(assets)}")
    for name in found[:20]:
        util.log(f"  {name}  {assets[name] / 1e6:.1f} MB")
    if len(found) > 20:
        util.log(f"  ... and {len(found) - 20} more")
    if not args.yes:
        util.log("dry run (pass --yes to delete)")
        return 0
    by_shard = _assets_by_shard(owner, repo, config)
    shards = {shard: names for shard, names in by_shard.items() if names.keys() & set(found)}
    for shard, names in shards.items():
        for name in sorted(names.keys() & set(found)):
            _delete_asset(config, ctx, owner, repo, shard, name)
    import finalize  # a body lists what its shard holds

    finalize.refresh_release_notes(config, ctx)
    util.log(f"deleted {len(found)} orphan asset(s) from {len(shards)} shard(s)")
    return 0


def _delete_asset(config: dict, ctx, owner: str, repo: str, shard: str, name: str) -> None:
    util.run(
        ["gh", "release", "delete-asset", shard, name, "--repo", f"{owner}/{repo}", "--yes"],
        env=util.subprocess_env(config, proxy=ctx.proxy),
        capture=True,
        check=False,
        quiet=True,
    )


def _referenced_assets(config: dict, ctx, manifest_lib) -> set[str]:
    """Every asset name the ledger or a consumer manifest still points at."""
    names: set[str] = set()
    ctx.ensure_index()
    for directory in sorted(ctx.index_root.iterdir()):
        if not directory.is_dir() or directory.name.startswith("."):
            continue
        for meta in index_store.all_version_meta(ctx.index_root, directory.name):
            for entry in index_store.entries(meta).values():
                for artifact in entry.get("artifacts") or []:
                    if artifact.get("asset"):
                        names.add(artifact["asset"])
                    for alias in artifact.get("aliases") or []:
                        if alias.get("asset"):
                            names.add(alias["asset"])
    package_cfg = config["repos"]["package"]
    package_repo = Path(ctx.package_repo)
    gitops.ensure_checkout(
        config,
        f"https://github.com/{package_cfg['owner']}/{package_cfg['repo']}.git",
        package_repo,
        branch=package_cfg.get("branch"),
    )
    # This decides what gets *deleted*, so a stale checkout is not good enough: a
    # failed fetch would make every asset only the newest manifests name look like
    # an orphan. Refuse instead of guessing.
    fetched = util.run(
        ["git", "-C", str(package_repo), "fetch", "--prune", "origin"],
        env=util.subprocess_env(config, proxy=ctx.proxy),
        capture=True,
        check=False,
        quiet=True,
    )
    if fetched.returncode != 0:
        util.die(
            "cannot read the consumer repository "
            f"({package_cfg['owner']}/{package_cfg['repo']}); refusing to guess which "
            "assets are orphans"
        )
    # Read them *from the fetched ref*, not from the working tree: this checkout is
    # also what the last publish wrote into, and its local commits are not what
    # consumers see.
    branch = package_cfg.get("branch") or "main"
    environment = util.subprocess_env(config, proxy=ctx.proxy)
    listed = util.run_out(
        [
            "git", "-C", str(package_repo), "ls-tree", "-r", "--name-only",
            f"origin/{branch}", "--", "packages",
        ],
        env=environment,
        check=False,
    )
    for path in [line for line in listed.splitlines() if line.endswith("manifest.txt")]:
        text = util.run_out(
            ["git", "-C", str(package_repo), "show", f"origin/{branch}:{path}"],
            env=environment,
            check=False,
        )
        names |= manifest_lib.asset_names(text)
    return names


def _release_assets(owner: str, repo: str, config: dict, proxy) -> dict[str, int]:
    """``{asset name: size}`` for every asset in every release of the cache."""
    found: dict[str, int] = {}
    for shard, names in _assets_by_shard(owner, repo, config).items():
        found.update({name: size for name, size in names.items()})
    return found


def _assets_by_shard(owner: str, repo: str, config: dict) -> dict[str, dict[str, int]]:
    """``{release tag: {asset name: size}}``, page by page."""
    found: dict[str, dict[str, int]] = {}
    for page in range(1, 11):
        batch = gh.api(
            f"repos/{owner}/{repo}/releases?per_page=100&page={page}", config=config
        )
        if not batch:
            break
        for release in batch:
            tag = release.get("tag_name") or ""
            if not tag:
                continue
            found[tag] = {
                asset.get("name"): int(asset.get("size") or 0)
                for asset in release.get("assets") or []
                if asset.get("name")
            }
        if len(batch) < 100:
            break
    return found


# ------------------------------------------------------------------------- remove
def cmd_remove(config: dict, ctx, args) -> int:
    ctx.ensure_index()
    graph = _read_deps(_recipe_sources(ctx))
    cached = {package for package, _, _, _ in _inventory(ctx)}

    if args.all or "*" in args.targets:
        targets = set(cached)
        versions: dict[str, set[str]] = {}
    else:
        targets = set()
        versions = {}
        for spec in args.targets:
            package, _, version = spec.partition("@")
            if package not in cached:
                util.die(f"{package} is not in the cache (see `cache.py list`)")
            targets.add(package)
            if version:
                versions.setdefault(package, set()).add(version)

    if args.include_deps:
        targets = _closure(targets, graph, reverse=False, recursive=args.recursive)
    if args.include_dependents:
        targets = _closure(targets, graph, reverse=True, recursive=args.recursive)

    plan: list[tuple[str, str, str, list[dict]]] = []
    for package, version, variant, artifacts in _inventory(ctx):
        if package not in targets:
            continue
        wanted = versions.get(package)
        if wanted and version not in wanted:
            continue
        plan.append((package, version, variant, artifacts))
    if not plan:
        util.log("nothing to remove")
        return 0

    total = sum(int(item.get("size") or 0) for _, _, _, artifacts in plan for item in artifacts)
    util.log(f"plan: {len(plan)} package version(s), {total / 1e6:.1f} MB")
    for package, version, variant, artifacts in plan:
        util.log(f"  {package}@{version} [{variant}]: {len(artifacts)} artifact(s)")
    if not args.yes:
        util.log("dry run (pass --yes to delete)")
        return 0

    cache_cfg = config["repos"]["cache"]
    owner, repo = cache_cfg["owner"], cache_cfg["repo"]
    shards: set[str] = set()
    for _, _, _, artifacts in plan:
        for item in artifacts:
            if item.get("release"):
                shards.add(item["release"])
            if item.get("asset"):
                util.run(
                    ["gh", "release", "delete-asset", item["release"], item["asset"],
                     "--repo", f"{owner}/{repo}", "--yes"],
                    env=util.subprocess_env(config, proxy=ctx.proxy),
                    capture=True,
                    check=False,
                    quiet=True,
                )
    _drop_from_index(ctx, plan)
    if args.prune_releases:
        for shard in sorted(shards):
            assets = gh.release_assets(owner, repo, shard, config=config)
            if assets is not None and not assets:
                util.run(
                    ["gh", "release", "delete", shard, "--repo", f"{owner}/{repo}", "--yes"],
                    env=util.subprocess_env(config, proxy=ctx.proxy),
                    capture=True,
                    check=False,
                    quiet=True,
                )
                util.run(
                    ["gh", "api", "--method", "DELETE",
                     f"repos/{owner}/{repo}/git/refs/tags/{shard}"],
                    env=util.subprocess_env(config, proxy=ctx.proxy),
                    capture=True,
                    check=False,
                    quiet=True,
                )
                util.log(f"removed empty release and tag {shard}")

    package_repo = Path(ctx.package_repo)
    package_cfg = config["repos"]["package"]
    gitops.ensure_checkout(
        config,
        f"https://github.com/{package_cfg['owner']}/{package_cfg['repo']}.git",
        package_repo,
        branch=package_cfg.get("branch"),
    )
    # Regenerating on a stale checkout keeps whatever it still holds, and a
    # manifest it rewrites from a stale tree can name an asset this run just
    # deleted. Sync first, then say what the ledger holds.
    gitops.sync_branch(package_repo)

    import finalize  # regenerates manifests from the index

    finalize.regenerate_manifests(config, ctx, index_root=ctx.index_root, package_repo=package_repo)
    # A release body lists what its shard currently holds, so the artifacts that
    # were just removed have to disappear from it here: a body that keeps naming a
    # deleted asset is the first thing a reader would try to download, and only a
    # round of `finalize` would ever rewrite it.
    finalize.refresh_release_notes(config, ctx)
    if not args.no_push:
        gitops.commit_and_push(package_repo, f"manifests: drop {len(plan)} version(s)")
        gitops.commit_and_push(ctx.index_root, f"index: drop {len(plan)} version(s)")
    util.log(f"removed {len(plan)} package version(s)")
    return 0


def _drop_from_index(ctx, plan) -> None:
    import json

    by_package: dict[str, set[tuple[str, str]]] = {}
    for package, version, variant, _ in plan:
        by_package.setdefault(package, set()).add((version, variant))
    for package, wanted in by_package.items():
        for path in sorted((ctx.index_root / package / "versions").glob("*.json")):
            doc = util.read_json(path, None)
            if not doc:
                continue
            version = str(doc.get("version"))
            entries = index_store.entries(doc)
            drop = {entry for item_version, entry in wanted if item_version == version}
            for entry in drop:
                entries.pop(entry, None)
            if not entries:
                path.unlink()
                continue
            # Written back under the key the readers use, and the legacy key is
            # dropped: leaving both would put two copies of the same map into a
            # generated document, which is how the two end up disagreeing.
            doc["entries"] = entries
            doc.pop("variants", None)
            path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
        index_path = ctx.index_root / package / "index.json"
        remaining = sorted(item.stem.replace(".json", "") for item in (ctx.index_root / package / "versions").glob("*.json"))
        if not remaining:
            # No version left: the package is gone from the cache, and an empty
            # package directory would only be noise in the ledger.
            util.rmtree(ctx.index_root / package)
            continue
        index = index_store.load_package(ctx.index_root, package)
        index["versions"] = remaining
        index["latest"] = remaining[0]
        index_path.write_text(json.dumps(index, indent=2) + "\n", encoding="utf-8")


# ------------------------------------------------------------------------ trigger
def cmd_trigger(config: dict, ctx, args) -> int:
    if args.workflow or not args.local:
        cache_cfg = config["repos"]["cache"]
        cmdline = ["gh", "workflow", "run", "build.yml",
                   "--repo", f"{cache_cfg['owner']}/{cache_cfg['repo']}"]
        for flag, value in (("plugin", args.plugin), ("version", args.version), ("batch", args.batch)):
            if value:
                cmdline += ["-f", f"{flag}={value}"]
        if args.first_only:
            cmdline += ["-f", "first_only=true"]
        if args.rebuild_all:
            cmdline += ["-f", "rebuild_all=true"]
        if args.dry_run:
            util.log("would run: " + " ".join(cmdline))
            return 0
        util.run(cmdline, env=util.subprocess_env(config, proxy=ctx.proxy))
        util.log("workflow dispatched; follow it with `gh run watch`")
        return 0

    scripts = Path(__file__).resolve().parent
    matrix_file = util.ROOT / ".work" / "matrix.json"
    discover = [sys.executable, str(scripts / "discover.py"), "--out", str(matrix_file)]
    if args.plugin:
        discover += ["--plugin", args.plugin]
    if args.version:
        discover += ["--version", args.version]
    if args.batch:
        discover += ["--batch", args.batch]
    if args.first_only:
        discover += ["--first-only"]
    if args.rebuild_all:
        discover += ["--rebuild-all"]
    util.run(discover, env=util.subprocess_env(config, proxy=ctx.proxy))
    matrix = util.read_json(matrix_file, []) or []
    if args.entry:
        matrix = [item for item in matrix if item.get("entry") == args.entry]
    if not matrix:
        util.log("nothing to build")
        return 0
    if args.dry_run:
        for item in matrix:
            waiting = item.get("waits_for") or []
            util.log(
                f"would build {item['package']}@{item['version']} [{item.get('entry')}]"
                + (f" after {', '.join(waiting)}" if waiting else "")
            )
        return 0
    # Local rounds run the stages the way the workflow does: the SDKs first, then
    # the jobs that reuse them, which read the results the stage before wrote.
    results_dir = util.ROOT / "results"
    util.rmtree(results_dir)
    for item in matrix:
        util.run(
            [sys.executable, str(scripts / "build.py"), "--plugin", item["plugin"],
             "--version", item["version"], "--entry", item.get("entry", ""),
             "--results", str(results_dir), "--dependency-results", str(results_dir)],
            env=util.subprocess_env(config, proxy=ctx.proxy),
        )
    finalize = [sys.executable, str(scripts / "finalize.py")]
    if not args.no_push:
        finalize.append("--push")
    util.run(finalize, env=util.subprocess_env(config, proxy=ctx.proxy))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
