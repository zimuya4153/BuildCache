"""Build one SDK version end to end.

The whole dependency closure is installed by xmake itself into an isolated
sandbox, then exported, packed and published as precompiled artifacts.
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib import (  # noqa: E402
    aliases,
    gh,
    git_shim,
    gitops,
    manifest as manifest_lib,
    packer,
    plan,
    plugins,
    recipefix,
    sources,
    util,
    xmake_env,
)


def build_once(config, ctx, plugin, args, *, plat, arch, toolchain, vs_toolset,
               xmake_version, commit, require, configs, entry, proxy,
               recipe_cutoff: str, git_cutoff: str, binary_repo, plans: list[tuple[bool, str]]):
    """Build the closure in the first sandbox that configures and exports.

    A plan is ``(use_today, label)``. The first one takes today's recipes -- that is
    what a consumer resolves, so the artifacts land on addresses a consumer asks
    for -- with the versions the project's own file pins forced on the whole tree
    (see :func:`dependency_overrides`). It is the only plan when nothing moved in
    the recipe sources since the version was released, because then the era *is*
    today.

    The second plan rolls everything back to the era, and is only reached when the
    era's xmake cannot read today's recipes (0.8.3's 2.8.6 dies on `on_source`).
    That kind of failure happens while the recipes are parsed, before anything is
    compiled, so trying costs seconds rather than a build.

    Returns ``(sandbox, prepared, recipe_commits, used_today, repo_order)``.
    """
    own = plugin.call("metadata")["package"]
    overlay = (f"packages/{own[0].lower()}/{own}",)
    attempts = int(config.get("xmake", {}).get("installRetries", 3))
    last = "nothing to try"

    for use_today, label in plans:
        prepared = sources.prepare(
            config,
            ctx,
            plugin,
            cutoff=None if use_today else recipe_cutoff,
            label=label,
            overlay=overlay if use_today else (),
        )
        # A snapshot of the era is taken just *before* the release, so the recipe
        # in it may not know the version being built at all -- and then xmake
        # fetches the project by tag, which these repositories do not use for
        # their releases. See recipefix.declare_version.
        for note in recipefix.declare_version(prepared, own, args.version, commit):
            util.log(note)
        recipe_commits = {name: sha for name, (_dir, sha) in prepared.items()}
        main_repo = prepared.get("xmake-repo", (None, ""))[0]
        local_repos = [
            (name, directory)
            for name, (directory, _sha) in prepared.items()
            if name != "xmake-repo"
        ]
        # Other consumer repositories whose published manifests this build may
        # reuse: a cache is a cache, and the artifacts of another deployment are
        # the same builds when the hash matches (which is what xmake checks --
        # nothing here reads a manifest that is not about this very build).
        for slug in config.get("repos", {}).get("packageMirrors") or []:
            owner, _, name = str(slug).partition("/")
            if not owner or not name:
                continue
            checkout = util.work_dir(config, f"mirror-{name}")
            gitops.ensure_checkout(
                config, f"https://github.com/{owner}/{name}.git", checkout
            )
            # Say so: a mirror that is not there is indistinguishable from one
            # whose manifests never match, and the log has to tell them apart.
            util.log(
                f"reusing the artifacts {owner}/{name} publishes "
                f"({len(list((checkout / 'packages').glob('*/*/*')))} manifest(s) visible)"
            )
            local_repos.append((f"mirror-{name}", checkout))
        # xmake searches the project's own repositories before the main one, so the
        # recipe lookup for a package has to follow the same order.
        repo_order = local_repos + [("xmake-repo", main_repo)]
        requires = [f'add_requires("{require}", {{configs = {_lua_table(configs)}}})']
        extra = plan.extra_deps(
            plugin, ctx, args.version, commit, _own_recipe_dir(prepared, own)
        )
        # The project's own file may name a dependency its recipe already pins to
        # another version; asking for both would build a second SDK nobody uses.
        # Not `sources`: that is the module this loop already uses.
        declared_sources = {name: Path(directory) for name, directory in repo_order}
        # The dependencies are resolved at the version's own date; the snapshot's
        # date (+ a few days, so the version's own recipe entry exists) is only for
        # the recipes. See plugins/ll/plugin.py::dependency_cutoff for what the
        # difference cost (preloader v1.6.0 landing three days after 0.10.2).
        deps_cutoff = plugin.call("dependency_cutoff", ctx, args.version, commit) or recipe_cutoff
        pins = dependency_pins(config, ctx, declared_sources, extra, cutoff=deps_cutoff)
        requires += extra_requires(config, ctx, repo_order, extra, pinned=pins)
        overrides = dependency_overrides(ctx, plugin, args.version, commit, extra, pins)
        lua = list(requires)
        for name, chosen in sorted(overrides.items()):
            # `override` is what makes xmake ignore the version the recipe's own
            # `on_load` dependency resolved -- which is a range, and reads a
            # prerelease as inside it. See dependency_overrides.
            lua.append(
                f'add_requireconfs("**.{name}", {{override = true, version = "{chosen}"}})'
            )
        if overrides:
            util.log(
                "forced on the whole resolve tree: "
                + ", ".join(f"{name}={chosen}" for name, chosen in sorted(overrides.items()))
            )
        suffix = "" if use_today else "-era"
        sandbox = xmake_env.create_sandbox(
            config,
            name=_slug(f"{plugin.name}-{args.version}-{entry['id']}{suffix}"),
            xmake_version=xmake_version,
            proxy=proxy,
            toolchain=None if toolchain == "msvc" else toolchain,
            repositories=local_repos,
            main_repo=main_repo,
            binary_repo=binary_repo,
            extra_lua="\n".join(lua),
        )
        git_shim.install(config, sandbox.env, git_cutoff)
        if pins:
            # The project's own build runs a fresh xmake of its own; the versions
            # travel to it through the recipe (see plugins/upstream.py).
            sandbox.env["BUILDCACHE_PINS"] = ",".join(
                f"{name}={version}" for name, version in sorted(pins.items())
            )
            util.log(
                "pinned for the project's own build: "
                + ", ".join(f"{name}={version}" for name, version in sorted(pins.items()))
            )

        # Some steps are known to be flaky (generating the bedrock runtime data can
        # fail and succeed on the next attempt), so both install steps are retried
        # instead of failing the job. The sandbox package cache is kept between
        # attempts (it lives in the sandbox root), so a retry resumes rather than
        # restarting.
        util.log_step(
            "configure (today's dependency recipes)" if use_today
            else "configure (the era's recipes)"
        )
        for attempt in range(1, attempts + 1):
            result = sandbox.configure(
                toolchain, vs_toolset, plat, arch, stream=True, check=False
            )
            if result.returncode == 0:
                break
            util.warn(f"configure failed (attempt {attempt}/{attempts})")
            xmake_env.dump_failed_logs(config, sandbox)
            if attempt == attempts:
                last = "configure failed"
            else:
                _retry_failed_packages(config, sandbox, attempts)
        else:
            continue

        util.log_step("install closure")
        # No explicit package argument: the sandbox project already declares the
        # require (including configs), which keeps the resolved buildid identical.
        for attempt in range(1, attempts + 1):
            util.rmtree(sandbox.export_dir)
            util.ensure_dir(sandbox.export_dir)
            result = sandbox.xmake(
                ["require", "-y", "--build", "--export", f"--packagedir={sandbox.export_dir}"],
                stream=True,
                check=False,
            )
            if result.returncode == 0:
                break
            util.warn(f"install closure failed (attempt {attempt}/{attempts})")
            xmake_env.dump_failed_logs(config, sandbox)
            if attempt == attempts:
                last = "install closure failed"
            else:
                _retry_failed_packages(config, sandbox, attempts)
        else:
            continue
        return sandbox, prepared, recipe_commits, use_today, repo_order

    util.die(f"could not build {require}: {last}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plugin", default="ll")
    parser.add_argument("--version", required=True)
    parser.add_argument("--entry", default="", help="build entry id (e.g. server/client)")
    parser.add_argument("--commit", default="", help="upstream commit (defaults to the recipe listing)")
    parser.add_argument("--profile", default="", help="profile from the config (platform/toolset overrides)")
    parser.add_argument("--toolchain", default="", help="override the detected toolchain")
    parser.add_argument("--vs-toolset", default="", help="pin the MSVC toolset used for the build")
    parser.add_argument("--xmake-version", default="", help="override the xmake version")
    parser.add_argument("--dry-run", action="store_true", help="do everything except upload/push")
    parser.add_argument("--keep-sandbox", action="store_true")
    parser.add_argument("--proxy", default=None)
    parser.add_argument("--results", default="results", help="directory for build-result JSON files")
    parser.add_argument(
        "--dependency-results",
        default="",
        help="directory of build results to reuse: the SDK stage's output, when this "
        "job depends on a package another job in the same round just published",
    )
    parser.add_argument("--shared-cache", default=".work/pkgcache", help="reused xmake package cache")
    return parser.parse_args()


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _own_recipe_dir(prepared: dict, package: str):
    """The project's own recipe inside the snapshots this plan builds from."""
    for _name, (directory, _sha) in (prepared or {}).items():
        candidate = Path(directory) / "packages" / package[0].lower() / package
        if (candidate / "xmake.lua").is_file():
            return candidate
    return None


def published_manifests(config: dict, ctx, results_dir: str = "") -> Path:
    """A local repository of every artifact this cache has already published.

    xmake resolves a precompiled artifact by looking for
    ``packages/<letter>/<package>/<version>/manifest.txt`` in *every* repository it
    has loaded (``artifacts_manifest()``), so a sandbox that carries the recipe
    snapshots only never sees ours -- and then compiles the whole closure from
    source, once per project, every round. LeviLamina is the dependency every
    plugin shares, so the cost lands on each of them.

    The manifests are generated from the ledger, i.e. from the same data
    ``finalize`` publishes, and handed over as xmake's *binary* repository. That
    is also what keeps the official precompiled mirror out of the picture: one
    repository, and it is ours.

    A ledger that cannot be read (a network hiccup) leaves an empty repository
    behind, which is the behaviour this used to have: everything compiles.

    Only entries of the *current* revision are offered: a bumped revision says
    every earlier build is stale, and handing xmake the older artifact would
    quietly undo that.

    ``results_dir`` adds the artifacts a *job of this round* has just published.
    The ledger cannot know about them yet -- ``finalize`` writes it at the end --
    and a job cannot see another job's files, but the workflow hands its build
    results over, and those carry exactly what a manifest needs.

    Both are reconciled against the releases first: two jobs share their closure,
    so an asset can be uploaded twice in one round, and a manifest that names the
    upload that lost fails every consumer with "unmatched checksum" -- xmake does
    not fall back to compiling when a checksum disagrees, it stops.
    """
    directory = util.sandbox_root(config) / "binary-repo"
    util.rmtree(directory)
    digests = None
    try:
        from lib import verify

        cache_cfg = config["repos"]["cache"]
        digests = verify.published_digests(config, cache_cfg["owner"], cache_cfg["repo"])
    except Exception as error:  # noqa: BLE001 - the recorded digests still work
        util.warn(f"cannot read the published digests, using the recorded ones: {error}")
    try:
        ctx.ensure_index()
        written = manifest_lib.write_repo(
            ctx.index_root,
            directory,
            revision=int(config.get("revision", 1)),
            digests=digests,
        )
    except Exception as error:  # noqa: BLE001 - the build goes on without reuse
        util.warn(f"cannot read the ledger, nothing precompiled will be reused: {error}")
        util.ensure_dir(directory)
        return directory
    if results_dir:
        results = [
            util.read_json(path, None)
            for path in sorted(Path(results_dir).glob("build-result-*.json"))
        ]
        fresh = manifest_lib.write_results(results, directory, digests=digests)
        if fresh:
            total_fresh = sum(len(versions) for versions in fresh.values())
            util.log(
                f"this round published {total_fresh} package version(s) that this "
                f"build can already reuse"
            )
    # It has to exist even when nothing was written: xmake takes an existing
    # directory as a repository and anything else as a URL, and then tries to
    # *clone* it ("execv(git clone ... binary-repo) failed") for every package.
    util.ensure_dir(directory)
    total = sum(len(versions) for versions in written.values())
    util.log(
        f"precompiled artifacts on offer: {total} package version(s) "
        f"in {len(written)} package(s)"
    )
    return directory


def main() -> int:
    args = parse_args()
    config = util.load_config()
    proxy = args.proxy or util.proxy_url()
    ctx = plugins.Context(config, proxy=proxy)
    plugin = next((item for item in plugins.load_all(config) if item.name == args.plugin), None)
    if plugin is None:
        util.die(f"unknown plugin: {args.plugin}")

    entries = plugin.call("list_versions", ctx) or []
    entry = next((item for item in entries if item["version"] == args.version), None)
    if entry is None and not args.commit:
        util.die(f"version {args.version} is not tracked by the recipe repository")
    if entry is None:
        # With the commit given, a version the plugin keeps out of its *queue* can
        # still be built -- which is how a probe asks "would this one work now?"
        # about something the rounds skip on purpose.
        entry = {"version": args.version, "commit": args.commit}
    commit = args.commit or entry["commit"]

    util.log_step(f"{plugin.name} {args.version} ({commit[:12]})")

    platform_cfg = config["platform"]
    plat, arch = platform_cfg["plat"], platform_cfg["arch"]
    # An entry may pin its own toolchain (that is how a plugin declares extra
    # build dimensions); otherwise the plugin's default applies.
    toolchain = (
        args.toolchain
        or entry.get("toolchain")
        or plugin.call("toolchain", ctx, args.version, commit)
        or "msvc"
    )
    upstream_toolchain = plugin.call("native_toolchain", ctx, args.version, commit) or toolchain
    util.log(f"toolchains: cache={toolchain}  upstream={upstream_toolchain}")
    vs_toolset = xmake_env.detect_vs_toolset(config, args.vs_toolset or None)
    xmake_version = (
        args.xmake_version or plugin.call("xmake_version", ctx, args.version, commit)
    )
    cutoff = plugin.call("git_cutoff", ctx, args.version, commit) or ""
    entries = plugin.entries(ctx, args.version, commit)
    entry = next((item for item in entries if item["id"] == args.entry), entries[0])
    configs = entry.get("configs") or {}
    alias_profiles = [
        found["id"] for found in (plugin.call("toolchain_aliases", ctx, args.version, commit) or [])
    ]

    skip, reason = plugin.call("should_skip", ctx, args.version, commit) or (False, "")
    if skip:
        util.log(f"skip {args.version}: {reason}")
        return 0
    own_package = plugin.call("metadata")["package"]
    # Packages are built and published under their own names: the repository we
    # publish holds manifests only, so a consumer resolving that name finds our
    # archive without a copy of the recipe being involved.
    require = f"{own_package} {args.version}"
    util.log("entry: " + entry["id"] + "  configs: " + str(configs))

    cutoff_for_recipes = plugin.call("recipe_cutoff", ctx, args.version, commit)
    changed = sources.sources_changed(config, ctx, cutoff_for_recipes)
    plans = [(True, args.version)] if not changed else [(True, args.version), (False, args.version)]
    # What the cache has already published, handed to xmake as its binary
    # repository: a dependency then installs instead of being compiled again.
    binary_repo = published_manifests(config, ctx, args.dependency_results)
    sandbox, prepared, recipe_commits, used_today, repo_order = build_once(
        config, ctx, plugin, args,
        plat=plat, arch=arch, toolchain=toolchain, vs_toolset=vs_toolset,
        xmake_version=xmake_version, commit=commit, require=require, configs=configs,
        entry=entry, proxy=proxy,
        recipe_cutoff=cutoff_for_recipes, git_cutoff=cutoff,
        binary_repo=binary_repo, plans=plans,
    )

    util.log_step("publish artifacts")
    results = publish(
        config, args, ctx, sandbox, plat, arch, toolchain, vs_toolset, xmake_version,
        commit, cutoff, recipe_commits, entry, configs, require.partition(" ")[0],
        plugin_name=plugin.name, upstream_toolchain=upstream_toolchain, plugin=plugin,
        repo_order=repo_order,
        alias_profiles=alias_profiles,
        dry_run=args.dry_run,
    )
    # The pass above took today's recipes, so the dependencies already sit at the
    # addresses a consumer asks for -- nothing left to build. It only falls back to
    # the era's recipes (where the era's xmake cannot read today's), and then the
    # dependencies have to be built once more, the consumer's way, with a current
    # xmake.
    deps = (
        []
        if used_today
        else build_dependency_pass(
            config, ctx, plugin, args, plat, arch, toolchain, vs_toolset,
            commit=commit, dry_run=args.dry_run,
            specs=plugin.call("dependency_specs", ctx, args.version, commit) or [],
            binary_repo=binary_repo,
        )
    )
    if deps:
        results["artifacts"].extend(deps)
    out = Path(args.results)
    util.ensure_dir(out)
    result_file = out / f"build-result-{plugin.name}-{args.version}-{entry['id']}.json"
    util.write_json(result_file, results)
    util.log(f"wrote {result_file} ({len(results['artifacts'])} artifact(s))")

    if not args.keep_sandbox:
        # Keep the xmake package cache (sandbox root) for the next run and only
        # drop the throwaway parts.
        util.rmtree(sandbox.project_dir)
        util.rmtree(sandbox.export_dir)
        util.rmtree(sandbox.directory / "dist")
    return 0


def build_dependency_pass(config, ctx, plugin, args, plat, arch, toolchain, vs_toolset,
                          *, commit, dry_run, specs, binary_repo=None) -> list[dict]:
    """Publish the dependencies at the addresses a *consumer* resolves.

    The era build uses the recipes of the version's own date. That is what the
    project's sources need, but not what a consumer resolves: it resolves today's
    recipes, so a dependency built against an older sibling (cpr against libcurl
    8.11 instead of 8.21) is rejected by xmake's compatibility check and compiled
    from source on every machine that has not seen it yet -- the one thing a
    prebuilt cache exists to prevent.

    Same specs, current sources, current xmake, and only the dependencies: the
    project's own sources stay in their era. It is an improvement, never a
    requirement -- anything that goes wrong is reported and the round keeps what
    the era build produced.

    A dry run builds the pass too (that is how its viability is checked without a
    CI round) and publishes nothing, like the pass above it.
    """
    if not specs:
        return []
    try:
        prepared = sources.prepare(
            config, ctx, plugin, cutoff=None, label=f"{args.version}-deps"
        )
        repos_here = [(name, directory) for name, (directory, _sha) in prepared.items()]
        main_repo = dict(repos_here).get("xmake-repo")
        local_repos = [(name, directory) for name, directory in repos_here if name != "xmake-repo"]
        newest = xmake_env.newest_xmake(config)
        util.log_step(f"dependency pass ({len(specs)} spec(s), xmake {newest})")
        sandbox = xmake_env.create_sandbox(
            config,
            name=_slug(f"{plugin.name}-{args.version}-deps"),
            xmake_version=newest,
            proxy=ctx.proxy,
            toolchain=None if toolchain == "msvc" else toolchain,
            repositories=local_repos,
            main_repo=main_repo,
            binary_repo=binary_repo or published_manifests(config, ctx),
            extra_lua="\n".join(f'add_requires("{spec}")' for spec in specs),
        )
        result = sandbox.configure(toolchain, vs_toolset, plat, arch, stream=True, check=False)
        if result.returncode == 0:
            util.rmtree(sandbox.export_dir)
            util.ensure_dir(sandbox.export_dir)
            result = sandbox.xmake(
                ["require", "-y", "--build", "--export", f"--packagedir={sandbox.export_dir}"],
                stream=True,
                check=False,
            )
        if result.returncode != 0:
            util.warn("the dependency pass could not build; the era artifacts stand")
            return []
        published = publish(
            config, args, ctx, sandbox, plat, arch, toolchain, vs_toolset, newest,
            commit, "", {}, {"id": "default", "configs": {}}, {}, "",
            plugin_name=plugin.name, plugin=plugin, repo_order=repos_here,
            dry_run=dry_run,
        )
        return published["artifacts"]
    except Exception as error:  # noqa: BLE001 - a missing extra is not a failed round
        util.warn(f"the dependency pass failed: {error}")
        return []


def publish(config, args, ctx, sandbox, plat, arch, toolchain, vs_toolset, xmake_version,
            commit, cutoff, recipe_commits, entry, configs, package_name,
            plugin_name="", upstream_toolchain="", plugin=None, repo_order=None,
            alias_profiles=None, dry_run=False, force=False) -> dict:
    owner = config["repos"]["cache"]["owner"]
    repo = config["repos"]["cache"]["repo"]
    max_assets = int(config["release"]["maxAssetsPerRelease"])
    target_branch = config["release"].get("targetBranch")
    if target_branch and not dry_run:
        gitops.ensure_orphan_branch(config, owner, repo, target_branch, proxy=ctx.proxy)
    skip = {name.lower() for name in config.get("recipes", {}).get("skipPackages", [])}
    dist = util.ensure_dir(sandbox.directory / "dist")

    if not dry_run:
        # Reuse needs the ledger. A ledger that cannot be read (a network hiccup)
        # must not fail the build: packing everything again is the old behaviour.
        try:
            ctx.ensure_index()
        except Exception as error:  # noqa: BLE001 - the build goes on without reuse
            util.warn(f"cannot read the ledger, every artifact will be packed: {error}")

    artifacts = []
    for letter_dir in sorted(Path(sandbox.export_dir).iterdir()):
        if not letter_dir.is_dir():
            continue
        for package_dir in sorted(letter_dir.iterdir()):
            if not package_dir.is_dir():
                continue
            name = package_dir.name
            if name in skip:
                util.log(f"skip configured tool package: {name}")
                continue
            # Packages keep their own names: the repository we publish holds
            # manifests only, so a consumer resolving that name finds our archive
            # (xmake looks the manifest up by the resolved name, in *every*
            # repository) without carrying a copy of the recipe.
            for version_dir in sorted(package_dir.iterdir()):
                if not version_dir.is_dir():
                    continue
                version = version_dir.name
                for hash_dir in sorted(version_dir.iterdir()):
                    if not hash_dir.is_dir():
                        continue
                    artifact = _publish_one(
                        config, ctx, sandbox, dist, owner, repo, max_assets, plugin,
                        name, version, hash_dir, plat, arch,
                        # The manifest's toolset field is compared against a
                        # consumer's VCToolsVersion, so it records the toolset the
                        # build really used.
                        vs_toolset,
                        repo_order=repo_order or [], commit=commit, toolchain=toolchain,
                        root_package=package_name,
                        target_branch=target_branch, dry_run=dry_run, force=force,
                    )
                    if artifact:
                        artifacts.append(artifact)

    return {
        "plugin": plugin_name,
        "package": package_name,
        "version": args.version,
        "entry": entry["id"],
        "configs": configs,
        "revision": int(config.get("revision", 1)),
        "upstream_commit": commit,
        "xmake_version": xmake_version,
        "toolchain": toolchain,
        "upstream_toolchain": upstream_toolchain,
        "alias_profiles": alias_profiles or [],
        "vs_toolset": vs_toolset,
        "recipe_commits": recipe_commits,
        "platform": {"plat": plat, "arch": arch, "mode": config["platform"]["mode"]},
        "built_at": now(),
        "artifacts": artifacts,
    }


def dependency_pins(
    config: dict, ctx, sources: dict[str, Path], extra: list[dict], *, cutoff: str | None
) -> dict[str, str]:
    """The version to use for each SDK the project's own file leaves open.

    A require *without* a version resolves to the newest SDK there is, and one the
    recipe has since dropped has no answer at all: for a package we publish, the
    version of the project's own era is what its sources were written against (see
    ``plan.era_version``). ``bsci`` 0.4.0 asks for ``levilamina`` and was then
    compiled against 26.51.6; its era's own newest is 26.20.0.

    It is not only our own packages: a rule package such as ``levibuildscript``
    resolves to the newest one too, and that is written for the newest xmake --
    while a project of an older era builds with the xmake of its own. The mismatch
    surfaces as a Lua error *inside* the build (``attempt to index a nil value
    (global 'co')``), which says nothing about a version at all.

    This decides the version twice -- for our sandbox's top-level require, and for
    the project's own build, which resolves it again in a fresh xmake of its own
    (the same map is handed to that build through the recipe, see
    ``plugins/upstream.py``).
    """
    pins: dict[str, str] = {}
    for item in extra:
        name = str(item.get("name") or "")
        if not name:
            continue
        if _declared_version(sources, name, item.get("version", "")) not in ("", None):
            continue
        version = plan.era_version(config, ctx, name, cutoff)
        if version:
            pins[name] = version
    return pins


def dependency_overrides(
    ctx, plugin, version: str, commit: str, extra: list[dict], pins: dict[str, str]
) -> dict[str, str]:
    """The SDK versions the whole resolve tree is forced to, and why.

    A recipe declares a *range* for the SDK it needs (`levilamina >=1.7.6 <1.8.0`),
    and xmake reads a prerelease as inside it: ``more-dimensions`` 0.12.1 then
    resolves to ``1.8.0-rc.2`` -- although its own file pins ``levilamina 1.7.6``,
    which is what its sources were written against. That SDK's generated headers do
    not even compile (``template <ThreadOwnerBehavior B = 0>`` over a scoped enum,
    under ``mc/util/ThreadOwner.h``), so the build dies inside a header nobody wrote
    a line about, and the version in the log is not the version anybody asked for.

    Which version the project's own build resolves is decided by the project's own
    file; what it *links* is the newest SDK installed in the sandbox, which is the
    recipe's unless the whole tree is told otherwise -- the project's own
    ``add_packages("levilamina")`` has no version to prefer. So the versions a
    project pins are collected here and handed to xmake as ``add_requireconfs``
    overrides (``build_once``), the one mechanism that rewrites a dependency's
    version for every package below the requirement.

    A version the project leaves *open* gets the era's answer, the same one
    :func:`dependency_pins` gives the project's own build: two answers for one
    dependency is exactly the mismatch this exists to remove. A dependency the
    recipe does not declare at all keeps its own place in this map: it is asked for
    directly (see :func:`extra_requires`), at a version already decided.
    """
    raw = plugin.call("extra_deps", ctx, version, commit) or []
    forced: dict[str, str] = {}
    for item in raw:
        name = str(item.get("name") or "")
        if not name:
            continue
        declared = str(item.get("version") or "")
        if declared and any(char in declared for char in "~^><*|"):
            # A range of its own ("1.7.x", ">=1.7.0 <=1.7.5") is not a version to
            # check out, and it is a deliberate statement: leave it to the two
            # resolvers, which agree on it anyway.
            util.warn(
                f"{name}: the project's own file names {declared!r}, which is a range; "
                f"its build keeps whatever that range resolves to"
            )
            continue
        chosen = declared or pins.get(name, "")
        if not chosen:
            continue
        forced[name] = chosen
    return forced


def extra_requires(
    config: dict, ctx, repos, extra: list[dict], *, pinned: dict[str, str] | None = None
) -> list[str]:
    """``add_requires`` lines for the dependencies only the project file names.

    A consumer resolving the project gets them (they are named by the project's
    own xmake.lua), so this build has to produce them as well: otherwise every
    consumer compiles them from source, which is the one thing a prebuilt cache
    exists to prevent. They are asked for at the top level (not as a dependency of
    the project) because that is what makes ``require --export`` export them.

    ``repos`` is in xmake's search order, the same one the recipe lookup uses.

    A dependency the project names *without* a version would resolve to today's
    newest, which is not what a project of its age was ever built against: for the
    packages we publish ourselves, the version is taken from the recipe as it was
    at the version's own date instead (see ``plan.era_version``).
    """
    if not extra:
        return []
    sources = {name: Path(directory) for name, directory in repos}
    pinned = pinned or {}
    lines = []
    for item in extra:
        version = _declared_version(sources, item["name"], item.get("version", ""))
        if version is None:
            # The recipe dropped that version: the package's own era is the only
            # answer that is not a guess (see dependency_pins).
            version = pinned.get(item["name"])
            if version is None:
                continue
        elif not version:
            version = pinned.get(item["name"], "")
        spec = f'{item["name"]} {version}'.strip()
        lines.append(f'add_requires("{spec}")')
    return lines


def _declared_version(sources: dict[str, Path], name: str, wanted: str) -> str | None:
    """The version to ask for, "" to let xmake choose, or ``None`` to ask for nothing.

    A version the recipe spells with a leading ``v`` is asked for that way, and a
    range (``~1.6.2``) is left to xmake. A version the recipe no longer declares
    means *nothing* is asked for -- deliberately not the newest one: a project
    pinning an SDK that old is written against its own, and swapping in a current
    SDK compiles it against something it was never built for. It then fails
    somewhere further along, or, worse, succeeds and produces an artifact that
    links a version its own sources never saw.
    """
    if not wanted:
        return ""
    if any(char in wanted for char in "~^><*|"):
        return wanted
    available = _recipe_versions(_find_recipe_dir(sources, name))
    if wanted in available:
        return wanted
    if f"v{wanted}" in available:
        return f"v{wanted}"
    util.warn(
        f"recipe for '{name}' no longer declares {wanted}; the project's own pin is "
        f"dropped rather than replaced by a newer SDK"
    )
    return None


def _find_recipe_dir(sources: dict[str, Path], name: str) -> Path | None:
    for directory in sources.values():
        candidate = Path(directory) / "packages" / name[0].lower() / name.lower()
        if (candidate / "xmake.lua").is_file():
            return candidate
    return None


def _recipe_versions(recipe_dir: Path | None) -> set[str]:
    if recipe_dir is None:
        return set()
    versions: set[str] = set()
    text = util.read_text(Path(recipe_dir) / "xmake.lua")
    for match in re.finditer(r'add_versions\(\s*"([^"]+)"', text):
        versions.add(_strip_url_alias(match.group(1)))
    for match in re.finditer(r'add_versionfiles\(\s*"([^"]+)"', text):
        listing = Path(recipe_dir) / match.group(1)
        if listing.is_file():
            for line in util.read_text(listing).splitlines():
                parts = line.split()
                if parts:
                    versions.add(_strip_url_alias(parts[0]))
    return versions


def _strip_url_alias(version: str) -> str:
    """``release:v1.15.5`` / ``git:v1.6.3`` pick a URL alias, not a version."""
    _prefix, sep, rest = version.partition(":")
    if sep and rest and "//" not in version:
        return rest
    return version


def container_for(config: dict, package: str) -> str:
    """Container for one package; ``7z`` is the default because xmake unpacks it."""
    artifacts_cfg = config.get("artifacts") or {}
    override = (artifacts_cfg.get("containers") or {}).get(package)
    return override or artifacts_cfg.get("container") or packer.default_container()


#: What a compiler leaves behind in an install directory.
_BUILD_OUTPUT_SUFFIXES = (
    ".lib", ".exp", ".dll", ".exe", ".pdb", ".obj", ".ilk", ".res",
    ".a", ".o", ".so", ".dylib",
)


def has_build_output(hash_dir: Path) -> bool:
    """Did this install produce anything a compiler built?"""
    for path in Path(hash_dir).rglob("*"):
        if path.is_file() and path.suffix.lower() in _BUILD_OUTPUT_SUFFIXES:
            return True
    return False


def should_publish(name: str, root_package: str, hash_dir: Path) -> bool:
    """Is this package worth an artifact in the cache?

    The project's own package always is: that is what a consumer installs, so a
    release is the whole point. A *dependency* that carries nothing compiled is
    not worth one -- installing it is a download plus a copy (a header-only
    library, a data package), which a consumer can do as cheaply as fetching an
    archive, and its hash does not even depend on the toolchain that built the
    project around it.
    """
    if name == root_package:
        return True
    return has_build_output(hash_dir)


def artifact_name(name: str, buildhash: str, toolset: str, suffix: str) -> str:
    """The default asset name: package, build hash and the MSVC generation.

    The hash covers plat/arch/toolchain/configs and the source revision, but not the
    generation of the compiler -- and the same sources really do get built with two
    of them (see :func:`cached_artifact`). Two archives, two names.
    """
    generation = "".join(ch for ch in str(toolset or "").split(".")[0:2] if ch.isdigit()) or "any"
    return f"{name}-{buildhash}-vc{generation}.{suffix}"


def cached_artifact(
    ctx, package: str, version: str, buildhash: str, revision: int, toolset: str = ""
) -> dict | None:
    """The published artifact for this exact build, if the ledger still holds one.

    The build hash *is* the identity of a build -- same hash means the same
    sources, configs and toolchain, so the archive is the one already published.
    A round that shares a dependency with an earlier one therefore reuses it
    instead of packing a second archive under the same name (which would leave
    the published checksum pointing at whichever upload happened to be last).

    The record has to come from the current ``revision``: that number is how a
    change in this framework -- a repair, a different packing -- says "everything
    built before me is stale".

    The MSVC *generation* is not part of the hash, but it does decide whether an
    artifact can be linked at all (xmake keeps only artifacts whose toolset is not
    newer than the consumer's). A version whose artifact was built with another
    generation is therefore not "already published": rebuilding it on the era's own
    image is the point, and its archive has to be addressable next to the other one.
    """
    from lib import index_store

    def generation(value) -> str:
        return ".".join(str(value or "").split(".")[:2])

    meta = index_store.version_meta(ctx.index_root, package, version)
    for entry in index_store.entries(meta).values():
        if int(entry.get("revision") or 0) != int(revision):
            continue
        for artifact in entry.get("artifacts") or []:
            if artifact.get("buildhash") == buildhash:
                if toolset and generation(artifact.get("toolset")) != generation(toolset):
                    # The same sources built with another MSVC generation: a
                    # different archive, and one that may not be linkable here.
                    continue
                return artifact
    return None


def published_buildhash(ctx, package: str, version: str, buildhash: str) -> bool:
    """Whether the ledger lists this build at *any* revision.

    Such a build already owns its asset name, and the bytes under it belong to an
    older revision of this framework. Replacing them is exactly what a revision
    bump asks for -- and the one case where an upload may clobber.
    """
    from lib import index_store

    meta = index_store.version_meta(ctx.index_root, package, version)
    for entry in index_store.entries(meta).values():
        for artifact in entry.get("artifacts") or []:
            if artifact.get("buildhash") == buildhash:
                return True
    return False


def _retry_failed_packages(config: dict, sandbox, attempts: int) -> None:
    """Report what failed before the step is retried as a whole.

    It used to rebuild every failed package on its own (``xmake require --build
    <name> <version>``), which looks cheaper and is not: with a spec on the command
    line xmake resolves a git source by *tag* instead of the commit the recipe
    pins, so for a package whose tags are named differently ("v0.13.0" upstream,
    "0.13.0" in the recipe) every one of those retries died in a clone -- hiding the
    error that actually failed the build, which is the whole point of retrying.
    The step itself is retried next, and xmake skips what is already installed, so
    that retry only rebuilds what failed.
    """
    failed = xmake_env.failed_packages(sandbox)
    if not failed:
        return
    util.log(
        f"{len(failed)} package(s) failed, retrying the step: "
        + ", ".join(f"{name} {version}" for name, version in failed)
    )
    # The reason is in the package's own install log, which lives inside the
    # sandbox: print it per attempt, not only at the end.
    xmake_env.dump_failed_logs(config, sandbox)


def _publish_one(config, ctx, sandbox, dist, owner, repo, max_assets, plugin,
                 name, version, hash_dir, plat, arch, toolset,
                 repo_order=None, commit="", toolchain="msvc", root_package="",
                 target_branch=None, dry_run=False, force=False) -> dict | None:
    if not should_publish(name, root_package, hash_dir):
        # A dependency with nothing compiled in it: an archive would save a
        # consumer nothing, so it does not get one.
        util.log(f"skip {name} {version}: nothing was compiled, nothing to precompile")
        return None
    replace = bool(force)
    if not dry_run and not force:
        reused = cached_artifact(
            ctx, name, version, hash_dir.name, int(config.get("revision", 1)), toolset or ""
        )
        if reused:
            util.log(f"reuse {name} {version} ({hash_dir.name}): already published")
            return reused
        # The name is already taken by an older revision of this same build: the
        # bump is what asks for those bytes to be replaced. A build the ledger has
        # never seen must not clobber whoever publishes it first in this round.
        replace = published_buildhash(ctx, name, version, hash_dir.name)
    container = container_for(config, name)
    suffix = "tar.zst" if container == "tar.zst" else "7z"
    # The build hash encodes plat/arch/toolchain/configs and the source revision but
    # *not* the MSVC generation, and the same sources do get built with two
    # generations (an era built by the image of its own day, a version built before
    # that policy existed by windows-latest). The generation is therefore part of the
    # name: two archives, both linkable by whoever can use their toolset, instead of
    # one name whose bytes depend on which upload happened to be last.
    asset = None
    if plugin is not None:
        asset = plugin.call(
            "asset_name", ctx, name, version, toolset, hash_dir.name, container
        )
    asset = asset or artifact_name(name, hash_dir.name, toolset or "", suffix)
    archive = dist / asset
    util.log(f"pack {name} {version} ({hash_dir.name})")
    # xmake strips one level while extracting precompiled artifacts, so the
    # archive mirrors the package cache layout including the first-letter dir.
    store_as = f"{name[0].lower()}/{name}/{version}/{hash_dir.name}"
    packer.pack(hash_dir, archive, store_as=store_as, container=container)
    digest = util.sha256_file(archive)
    size = archive.stat().st_size
    key = manifest_lib.build_id(plat, arch, toolset, hash_dir.name)
    # The same artifact is reachable for other consumer toolchains; the plugin
    # decides which ones, the hash is recomputed offline (never rebuilt).
    derived: list[dict] = []
    if plugin is not None and not dry_run:
        # The plugin decides the addresses of *its* package; a dependency's
        # addresses follow from its own build (see aliases.closure_profiles).
        profiles = None if name == root_package else aliases.closure_profiles(toolchain)
        derived = aliases.for_artifact(
            ctx, plugin, sandbox, list(repo_order or []), plat, arch,
            package=name, version=version, commit=commit, hash_dir=hash_dir,
            toolset=toolset, profiles=profiles,
        )
    url = ""
    release = ""
    if dry_run:
        release = f"{name}_dry_run"
        url = f"file://{archive.as_posix()}"
    else:
        # A shard and its tag are named after the package, because the tag is an
        # address: the version, the entry and the hash all live in the index and
        # the release body instead of in a longer name.
        release = gh.assign_release(name, [asset], owner, repo, max_assets, config=config)
        gh.ensure_release(owner, repo, release, target=target_branch, config=config)
        published = gh.upload_asset(
            owner, repo, release, archive, config=config, replace=replace
        )
        if published.get("sha256") and published["sha256"] != digest:
            # Two builds of one identity are not byte identical (another era's
            # xmake, the other half of this round). Consumers download whatever
            # the shard serves, so that is what the ledger has to name.
            util.log(
                f"{asset}: this build differs from the published archive "
                f"({published['sha256'][:8]}); recording the published one"
            )
            digest = published["sha256"]
            size = int(published.get("size") or size)
        url = gh.asset_url(owner, repo, release, asset)
    return {
        "package": name,
        "version": version,
        "buildhash": hash_dir.name,
        "key": key,
        "toolset": toolset,
        "release": release,
        "asset": asset,
        "url": url,
        "sha256": digest,
        "size": size,
        "container": container,
        "aliases": derived,
    }


def _lua_table(mapping: dict) -> str:
    if not mapping:
        return "{}"
    body = ", ".join(f'{key} = {_lua_value(value)}' for key, value in mapping.items())
    return "{ " + body + " }"


def _lua_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return f'"{value}"'


def _slug(text: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in text)


if __name__ == "__main__":
    raise SystemExit(main())
