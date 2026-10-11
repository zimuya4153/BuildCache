"""Consumer-side check: does a fresh project download our artifact instead of building?

The test resolves a published package in a throwaway sandbox and asserts that

* xmake downloaded our release asset, and
* the package was installed from the artifact (no ``source/`` build directory),
* the expected headers/libraries are present in the install dir.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib import index_store, plugins, util, xmake_env  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--component", default="levilamina")
    parser.add_argument("--version", default="", help="defaults to the newest indexed version")
    parser.add_argument("--entry", default="server", help="variant to consume (e.g. server/client)")
    parser.add_argument(
        "--toolchain",
        default="auto",
        choices=["auto", "msvc", "clang-cl"],
        help="how the consumer *declares* its toolchain, which is part of the "
        "build hash: 'auto' declares nothing (windows defaults to msvc), 'msvc' "
        "and 'clang-cl' write set_toolchains() into the project file and must be "
        "served by a manifest alias. Never pass it as a command line --toolchain: "
        "xmake would hash the same but skip artifact selection entirely.",
    )
    parser.add_argument("--expect", default="", help="extra relative path that must exist")
    parser.add_argument("--keep-sandbox", action="store_true")
    parser.add_argument("--proxy", default=None)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = util.load_config()
    proxy = args.proxy or util.proxy_url()
    ctx = plugins.Context(config, proxy=proxy)
    ctx.ensure_index()

    meta = _pick_meta(ctx, args.component, args.version)
    if meta is None:
        util.die(f"no indexed build for {args.component} {args.version or '(latest)'}")
    version = str(meta["version"])
    variant_meta = index_store.entries(meta).get(args.entry)
    if variant_meta is None:
        util.die(
            f"{args.component} {version} has no '{args.entry}' variant "
            f"(available: {', '.join(sorted(index_store.entries(meta))) or 'none'})"
        )
    configs = variant_meta.get("configs") or {}
    toolset = meta.get("vs_toolset")
    toolchain = meta.get("toolchain") or "msvc"
    util.log_step(
        f"consume {args.component} {version} [{args.entry}] "
        f"(built with {toolchain}, toolset {toolset})"
    )

    main_snapshot = _snapshot(config, ctx, "xmake-repo", proxy)
    levimc_snapshot = _snapshot(config, ctx, "levimc-repo", proxy)
    published = _published_repo(config, ctx, proxy)
    util.rmtree(util.sandbox_root(config) / "selftest")
    sandbox = xmake_env.create_sandbox(
        config,
        name="selftest",
        xmake_version=str(meta.get("xmake_version") or "3.1.1"),
        proxy=proxy,
        toolchain=None if args.toolchain == "auto" else args.toolchain,
        repositories=[("llsdk", published), ("levimc-repo", levimc_snapshot)],
        main_repo=main_snapshot,
        binary_repo=published,
        extra_lua=(
            f'add_requires("{args.component} {version}", '
            f"{{configs = {_lua_table(configs)}}})"
        ),
    )
    plat, arch = config["platform"]["plat"], config["platform"]["arch"]
    # Deliberately no command line toolchain: the sandbox project file already
    # declares it, and xmake only selects precompiled artifacts when it checked
    # an msvc toolchain itself, which a `--toolchain=` argument prevents.
    result = sandbox.configure(None, toolset, plat, arch, capture=True, check=False)
    if result.returncode != 0:
        # A failing consumer run is the one case where the reason matters most,
        # and xmake only points at a log inside the sandbox.
        xmake_env.dump_failed_logs(config, sandbox)
    log = result.stdout or ""
    expected_assets = [item["asset"] for item in variant_meta.get("artifacts", [])]
    fetched = any(f"/{asset}" in log for asset in expected_assets)
    precompiled_ok = any("(precompiled)" in line and "ok" in line for line in log.splitlines())
    if not fetched:
        for line in log.splitlines():
            if "download" in line and "http" in line:
                util.log("  (downloaded) " + line.strip())

    installed = _find_install(sandbox, args.component, version)
    if installed is None:
        util.die("package was not installed at all")
    build_dirs = [
        item
        for item in installed.parent.iterdir()
        if item.name.startswith("source")
    ]
    problems = []
    if build_dirs:
        problems.append(
            "package was built from source instead of using the artifact "
            f"(found {', '.join(str(item.name) for item in build_dirs)})"
        )
    if not fetched:
        problems.append("no release asset was downloaded (artifact was never selected)")
    if fetched and not precompiled_ok:
        problems.append(
            "the artifact download started but the precompiled install failed "
            "(xmake fell back to a source build)"
        )
    expected = args.expect or _default_expectation(args.component)
    if expected and not (installed / expected).exists():
        problems.append(f"missing {expected} in {installed}")
    manifest = installed / "manifest.txt"
    if not manifest.is_file():
        problems.append(f"install dir has no manifest.txt: {installed}")

    util.log(f"installed at: {installed}")
    if problems:
        for line in log.splitlines():
            if any(word in line for word in ("precompiled", "manifest", "error", "failed")):
                util.log("  > " + line.strip())
        for problem in problems:
            util.warn(problem)
        if not args.keep_sandbox:
            util.rmtree(sandbox.directory)
        return 1
    util.log("PASS: artifact was consumed without compiling the package")
    if not args.keep_sandbox:
        util.rmtree(sandbox.directory)
    return 0


def _pick_meta(ctx, component: str, version: str) -> dict | None:
    metas = index_store.all_version_meta(ctx.index_root, component)
    if not metas:
        return None
    if version:
        return next((item for item in metas if str(item.get("version")) == version), None)
    return metas[0]


def _snapshot(config: dict, ctx, name: str, proxy: str | None) -> Path:
    from lib import sources as sources_module

    prepared = sources_module.prepare(
        config, ctx, only=name, env=util.subprocess_env(config, proxy=proxy)
    )
    return prepared[name][0]


def _published_repo(config: dict, ctx, proxy: str | None) -> Path:
    """Clone the published package repository the way a consumer would."""
    from lib import gitops

    package_cfg = config["repos"]["package"]
    url = f"https://github.com/{package_cfg['owner']}/{package_cfg['repo']}.git"
    checkout = util.ROOT / ".work" / "selftest-repo"
    gitops.ensure_checkout(config, url, checkout, branch=package_cfg.get("branch"), reset=True)
    return checkout


def _find_install(sandbox, component: str, version: str) -> Path | None:
    pattern = f"packages/{component[0]}/{component}/{version}/*"
    for candidate in sorted((sandbox.root_dir / ".xmake").glob(pattern)):
        if candidate.is_dir():
            return candidate
    return None


def _default_expectation(component: str) -> str:
    return {
        "levilamina": "include/ll/api/Global.h",
        "fmt": "include/fmt/format.h",
        "entt": "include/entt/entt.hpp",
    }.get(component, "")


def _lua_table(mapping: dict) -> str:
    if not mapping:
        return "{}"
    body = ", ".join(f'{key} = "{value}"' for key, value in mapping.items())
    return "{ " + body + " }"


if __name__ == "__main__":
    raise SystemExit(main())
