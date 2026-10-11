"""Standalone ``git`` wrapper that pins clones to a cutoff date.

Copied into the sandbox as ``git.cmd`` + ``_buildcache_git_shim.py`` and placed
first on ``PATH``. It only ever runs inside a sandbox, so the developer's own
git usage is untouched.

Behaviour:

* any subcommand other than ``clone`` is forwarded verbatim
* after a successful ``clone`` the new working tree is checked out at the newest
  commit that existed on ``BUILDCACHE_GIT_CUTOFF``, recursively for submodules
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

LOG = Path(__file__).with_name("git-shim.log")


def real_git() -> str:
    override = os.environ.get("BUILDCACHE_REAL_GIT")
    if override:
        return override
    here = Path(__file__).resolve().parent
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if not entry:
            continue
        try:
            if Path(entry).resolve() == here:
                continue
        except OSError:
            continue
        for name in ("git.exe", "git.cmd", "git.bat", "git"):
            candidate = Path(entry) / name
            if candidate.is_file():
                return str(candidate)
    return "git"


def note(message: str) -> None:
    try:
        with open(LOG, "a", encoding="utf-8") as handle:
            handle.write(f"[{time.strftime('%H:%M:%S')}] {message}\n")
    except OSError:
        pass


def cutoff() -> str:
    return os.environ.get("BUILDCACHE_GIT_CUTOFF", "")


def pin(repo: Path, depth: int = 0) -> None:
    if depth > 4 or not (repo / ".git").exists():
        return
    when = cutoff()
    if not when:
        return
    git = real_git()
    if (repo / ".git" / "shallow").exists():
        subprocess.run([git, "fetch", "--unshallow", "--tags"], cwd=repo,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    commit = subprocess.run(
        [git, "rev-list", "-n", "1", f"--before={when}", "HEAD"],
        cwd=repo, capture_output=True, text=True,
    ).stdout.strip()
    if not commit:
        return
    subprocess.run([git, "checkout", "-q", commit], cwd=repo)
    note(f"pin {repo.name} -> {commit[:12]} (before {when})")
    if (repo / ".gitmodules").exists():
        subprocess.run([git, "submodule", "update", "--init", "--recursive"], cwd=repo)
        for inner in sorted((repo / ".git" / "modules").rglob("modules")):
            pin(inner.parent.parent, depth + 1)


def clone_target(argv: list[str]) -> str | None:
    positional: list[str] = []
    skip = False
    for arg in argv:
        if skip:
            skip = False
            continue
        if arg.startswith("-"):
            if arg in ("--depth", "--branch", "-b", "--origin", "-o", "--config",
                       "--reference", "--separate-git-dir", "--template", "--jobs", "-j"):
                skip = True
            continue
        positional.append(arg)
    if not positional:
        return None
    url = positional[0]
    if len(positional) > 1:
        target = positional[1]
    else:
        target = url.rstrip("/").split("/")[-1]
    if target.endswith(".git"):
        target = target[:-4]
    return target


def _ref_position(argv: list[str]) -> tuple[int, bool] | None:
    """Where the ref of a clone is: ``(index, inline)``, or ``None``.

    ``-b X`` puts it after the flag (``(i + 1, False)``); ``--branch=X`` puts it
    inside the argument (``(i, True)``).
    """
    for index, arg in enumerate(argv):
        if arg in ("-b", "--branch"):
            return (index + 1, False) if index + 1 < len(argv) else None
        if arg.startswith("--branch="):
            return index, True
    return None


def with_v(argv: list[str]) -> list[str]:
    """``-b 0.4.0`` -> ``-b v0.4.0``: the same release, spelled as upstream tags it."""
    found = _ref_position(argv)
    if found is None:
        return argv
    index, inline = found
    if inline:
        head, _, ref = argv[index].partition("=")
        return list(argv[:index]) + [f"{head}=v{ref}"] + list(argv[index + 1:])
    return list(argv[:index]) + [f"v{argv[index]}"] + list(argv[index + 1:])


def without_ref(argv: list[str]) -> list[str]:
    """Drop ``-b <ref>``: clone the default branch and let ``pin()`` rewind it."""
    found = _ref_position(argv)
    if found is None:
        return argv
    index, inline = found
    start = index if inline else index - 1
    return list(argv[:start]) + list(argv[index + 1:])


def main(argv: list[str]) -> int:
    git = real_git()
    if not argv or argv[0] != "clone":
        return subprocess.call([git] + argv)

    target = clone_target(argv[1:])
    destination = Path.cwd() / target if target else None
    # A recipe may name a version that upstream tags with a "v" -- `0.4.0` against
    # `v0.4.0` -- and then xmake asks git for a ref that does not exist and the
    # clone dies before anything can be built. It is the *era* plan that meets
    # this, because the recipe as it was on that date had not pinned the commit
    # yet, and the failure looks nothing like its cause. So the "v" is tried, and
    # failing that the clone is repeated without a ref: the shim's own pin() then
    # checks the tree out at the newest commit before the cutoff.
    attempts = [argv]
    if _ref_position(argv[1:]) is not None:
        attempts.append(with_v(argv))
        attempts.append(without_ref(argv))
    result = 0
    for index, attempt in enumerate(attempts):
        result = subprocess.call([git] + attempt)
        if result == 0:
            if index:
                note(f"clone retried without a ref that does not exist: {' '.join(attempt)}")
            break
        # A failed clone leaves a directory behind, and the next attempt refuses
        # to clone into it.
        if index + 1 < len(attempts) and destination is not None:
            note(f"clone failed, retrying ({index + 1}): {' '.join(attempt)}")
            shutil.rmtree(destination, ignore_errors=True)
    if result != 0:
        return result
    if target:
        pin(Path.cwd() / target)
    return result


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
