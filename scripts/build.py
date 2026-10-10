"""Build one SDK version end to end.

The whole dependency closure is installed by xmake itself into an isolated
sandbox, then exported, packed and published as precompiled artifacts.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib import (  # noqa: E402
    gh,
    git_shim,
    gitops,
    packer,
    plugins,
    recipefix,
    repos,
    util,
    vendoring,
    xmake_env,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plugin", default="ll")
    parser.add_argument("--version", required=True)
    parser.add_argument("--variant", default="", help="variant name (e.g. server/client)")
    parser.add_argument("--commit", default="", help="upstream commit (defaults to the recipe listing)")
    parser.add_argument("--profile", default="", help="profile from the config (platform/toolset overrides)")
    parser.add_argument("--toolchain", default="", help="override the detected toolchain")
    parser.add_argument("--vs-toolset", default="", help="pin the MSVC toolset used for the build")
    parser.add_argument("--xmake-version", default="", help="override the xmake version")
    parser.add_argument("--dry-run", action="store_true", help="do everything except upload/push")
    parser.add_argument("--no-vendor", action="store_true", help="reuse the recipes already vendored")
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
    if not args.no_vendor:
        marker = vendoring.vendor(config, ctx, plugin)
        util.log(f"recipes vendored (sources: {marker['sources']})")

    platform_cfg = config["platform"]
    plat, arch = platform_cfg["plat"], platform_cfg["arch"]
    toolchain = args.toolchain or plugin.call("toolchain", ctx, args.version, commit) or "msvc"
    upstream_toolchain = plugin.call("native_toolchain", ctx, args.version, commit) or toolchain
    util.log(f"toolchains: cache={toolchain}  upstream={upstream_toolchain}")
    vs_toolset = xmake_env.detect_vs_toolset(config, args.vs_toolset or None)
    xmake_version = (
        args.xmake_version or plugin.call("xmake_version", ctx, args.version, commit)
    )
    cutoff = plugin.call("git_cutoff", ctx, args.version, commit) or ""
    variants = plugin.call("variants", ctx, args.version, commit) or [
        {"name": "default", "configs": plugin.call("configs", ctx, args.version, commit) or {}}
    ]
    variant = next(
        (item for item in variants if item["name"] == args.variant), variants[0]
    )
    configs = variant.get("configs") or {}
    upstream_package = plugin.call("metadata")["package"]
    require = f"{ctx.package_name(upstream_package)} {args.version}"
    util.log(f"variant: {variant['name']}  configs: {configs}")

    snapshot_mode = str(config.get("recipes", {}).get("snapshotMode", "head"))
    recipe_commits: dict[str, str] = {}
    local_repos: list[tuple[str, Path]] = []
    main_repo = None
    build_env = util.subprocess_env(config, proxy=proxy)
    for source in config.get("recipes", {}).get("sources", []):
        mirror = ctx.mirror(source["name"], source["url"])
        wanted = plugin.call("recipe_cutoff", ctx, args.version, commit) if snapshot_mode == "version" else None
        if wanted:
            dest = util.ROOT / ".vendor" / f"{source['name']}@{args.version}"
        else:
            dest = util.ROOT / ".vendor" / f"{source['name']}@{repos.commit_at(mirror, env=build_env)[:12]}"
        recipe_commits[source["name"]] = repos.snapshot(
            mirror, dest, cutoff_iso=wanted, env=build_env
        )
        for note in recipefix.apply_fixups(config, source["name"], dest):
            util.log(note)
        for note in recipefix.normalize_tree(dest):
            util.log(note)
        if source["name"] == "xmake-repo":
            main_repo = dest
        elif source["name"] == "levimc-repo":
            local_repos.append((source["name"], dest))

    sandbox = xmake_env.create_sandbox(
        config,
        name=_slug(f"{plugin.name}-{args.version}-{variant['name']}"),
        xmake_version=xmake_version,
        proxy=proxy,
        toolchain=None if toolchain == "msvc" else toolchain,
        repositories=local_repos,
        main_repo=main_repo,
        binary_repo=Path(ctx.package_repo),
        extra_lua=(
            f'add_requires("{require}", {{configs = {_lua_table(configs)}}})'
        ),
    )
    git_shim.install(config, sandbox.env, cutoff)

    util.log_step("configure")
    sandbox.configure(toolchain, vs_toolset, plat, arch)

    util.log_step("install closure")
    # No explicit package argument: the sandbox project already declares the
    # require (including configs), which keeps the resolved buildid identical.
    # The sandbox package cache is kept between runs (it lives in the sandbox
    # root), so re-running reuses everything that is already built.
    util.rmtree(sandbox.export_dir)
    util.ensure_dir(sandbox.export_dir)
    sandbox.xmake(
        ["require", "-y", "--build", "--export", f"--packagedir={sandbox.export_dir}"],
        tail=30,
    )

    util.log_step("publish artifacts")
    results = publish(
        config, args, ctx, sandbox, plat, arch, toolchain, vs_toolset, xmake_version,
        commit, cutoff, recipe_commits, variant, configs, require.partition(" ")[0],
        plugin_name=plugin.name, upstream_toolchain=upstream_toolchain, plugin=plugin,
        dry_run=args.dry_run,
    )
    out = Path(args.results)
    util.ensure_dir(out)
    result_file = out / f"build-result-{plugin.name}-{args.version}-{variant['name']}.json"
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
            commit, cutoff, recipe_commits, variant, configs, package_name,
            plugin_name="", upstream_toolchain="", plugin=None, dry_run=False) -> dict:
    owner = config["repos"]["cache"]["owner"]
    repo = config["repos"]["cache"]["repo"]
    max_assets = int(config["release"]["maxAssetsPerRelease"])
    target_branch = config["release"].get("targetBranch")
    if target_branch and not dry_run:
        gitops.ensure_orphan_branch(config, owner, repo, target_branch, proxy=ctx.proxy)
    skip = {name.lower() for name in config.get("recipes", {}).get("skipPackages", [])}
    dist = util.ensure_dir(sandbox.directory / "dist")

    artifacts = []
    for letter_dir in sorted(Path(sandbox.export_dir).iterdir()):
        if not letter_dir.is_dir():
            continue
        for package_dir in sorted(letter_dir.iterdir()):
            if not package_dir.is_dir():
                continue
            name = package_dir.name
            if not name.startswith(ctx.prefix):
                util.log(f"skip non-prefixed package from the closure: {name}")
                continue
            if name in skip:
                util.log(f"skip configured tool package: {name}")
                continue
            for version_dir in sorted(package_dir.iterdir()):
                if not version_dir.is_dir():
                    continue
                version = version_dir.name
                for hash_dir in sorted(version_dir.iterdir()):
                    if not hash_dir.is_dir():
                        continue
                    artifacts.append(
                        _publish_one(
                            config, ctx, sandbox, dist, owner, repo, max_assets, plugin,
                            name, version, hash_dir, plat, arch, toolset_for_key(vs_toolset, name),
                            target_branch=target_branch, dry_run=dry_run,
                        )
                    )

    return {
        "plugin": plugin_name,
        "package": package_name,
        "version": args.version,
        "variant": variant["name"],
        "configs": configs,
        "upstream_commit": commit,
        "upstream_date": cutoff,
        "xmake_version": xmake_version,
        "toolchain": toolchain,
        "upstream_toolchain": upstream_toolchain,
        "vs_toolset": vs_toolset,
        "recipe_commits": recipe_commits,
        "prefix": ctx.prefix,
        "platform": {"plat": plat, "arch": arch, "mode": config["platform"]["mode"]},
        "built_at": now(),
        "artifacts": artifacts,
    }


def toolset_for_key(vs_toolset: str | None, package: str) -> str | None:
    """The manifest toolset field is compared against a consumer's VCToolsVersion."""
    return vs_toolset


def container_for(config: dict, package: str) -> str:
    """Container for one package; ``7z`` is the default because xmake unpacks it."""
    artifacts_cfg = config.get("artifacts") or {}
    override = (artifacts_cfg.get("containers") or {}).get(package)
    return override or artifacts_cfg.get("container") or packer.default_container()


def _publish_one(config, ctx, sandbox, dist, owner, repo, max_assets, plugin,
                 name, version, hash_dir, plat, arch, toolset, target_branch=None, dry_run=False) -> dict:
    from lib import manifest as manifest_lib

    container = container_for(config, name)
    suffix = "tar.zst" if container == "tar.zst" else "7z"
    # The build hash already encodes plat/arch/toolchain/configs and shards are
    # per package, so the asset name only needs to be unique inside its shard.
    # Everything a human needs lives in the release body instead.
    asset = None
    if plugin is not None:
        asset = plugin.call(
            "asset_name", ctx, name, version, toolset, hash_dir.name, container
        )
    asset = asset or f"{hash_dir.name}.{suffix}"
    archive = dist / asset
    util.log(f"pack {name} {version} ({hash_dir.name})")
    # xmake strips one level while extracting precompiled artifacts, so the
    # archive mirrors the package cache layout including the first-letter dir.
    store_as = f"{name[0].lower()}/{name}/{version}/{hash_dir.name}"
    packer.pack(hash_dir, archive, store_as=store_as, container=container)
    digest = util.sha256_file(archive)
    key = manifest_lib.build_id(plat, arch, toolset, hash_dir.name)
    url = ""
    release = ""
    if dry_run:
        release = f"{name}_dry_run"
        url = f"file://{archive.as_posix()}"
    else:
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
