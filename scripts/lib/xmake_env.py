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


#: xmake copies every installed file in verbose mode, one line each ("Copying:
#: ..." / "> copy ..."): a package with a few hundred headers then buries
#: everything else in the log. Only the printing is filtered -- such a line still
#: counts as activity for the heartbeat, otherwise a long copy phase would look
#: like the stuck step that guard exists for.
_COPY_NOISE = re.compile(r"^\s*>?\s*(copy\b|copying:)", re.IGNORECASE)


def is_copy_noise(line: str) -> bool:
    """Is this verbose xmake line one of the copy notices?"""
    return bool(_COPY_NOISE.match(line or ""))


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
    #: Run xmake verbosely, so the log shows what it is doing instead of going
    #: quiet for minutes at a time. The copy operations are filtered out below:
    #: they are the bulk of that output and say nothing.
    verbose: bool = True

    def xmake(
        self,
        args: list,
        cwd: Path | None = None,
        check: bool = True,
        tail: int = 0,
        capture: bool = False,
        quiet: bool = False,
        stream: bool = False,
    ):
        if stream:
            return self._xmake_streaming(args, cwd=cwd, check=check)
        if self.verbose:
            args = list(args) + ["-v"]
        result = util.run(
            [str(self.xmake_exe)] + [str(a) for a in args],
            cwd=cwd or self.project_dir,
            env=self.env,
            capture=capture or tail > 0,
            check=check,
            timeout=self.timeout,
            quiet=quiet,
        )
        output = result.stdout or ""
        if capture and output and not quiet:
            for line in output.splitlines():
                if not is_copy_noise(line):
                    util.log("  | " + line)
        elif tail > 0 and output:
            for line in output.splitlines()[-tail:]:
                if not is_copy_noise(line):
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

        if self.verbose:
            args = list(args) + ["-v"]
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
        previous = None
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
            text = line.rstrip()
            # Progress animations redraw the same line; keep the log readable.
            if text and text != previous and not is_copy_noise(text):
                util.log("  | " + text)
            previous = text
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
        check: bool = True,
        stream: bool = False,
    ):
        args = ["f", "-y", "-p", plat, "-a", arch, "-m", "release"]
        if vs_toolset:
            args.append(f"--vs_toolset={vs_toolset}")
        if toolset == "clang-cl":
            args.append("--toolchain=clang-cl")
        # `xmake f` is where the dependency closure actually gets installed, so
        # it is usually the longest silent step: it needs the heartbeat too.
        return self.xmake(args, capture=capture, check=check, stream=stream)


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


def newest_xmake(config: dict) -> str:
    """The newest xmake there is.

    An era build uses the xmake of its own time; anything built for a *consumer*
    has to use the current one, because that is what the project on the other side
    runs -- and because today's recipes use today's APIs.
    """
    import datetime

    today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    return xmake_version_for_date(config, today)


def ensure_xmake(config: dict, version: str, proxy: str | None = None) -> Path:
    """Download + extract ``xmake-v<version>.win64.zip``; returns ``xmake.exe``."""
    import urllib.request

    target = util.cache_dir(config, "xmake") / f"v{version}"
    exe = target / "xmake" / "xmake.exe"
    if not exe.is_file():
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
    # a tree that was cached before this backport existed still needs it
    patch_cmake_toolset_family(exe.parent)
    return exe


#: xmake before 2.9.4 derives CMake's platform toolset as "v<major><first digit of
#: minor>". That is right up to MSVC 14.3x (v143) and wrong from 14.4x on: those are
#: still the v143 family ("MSVC v143 - VS 2022 C++ build tools (v14.40)"), so the
#: derivation asks MSBuild for "v144" -- a Visual Studio 2026 toolset -- and every
#: CMake package dies with MSB8020. 2.9.4 maps 14.40..14.49 back to v143 ("@see
#: https://github.com/xmake-io/xmake/pull/5176") and the 2.8 line never got it. An
#: era build deliberately uses the xmake of its own time, so the backport lands on
#: the xmake we downloaded, instead of pinning an MSVC generation the images no
#: longer carry.
_TOOLSET_FAMILY_OLD = '            toolset_ver = "v" .. verinfo[1] .. (verinfo[2]:sub(1, 1) or "0")'
_TOOLSET_FAMILY_NEW = """            toolset_ver = "v" .. verinfo[1] .. (verinfo[2]:sub(1, 1) or "0")
            -- BuildCache: 14.4x is the v143 family (VS 2022), not v144 (VS 2026).
            local _minor = tonumber(verinfo[2]) or 0
            if toolset_ver == "v144" and _minor >= 40 and _minor < 50 then
                toolset_ver = "v143"
            end"""


def patch_cmake_toolset_family(xmake_dir: Path) -> bool:
    """Backport 2.9.4's fix into an xmake that predates it. Idempotent.

    Only the 2.8 line carries this derivation inline; 2.9.4+ moved it into
    ``modules/private/utils/toolchain.lua`` and already maps v144 back to v143.
    ``private/utils/upgrade_vsproj.lua`` repeats the derivation, but only
    ``xmake project -k`` reaches it, which no package install runs.
    """
    path = Path(xmake_dir) / "modules" / "package" / "tools" / "cmake.lua"
    if not path.is_file():
        return False
    text = util.read_text(path)
    if _TOOLSET_FAMILY_NEW in text or _TOOLSET_FAMILY_OLD not in text:
        return False
    util.write_text(path, text.replace(_TOOLSET_FAMILY_OLD, _TOOLSET_FAMILY_NEW, 1))
    util.log("patched xmake's cmake.lua: 14.4x is the v143 toolset family, not v144")
    return True


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
    # No colours and no animated progress: the animation redraws one line with
    # \r, which in a piped log becomes either noise or a long silent gap.
    env["XMAKE_COLORTERM"] = "nocolor"
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
        verbose=bool(config.get("xmake", {}).get("verbose", True)),
    )


def failed_packages(sandbox) -> list[tuple[str, str]]:
    """Every package whose install left an ``installdir.failed`` marker.

    The layout under the sandbox cache is ``packages/<key>/<letter>/<name>/<version>/``,
    so the marker tells us exactly which package to retry on its own.
    """
    found: list[tuple[str, str]] = []
    cache = Path(sandbox.root_dir) / ".xmake" / "cache" / "packages"
    for marker in sorted(cache.glob("*/*/*/*/installdir.failed")):
        found.append((marker.parent.parent.name, marker.parent.name))
    return found


def failed_install_logs(sandbox) -> list[dict]:
    """The logs xmake writes next to a package it failed to install.

    xmake's own advice is "if you want to get more verbose errors, please see:
    <path>", and that path is inside the sandbox -- which nobody can open from a
    CI runner. So the logs are read here and printed by :func:`dump_failed_logs`.
    """
    cache = Path(sandbox.root_dir) / ".xmake" / "cache" / "packages"
    found: list[dict] = []
    for marker in sorted(cache.glob("*/*/*/*/installdir.failed")):
        directory = marker / "logs"
        found.append(
            {
                "package": marker.parent.parent.name,
                "version": marker.parent.name,
                "logs": sorted(directory.glob("*.txt")) if directory.is_dir() else [],
            }
        )
    return found


def dump_failed_logs(config: dict, sandbox) -> None:
    """Print why every failed package failed, instead of pointing into the sandbox."""
    limit = int(config.get("xmake", {}).get("failedLogLines", 120))
    for item in failed_install_logs(sandbox):
        if not item["logs"]:
            util.warn(f"{item['package']} {item['version']}: no install log was written")
            continue
        for path in item["logs"]:
            lines = util.read_text(path).splitlines()
            util.log(f"--- {item['package']} {item['version']} install log ({path})")
            hidden = len(lines) - limit
            for line in lines[-limit:] if limit else lines:
                util.log("  | " + line)
            if hidden > 0:
                util.log(f"  | ... ({hidden} earlier line(s) not shown)")
