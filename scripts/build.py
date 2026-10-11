"""Build one SDK version end to end.

The whole dependency closure is installed by xmake itself into an isolated
sandbox, then exported, packed and published as precompiled artifacts.
"""

from __future__ import annotations

import argparse
import json
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
    packer,
    plugins,
    recipefix,
    repos,
    sources,
    util,
    xmake_env,
)


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
    parser.add_argument("--shared-cache", default=".work/pkgcache", help="reused xmake package cache")
    return parser.parse_args()


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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
    upstream_package = plugin.call("metadata")["package"]
    # Built with the era's own recipes, i.e. with the original package names;
    # renaming to our prefix happens at publish time. That keeps a build
    # internally consistent: era xmake + era recipes + era dependency pins.
    require = f"{upstream_package} {args.version}"
    util.log("entry: " + entry["id"] + "  configs: " + str(configs))

    cutoff_for_recipes = plugin.call("recipe_cutoff", ctx, args.version, commit)
    prepared = sources.prepare(
        config, ctx, plugin, cutoff=cutoff_for_recipes, label=args.version
    )
    recipe_commits = {name: commit_sha for name, (_dir, commit_sha) in prepared.items()}
    main_repo = prepared.get("xmake-repo", (None, ""))[0]
    local_repos = [
        (name, directory) for name, (directory, _sha) in prepared.items() if name != "xmake-repo"
    ]
    # xmake searches the project's own repositories before the main one, so the
    # recipe lookup for a package has to follow the same order.
    repo_order = local_repos + [("xmake-repo", main_repo)]

    # Keep the official precompiled mirror out of the way: the closure has to be
    # built from source with the era recipes, not fetched as someone else's artifact.
    empty_repo = util.ensure_dir(util.sandbox_root(config) / "empty-repo")

    requires = [f'add_requires("{require}", {{configs = {_lua_table(configs)}}})']
    requires += extra_requires(
        ctx, repo_order, plugin.call("extra_deps", ctx, args.version, commit) or []
    )
    sandbox = xmake_env.create_sandbox(
        config,
        name=_slug(f"{plugin.name}-{args.version}-{entry['id']}"),
        xmake_version=xmake_version,
        proxy=proxy,
        toolchain=None if toolchain == "msvc" else toolchain,
        repositories=local_repos,
        main_repo=main_repo,
        binary_repo=empty_repo,
        extra_lua="\n".join(requires),
    )
    git_shim.install(config, sandbox.env, cutoff)

    # Some steps are known to be flaky (generating the bedrock runtime data can
    # fail and succeed on the next attempt), so both install steps are retried
    # instead of failing the job. The sandbox package cache is kept between
    # attempts (it lives in the sandbox root), so a retry resumes rather than
    # restarting.
    attempts = int(config.get("xmake", {}).get("installRetries", 3))

    util.log_step("configure")
    # The configure step is where the dependency closure gets installed, so a
    # failure here has to be retried exactly like the export step below.
    for attempt in range(1, attempts + 1):
        result = sandbox.configure(
            toolchain, vs_toolset, plat, arch, stream=True, check=False
        )
        if result.returncode == 0:
            break
        util.warn(f"configure failed (attempt {attempt}/{attempts})")
        xmake_env.dump_failed_logs(config, sandbox)
        if attempt == attempts:
            util.die(f"configure failed after {attempts} attempt(s)")
        _retry_failed_packages(config, sandbox, attempts)

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
            util.die(f"install closure failed after {attempts} attempt(s)")
        _retry_failed_packages(config, sandbox, attempts)

    util.log_step("publish artifacts")
    results = publish(
        config, args, ctx, sandbox, plat, arch, toolchain, vs_toolset, xmake_version,
        commit, cutoff, recipe_commits, entry, configs, require.partition(" ")[0],
        plugin_name=plugin.name, upstream_toolchain=upstream_toolchain, plugin=plugin,
        repo_order=repo_order,
        alias_profiles=alias_profiles,
        dry_run=args.dry_run,
    )
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


def publish(config, args, ctx, sandbox, plat, arch, toolchain, vs_toolset, xmake_version,
            commit, cutoff, recipe_commits, entry, configs, package_name,
            plugin_name="", upstream_toolchain="", plugin=None, repo_order=None,
            alias_profiles=None, dry_run=False) -> dict:
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
            # manifests only, so a consumer resolving the upstream package finds
            # our archive (xmake looks the manifest up by the resolved name, in
            # every repository) without carrying a copy of the recipe.
            published_name = name
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
                        toolset_for_key(vs_toolset, name),
                        repo_order=repo_order or [], commit=commit, toolchain=toolchain,
                        root_package=package_name,
                        target_branch=target_branch, dry_run=dry_run,
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
        "entry": entry["id"],
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


def toolset_for_key(vs_toolset: str | None, package: str) -> str | None:
    """The manifest toolset field is compared against a consumer's VCToolsVersion."""
    return vs_toolset


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
                 target_branch=None, dry_run=False) -> dict | None:
    from lib import manifest as manifest_lib

    if not should_publish(name, root_package, hash_dir):
        # A dependency with nothing compiled in it: an archive would save a
        # consumer nothing, so it does not get one.
        util.log(f"skip {name} {version}: nothing was compiled, nothing to precompile")
        return None
    if not dry_run:
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
        # Shards and their tags are named after the *upstream* package: the
        # prefix only exists to keep published package names distinct, it has no
        # business showing up in release tags or titles.
        release = gh.assign_release(name, [asset], owner, repo, max_assets, config=config)
        gh.ensure_release(owner, repo, release, target=target_branch, config=config)
        gh.upload_asset(owner, repo, release, archive, config=config)
        url = gh.asset_url(owner, repo, release, asset)
    return {
        "package": name,
        "upstream_package": name,
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
