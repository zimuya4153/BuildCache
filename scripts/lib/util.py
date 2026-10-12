"""Shared helpers: paths, config, logging, process and file I/O."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = ROOT / "config.json"

_CONFIG: dict | None = None

# A runner's console is not UTF-8: xmake prints bullets and arrows (👉) in its
# progress output, and a logger that dies on them takes the job down minutes into
# a build, with a traceback that says nothing about what was being built. Output
# goes out as UTF-8, with anything unencodable replaced rather than fatal.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass


def log(message: str) -> None:
    print(mask(str(message)), flush=True)


def log_step(message: str) -> None:
    print(mask(f"\n=== {message} ==="), flush=True)


def warn(message: str) -> None:
    print(mask(f"warning: {message}"), file=sys.stderr, flush=True)


def die(message: str, code: int = 1) -> None:
    print(mask(f"error: {message}"), file=sys.stderr, flush=True)
    raise SystemExit(code)


# Tokens must never reach a log file: pushed URLs embed them and the command echo
# below prints whole command lines.
_URL_CREDENTIALS = re.compile(r"(https?://)[^/@\s]+:[^/@\s]+@")
_TOKEN_LITERAL = re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{8,}|github_pat_[A-Za-z0-9_]{10,})")
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def mask(text: str) -> str:
    """Redact credentials and drop terminal control sequences from our output."""
    text = _ANSI.sub("", text)
    text = _URL_CREDENTIALS.sub(r"\1***:***@", text)
    return _TOKEN_LITERAL.sub("***", text)


def load_config() -> dict:
    global _CONFIG
    if _CONFIG is None:
        if not CONFIG_PATH.is_file():
            die(f"config not found: {CONFIG_PATH}")
        _CONFIG = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        try:  # validate the boundary so a typo fails now, not after a long build
            from . import schemas

            schemas.validate(schemas.Config, _CONFIG)
        except ImportError:
            pass
        except Exception as error:  # pydantic validation error, kept readable
            die(f"config.json is not valid: {error}")
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
    """Read a JSON document; a missing *or unusable* file yields the default.

    The index and the results are generated documents that get rewritten from
    scratch, so one truncated file (an interrupted run leaves empty ones behind)
    must not be able to abort the whole publish.
    """
    if not Path(path).is_file():
        return default
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        warn(f"{path}: unreadable JSON ({error}), treating it as absent")
        return default


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
    # Everything that touches GitHub (releases, pushes, API) should be attributed
    # to the bot account when its token is configured.
    bot = (config.get("bot") or {}) if isinstance(config, dict) else {}
    token_env = bot.get("tokenEnv") or "BUILDCACHE_BOT_TOKEN"
    bot_token = os.environ.get(token_env, "").strip()
    if bot_token:
        env["GH_TOKEN"] = bot_token
        env["GITHUB_TOKEN"] = bot_token
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


def git_identity(config: dict) -> tuple[str, str]:
    """Committer identity used for every generated commit."""
    bot = (config.get("bot") or {}) if isinstance(config, dict) else {}
    return (
        str(bot.get("name") or "github-actions[bot]"),
        str(bot.get("email") or "41898282+github-actions[bot]@users.noreply.github.com"),
    )


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
