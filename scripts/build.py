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
    sources,
    util,
    xmake_env,
)


def build_once(config, ctx, plugin, args, *, plat, arch, toolchain, vs_toolset,
               xmake_version, commit, require, configs, entry, proxy,
               recipe_cutoff: str, git_cutoff: str, binary_repo, plans: list[tuple[bool, str]]):
    """Build the closure in the first sandbox that configures and exports.

    A plan is ``(use_today, label)``. The first one takes the dependencies from
    today's recipes -- that is what a consumer resolves, so the artifacts land on
    addresses a consumer asks for -- and keeps the project's own recipe from its
    era, which is what pins the dependency versions and carries our repairs. It is
    the only plan when nothing moved in the recipe sources since the version was
    released, because then the era *is* today.

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
        recipe_commits = {name: sha for name, (_dir, sha) in prepared.items()}
        main_repo = prepared.get("xmake-repo", (None, ""))[0]
        local_repos = [
            (name, directory)
            for name, (directory, _sha) in prepared.items()
            if name != "xmake-repo"
        ]
        # xmake searches the project's own repositories before the main one, so the
        # recipe lookup for a package has to follow the same order.
        repo_order = local_repos + [("xmake-repo", main_repo)]
        requires = [f'add_requires("{require}", {{configs = {_lua_table(configs)}}})']
        # The project's own file may name a dependency its recipe already pins to
        # another version; asking for both would build a second SDK nobody uses.
        requires += extra_requires(
            ctx,
            repo_order,
            plan.extra_deps(
                plugin, ctx, args.version, commit,
                _own_recipe_dir(prepared, own),
            ),
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
            extra_lua="\n".join(requires),
        )
        git_shim.install(config, sandbox.env, git_cutoff)

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
    if entry is None:
        util.die(f"version {args.version} is not tracked by the recipe repository")
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
            plugin_name=plugin.name, plugin=plugin, repo_order=repos_here, force=True,
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


def extra_requires(ctx, repos, extra: list[dict]) -> list[str]:
    """``add_requires`` lines for the dependencies only the project file names.

    A consumer resolving the project gets them (they are named by the project's
    own xmake.lua), so this build has to produce them as well: otherwise every
    consumer compiles them from source, which is the one thing a prebuilt cache
    exists to prevent. They are asked for at the top level (not as a dependency of
    the project) because that is what makes ``require --export`` export them.

    ``repos`` is in xmake's search order, the same one the recipe lookup uses.
    """
    if not extra:
        return []
    sources = {name: Path(directory) for name, directory in repos}
    lines = []
    for item in extra:
        version = _declared_version(sources, item["name"], item.get("version", ""))
        spec = f'{item["name"]} {version}'.strip()
        lines.append(f'add_requires("{spec}")')
    return lines


def _declared_version(sources: dict[str, Path], name: str, wanted: str) -> str:
    """The version to ask for, or "" to let xmake pick the newest.

    A version the recipe spells with a leading ``v`` is asked for that way, and a
    range (``~1.6.2``) is left to xmake. A version the recipe no longer declares is
    not asked for at all: xmake resolves the newest one instead.
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
    util.warn(f"recipe for '{name}' has no version {wanted}; using the newest available")
    return ""


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


def cached_artifact(
    ctx, package: str, version: str, buildhash: str, revision: int
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
    """
    from lib import index_store

    meta = index_store.version_meta(ctx.index_root, package, version)
    for entry in index_store.entries(meta).values():
        if int(entry.get("revision") or 0) != int(revision):
            continue
        for artifact in entry.get("artifacts") or []:
            if artifact.get("buildhash") == buildhash:
                return artifact
    return None


def _retry_failed_packages(config: dict, sandbox, attempts: int) -> None:
    """Rebuild the packages that failed, one by one, before retrying the whole step."""
    failed = xmake_env.failed_packages(sandbox)
    if not failed:
        return
    util.log(
        f"retrying {len(failed)} failed package(s) on their own: "
        + ", ".join(f"{name} {version}" for name, version in failed)
    )
    for name, version in failed:
        for attempt in range(1, attempts + 1):
            # Say what is being retried and how it went: a silent retry loop is
            # indistinguishable from a hang in the log.
            util.log(f"== retry {name} {version} (attempt {attempt}/{attempts})")
            single = sandbox.xmake(
                ["require", "-y", "--build", f"{name} {version}"],
                stream=True,
                check=False,
            )
            if single.returncode == 0:
                util.log(f"== retry {name} {version} ok")
                break
            util.warn(f"{name} {version} failed (attempt {attempt}/{attempts})")
            # The reason is in the package's own install log, which lives inside
            # the sandbox: print it per failed attempt, not only at the end.
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
    if not dry_run and not force:
        reused = cached_artifact(
            ctx, name, version, hash_dir.name, int(config.get("revision", 1))
        )
        if reused:
            util.log(f"reuse {name} {version} ({hash_dir.name}): already published")
            return reused
    container = container_for(config, name)
    suffix = "tar.zst" if container == "tar.zst" else "7z"
    # The build hash already encodes plat/arch/toolchain/configs and the source
    # revision, and shards are per package, so the asset name only needs the name
    # plus the hash.
    asset = None
    if plugin is not None:
        asset = plugin.call(
            "asset_name", ctx, name, version, toolset, hash_dir.name, container
        )
    asset = asset or f"{name}-{hash_dir.name}.{suffix}"
    archive = dist / asset
    util.log(f"pack {name} {version} ({hash_dir.name})")
    # xmake strips one level while extracting precompiled artifacts, so the
    # archive mirrors the package cache layout including the first-letter dir.
    store_as = f"{name[0].lower()}/{name}/{version}/{hash_dir.name}"
    packer.pack(hash_dir, archive, store_as=store_as, container=container)
    digest = util.sha256_file(archive)
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
        gh.upload_asset(owner, repo, release, archive, config=config)
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
        "size": archive.stat().st_size,
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
