"""Shared helpers: paths, config, logging, process and file I/O."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = ROOT / "config.json"

_CONFIG: dict | None = None


def log(message: str) -> None:
    print(message, flush=True)


def log_step(message: str) -> None:
    print(f"\n=== {message} ===", flush=True)


def warn(message: str) -> None:
    print(f"warning: {message}", file=sys.stderr, flush=True)


def die(message: str, code: int = 1) -> None:
    print(f"error: {message}", file=sys.stderr, flush=True)
    raise SystemExit(code)


def load_config() -> dict:
    global _CONFIG
    if _CONFIG is None:
        if not CONFIG_PATH.is_file():
            die(f"config not found: {CONFIG_PATH}")
        _CONFIG = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    return _CONFIG


def work_dir(config: dict, name: str) -> Path:
    path = ROOT / ".work" / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def cache_dir(config: dict, name: str) -> Path:
    path = ROOT / ".cache" / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def read_text(path: Path, default: str = "") -> str:
    if not Path(path).is_file():
        return default
    return Path(path).read_text(encoding="utf-8")


def write_text(path: Path, text: str) -> None:
    """Write UTF-8 without BOM (xmake's Lua parser rejects a BOM)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def read_json(path: Path, default=None):
    if not Path(path).is_file():
        return default
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path: Path, data) -> None:
    write_text(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def ensure_dir(path: Path) -> Path:
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    return path


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    import hashlib

    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(chunk), b""):
            digest.update(block)
    return digest.hexdigest()


def proxy_url() -> str | None:
    """Opt-in proxy for local development, never enabled implicitly."""
    value = os.environ.get("BUILDCACHE_PROXY", "").strip()
    return value or None


def subprocess_env(config: dict, extra: dict | None = None, proxy: str | None = None) -> dict:
    env = os.environ.copy()
    if extra:
        env.update({str(k): str(v) for k, v in extra.items()})
    # Two git behaviours have to be forced for reproducible builds, without
    # touching the user's own configuration:
    #   * core.longpaths  - package builds nest deeply on Windows
    #   * core.autocrlf=false - recipe patches must stay byte faithful, otherwise
    #     a CRLF-converted diff no longer applies
    env.setdefault("GIT_CONFIG_COUNT", "2")
    env.setdefault("GIT_CONFIG_KEY_0", "core.longpaths")
    env.setdefault("GIT_CONFIG_VALUE_0", "true")
    env.setdefault("GIT_CONFIG_KEY_1", "core.autocrlf")
    env.setdefault("GIT_CONFIG_VALUE_1", "false")
    proxy = proxy if proxy is not None else proxy_url()
    if proxy:
        env["HTTP_PROXY"] = proxy
        env["HTTPS_PROXY"] = proxy
        env["http_proxy"] = proxy
        env["https_proxy"] = proxy
    else:
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            env.pop(key, None)
    return env


def absolute(path_like, base: Path | None = None) -> Path:
    """Resolve a config path against the repository root when it is relative."""
    path = Path(path_like)
    if path.is_absolute():
        return path
    return (base or ROOT) / path


def default_sandbox_root() -> Path:
    """A deliberately short path: CMake rejects object paths beyond 250 chars.

    Package builds nest deeply (``.xmake/cache/packages/.../CMakeFiles/
    CMakeScratch/TryCompile-*/``), so the sandbox has to sit near the drive root
    instead of inside the checkout.
    """
    import tempfile

    drive = Path(tempfile.gettempdir()).drive or "C:"
    return Path(f"{drive}/.buildcache")


def sandbox_root(config: dict) -> Path:
    configured = (config.get("paths") or {}).get("sandboxRoot")
    return absolute(configured) if configured else default_sandbox_root()


def run(
    cmd: list,
    cwd: Path | None = None,
    env: dict | None = None,
    capture: bool = False,
    check: bool = True,
    quiet: bool = False,
    timeout: float | None = None,
) -> subprocess.CompletedProcess:
    cmd = [str(part) for part in cmd]
    if not quiet:
        log("+ " + " ".join(_quote(part) for part in cmd))
    try:
        result = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            env=env,
            capture_output=capture,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        die(f"command timed out after {int(timeout)}s: {' '.join(cmd)}")
        raise  # unreachable, keeps type checkers happy
    if check and result.returncode != 0:
        if capture:
            tail = "\n".join((result.stdout or "").splitlines()[-40:])
            err = "\n".join((result.stderr or "").splitlines()[-40:])
            die(f"command failed ({result.returncode}): {' '.join(cmd)}\n{tail}\n{err}")
        die(f"command failed ({result.returncode}): {' '.join(cmd)}")
    return result


def run_out(cmd: list, cwd: Path | None = None, env: dict | None = None, check: bool = True) -> str:
    result = run(cmd, cwd=cwd, env=env, capture=True, check=check, quiet=True)
    return (result.stdout or "").strip()


def _quote(part: str) -> str:
    return f'"{part}"' if " " in part else part


def rmtree(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)


def find_tool(name: str) -> str | None:
    return shutil.which(name)
