"""Shared git mirrors plus per-job snapshots pinned to a cutoff date."""

from __future__ import annotations

import time
from pathlib import Path

from . import util

FETCH_INTERVAL = 6 * 3600


def mirror(config: dict, name: str, url: str, proxy: str | None = None, refresh: bool = False) -> Path:
    """Clone once and periodically refresh a full mirror under ``.cache/mirrors``."""
    directory = util.absolute(config.get("_mirror_root", ".cache/mirrors")) / name
    env = util.subprocess_env(config, proxy=proxy)
    if not _is_clone(directory):
        util.ensure_dir(directory.parent)
        _clone_with_retry(url, directory, env)
        return directory
    stamp = directory / ".buildcache-fetched"
    stale = not stamp.is_file() or time.time() - stamp.stat().st_mtime > FETCH_INTERVAL
    if refresh or stale:
        _fetch_with_retry(directory, env)
        util.write_text(stamp, str(int(time.time())))
    return directory


def _is_clone(directory: Path) -> bool:
    """A clone is only usable once HEAD resolves (partial clones are not)."""
    if not (directory / ".git").is_dir():
        return False
    return util.run(
        ["git", "-C", str(directory), "rev-parse", "--verify", "HEAD"],
        check=False,
        quiet=True,
        capture=True,
    ).returncode == 0


def _clone_with_retry(url: str, directory: Path, env: dict, attempts: int = 3) -> None:
    for attempt in range(1, attempts + 1):
        util.rmtree(directory)
        result = util.run(
            ["git", "clone", url, str(directory)], env=env, quiet=True, check=False, capture=True
        )
        if result.returncode == 0 and _is_clone(directory):
            return
        util.warn(
            f"git clone {url} failed (attempt {attempt}/{attempts}); retrying"
        )
        time.sleep(5 * attempt)
    raise RuntimeError(f"cannot clone {url}")


def _fetch_with_retry(directory: Path, env: dict, attempts: int = 2) -> None:
    for attempt in range(1, attempts + 1):
        result = util.run(
            ["git", "-C", str(directory), "fetch", "--tags", "--force", "origin"],
            env=env,
            quiet=True,
            check=False,
            capture=True,
        )
        if result.returncode == 0:
            return
        util.warn(f"git fetch in {directory.name} failed (attempt {attempt}/{attempts})")
        time.sleep(5 * attempt)


def commit_at(mirror_dir: Path, cutoff_iso: str | None = None, ref: str = "HEAD", env: dict | None = None) -> str:
    if cutoff_iso:
        out = util.run_out(
            ["git", "-C", str(mirror_dir), "rev-list", "-n", "1", f"--before={cutoff_iso}", ref],
            env=env,
            check=False,
        )
        if out:
            return out
    return util.run_out(["git", "-C", str(mirror_dir), "rev-parse", ref], env=env)


def snapshot(
    mirror_dir: Path,
    dest: Path,
    commit: str | None = None,
    cutoff_iso: str | None = None,
    env: dict | None = None,
) -> str:
    """Materialise an independent working tree at ``commit`` (hardlinked clone)."""
    dest = Path(dest)
    if not commit:
        commit = commit_at(mirror_dir, cutoff_iso, env=env)
    util.rmtree(dest)
    util.ensure_dir(dest.parent)
    util.run(
        ["git", "clone", "--shared", "--no-checkout", "--quiet", str(mirror_dir), str(dest)],
        env=env,
        quiet=True,
    )
    util.run(
        ["git", "-C", str(dest), "checkout", "--detach", "--quiet", commit],
        env=env,
        quiet=True,
    )
    return commit


def head(directory: Path, env: dict | None = None) -> str:
    return util.run_out(["git", "-C", str(directory), "rev-parse", "HEAD"], env=env, check=False)


def file_at(directory: Path, commit: str, path: str, env: dict | None = None) -> str:
    return util.run_out(
        ["git", "-C", str(directory), "show", f"{commit}:{path}"], env=env, check=False
    )
