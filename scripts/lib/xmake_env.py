"""Isolated xmake sandboxes.

Every job gets its own ``XMAKE_ROOT`` plus a pinned xmake binary, and sees the
recipe repositories through ``XMAKE_MAIN_REPO`` / local-dir repositories checked
out at the snapshot commit for the version being built. Nothing leaks between
jobs or into the developer's own ``~/.xmake``.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from . import semver, util


@dataclass
class Sandbox:
    name: str
    directory: Path
    project_dir: Path
    root_dir: Path
    export_dir: Path
    env: dict = field(default_factory=dict)
    xmake_exe: Path | None = None
    timeout: float | None = 5400
    heartbeat: float = 60.0
    silent_timeout: float = 1800.0

    def xmake(
        self,
        args: list,
        cwd: Path | None = None,
        check: bool = True,
        tail: int = 0,
        capture: bool = False,
        stream: bool = False,
    ):
        if stream:
            return self._xmake_streaming(args, cwd=cwd, check=check)
        result = util.run(
            [str(self.xmake_exe)] + [str(a) for a in args],
            cwd=cwd or self.project_dir,
            env=self.env,
            capture=capture or tail > 0,
            check=check,
            timeout=self.timeout,
        )
        output = result.stdout or ""
        if capture and output:
            for line in output.splitlines():
                util.log("  | " + line)
        elif tail > 0 and output:
            for line in output.splitlines()[-tail:]:
                util.log("  | " + line)
        return result

    def _xmake_streaming(self, args: list, cwd: Path | None = None, check: bool = True):
        """Run xmake and print its output while it happens.

        Buffering the child's output (``capture=True``) makes a long package
        build look like a hang both locally and in CI, where a step that prints
        nothing for many minutes may be treated as dead.

        Some individual steps are silent for a long time by nature (cloning a
        big repository, a download without a TTY to render progress into). A
        heartbeat keeps such a run visibly alive, and a silence limit turns a
        genuinely stuck child into a failure instead of a run that never ends.
        """
        import queue
        import threading

        cmd = [str(self.xmake_exe)] + [str(a) for a in args]
        util.log("+ " + " ".join(cmd))
        process = subprocess.Popen(
            cmd,
            cwd=str(cwd or self.project_dir),
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        lines: "queue.Queue" = queue.Queue()

        def pump():
            for line in process.stdout or []:
                lines.put(line)
            lines.put(None)

        threading.Thread(target=pump, daemon=True).start()
        collected: list[str] = []
        started = last = time.time()
        while True:
            try:
                line = lines.get(timeout=self.heartbeat)
            except queue.Empty:
                idle = time.time() - last
                util.log(f"  | ... still running ({int(time.time() - started)}s, {int(idle)}s silent)")
                if self.silent_timeout and idle > self.silent_timeout:
                    process.kill()
                    util.die(
                        f"no output for {int(idle)}s, killing what looks like a stuck step: "
                        + " ".join(cmd)
                    )
                continue
            if line is None:
                break
            last = time.time()
            util.log("  | " + line.rstrip())
            collected.append(line)
        code = process.wait()
        output = "".join(collected)
        if check and code != 0:
            util.die(f"command failed ({code}): {' '.join(cmd)}")
        return subprocess.CompletedProcess(cmd, code, output, None)

    def configure(
        self,
        toolset: str | None,
        vs_toolset: str | None,
        plat: str,
        arch: str,
        capture: bool = False,
    ):
        args = ["f", "-y", "-p", plat, "-a", arch, "-m", "release"]
        if vs_toolset:
            args.append(f"--vs_toolset={vs_toolset}")
        if toolset == "clang-cl":
            args.append("--toolchain=clang-cl")
        return self.xmake(args, capture=capture)


def list_xmake_releases(config: dict, refresh: bool = False) -> list[dict]:
    """All published xmake releases with publish dates (oldest first)."""
    from . import gh

    cache = util.cache_dir(config, "xmake") / "releases.json"
    if cache.is_file() and not refresh:
        return json.loads(cache.read_text(encoding="utf-8"))
    releases: list[dict] = []
    page = 1
    while page <= 20:
        batch = gh.api(
            f"repos/xmake-io/xmake/releases?per_page=100&page={page}", config=config, cache=False
        )
        if not batch:
            break
        for item in batch:
            tag = (item.get("tag_name") or "").lstrip("v")
            if not tag:
                continue
            releases.append(
                {
                    "version": tag,
                    "published_at": item.get("published_at") or item.get("created_at") or "",
                    "prerelease": bool(item.get("prerelease")),
                }
            )
        if len(batch) < 100:
            break
        page += 1
    releases.sort(key=lambda item: item["published_at"])
    util.write_json(cache, releases)
    return releases


def xmake_version_for_date(config: dict, cutoff_iso: str, floor: str | None = None) -> str:
    """Newest stable xmake released on/before ``cutoff_iso``, clamped to ``floor``."""
    releases = [
        item
        for item in list_xmake_releases(config)
        if not item["prerelease"] and item["version"].count(".") >= 2
    ]
    chosen = None
    for item in releases:
        if item["published_at"][:10] <= cutoff_iso[:10]:
            chosen = item["version"]
    floor = floor or str(util.load_config().get("xmake", {}).get("minArtifactsVersion", "3.0.0"))
    if chosen is None or semver.lt(chosen, floor):
        newer = [item["version"] for item in releases if not semver.lt(item["version"], floor)]
        chosen = newer[-1] if newer else floor
    return chosen


def ensure_xmake(config: dict, version: str, proxy: str | None = None) -> Path:
    """Download + extract ``xmake-v<version>.win64.zip``; returns ``xmake.exe``."""
    import urllib.request

    target = util.cache_dir(config, "xmake") / f"v{version}"
    exe = target / "xmake" / "xmake.exe"
    if exe.is_file():
        return exe
    url = (
        f"https://github.com/xmake-io/xmake/releases/download/v{version}/"
        f"xmake-v{version}.win64.zip"
    )
    util.ensure_dir(target)
    archive = target / f"xmake-v{version}.win64.zip"
    util.log(f"downloading {url}")
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy, "https": proxy} if proxy else {})
    )
    with opener.open(url) as response, open(archive, "wb") as handle:
        while True:
            chunk = response.read(1 << 20)
            if not chunk:
                break
            handle.write(chunk)
    with zipfile.ZipFile(archive) as zipped:
        zipped.extractall(target)
    if not exe.is_file():
        raise RuntimeError(f"xmake binary missing after extracting {archive}")
    return exe


def detect_vs_toolset(config: dict, explicit: str | None = None) -> str | None:
    """Newest installed MSVC toolset (``VC\\Tools\\MSVC\\<version>``)."""
    if explicit:
        return explicit
    configured = (config.get("platform") or {}).get("vsToolset")
    if configured:
        return configured
    found: set[str] = set()
    for installation in _visual_studio_installations():
        tools = installation / "VC" / "Tools" / "MSVC"
        if tools.is_dir():
            found.update(item.name for item in tools.iterdir() if item.is_dir())
    if not found:
        return None
    return sorted(found, key=_toolset_key)[-1]


def _visual_studio_installations() -> list[Path]:
    """Locate VS installations through vswhere, falling back to a directory scan."""
    installations: list[Path] = []
    program_files = os.environ.get("ProgramFiles(x86)") or os.environ.get("ProgramFiles") or ""
    vswhere = Path(program_files) / "Microsoft Visual Studio" / "Installer" / "vswhere.exe"
    if vswhere.is_file():
        result = util.run(
            [str(vswhere), "-latest", "-products", "*", "-property", "installationPath"],
            capture=True,
            check=False,
            quiet=True,
        )
        for line in (result.stdout or "").splitlines():
            line = line.strip()
            if line:
                installations.append(Path(line))
    for base in (
        Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Microsoft Visual Studio",
        Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)")) / "Microsoft Visual Studio",
    ):
        if base.is_dir():
            installations += [item for item in base.glob("*/*") if item.is_dir()]
    return installations


def _toolset_key(version: str):
    return tuple(int(part) for part in re.findall(r"\d+", version))


def create_sandbox(
    config: dict,
    name: str,
    xmake_version: str,
    *,
    proxy: str | None = None,
    toolchain: str | None = None,
    repositories: list[tuple[str, Path]] | None = None,
    main_repo: Path | None = None,
    binary_repo: Path | str | None = None,
    requires: list[str] | None = None,
    extra_lua: str = "",
) -> Sandbox:
    """Create a self-contained sandbox and write its ``xmake.lua``."""
    directory = util.ensure_dir(util.sandbox_root(config) / name)
    project_dir = util.ensure_dir(directory / "project")
    root_dir = util.ensure_dir(directory / "root")
    export_dir = util.ensure_dir(directory / "export")

    xmake_exe = ensure_xmake(config, xmake_version, proxy=proxy)
    bin_dir = xmake_exe.parent

    lines = [
        # Must match the xmake that actually runs here: an era-matched 2.8.x
        # refuses to run a project that claims to need 3.0.0.
        f'set_xmakever("{xmake_version}")',
        f'set_allowedplats("{config["platform"]["plat"]}")',
        'set_runtimes("MD")',
    ]
    for repo_name, repo_dir in repositories or []:
        lines.append(f'add_repositories("{repo_name} {Path(repo_dir).as_posix()}")')
    if toolchain:
        lines.append(f'set_toolchains("{toolchain}")')
    if extra_lua:
        lines.append(extra_lua)
    for require in requires or []:
        lines.append(f'add_requires("{require}")')
    lines.append('target("_buildcache_sandbox")')
    lines.append('    set_kind("phony")')
    lines.append("")
    util.write_text(project_dir / "xmake.lua", "\n".join(lines))

    env = util.subprocess_env(config, proxy=proxy)
    # xmake 3.x derives every runtime directory from XMAKE_GLOBALDIR/.xmake.
    env["XMAKE_GLOBALDIR"] = str(root_dir)
    env["PATH"] = (
        str(bin_dir)
        + os.pathsep
        + str(bin_dir / "winenv" / "bin")
        + os.pathsep
        + env["PATH"]
    )
    if main_repo is not None:
        env["XMAKE_MAIN_REPO"] = str(main_repo)
    if binary_repo is not None:
        # Replaces the official precompiled-artifacts mirror with ours.
        env["XMAKE_BINARY_REPO"] = str(binary_repo)

    return Sandbox(
        name=name,
        directory=directory,
        project_dir=project_dir,
        root_dir=root_dir,
        export_dir=export_dir,
        env=env,
        xmake_exe=xmake_exe,
        timeout=float(config.get("xmake", {}).get("timeoutSeconds", 5400)),
        heartbeat=float(config.get("xmake", {}).get("heartbeatSeconds", 30)),
        silent_timeout=float(config.get("xmake", {}).get("silentTimeoutSeconds", 1800)),
    )
