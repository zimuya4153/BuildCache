"""Install the sandbox ``git`` wrapper (see :mod:`git_shim_impl`)."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from . import util


def install(config: dict, sandbox_env: dict, cutoff_iso: str, real_git: str | None = None) -> Path:
    """Write the shim into its own directory and prepend it to ``PATH``."""
    directory = util.ensure_dir(util.sandbox_root(config) / ".git-shim")
    script = directory / "_buildcache_git_shim.py"
    source = Path(__file__).with_name("git_shim_impl.py")
    shutil.copyfile(source, script)
    util.write_text(directory / "git.cmd", f'@"{sys.executable}" "{script}" %*\n')
    sandbox_env["BUILDCACHE_GIT_CUTOFF"] = cutoff_iso
    if real_git:
        sandbox_env["BUILDCACHE_REAL_GIT"] = real_git
    sandbox_env["PATH"] = str(directory) + os.pathsep + sandbox_env.get("PATH", "")
    return directory
