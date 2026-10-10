"""Runtime dependencies are installed automatically.

The framework is allowed to use any third-party package, so instead of keeping
optional fallbacks around, missing imports are installed on the spot from
``requirements.txt`` (through the configured proxy when one is set).
"""

from __future__ import annotations

import importlib
import os
import subprocess
import sys

# import name -> distribution name
REQUIRED = {
    "semver": "semver>=3.0",
    "pydantic": "pydantic>=2.0",
    "pytest": "pytest>=8.0",
}

_ENSURED = False


def ensure(required: dict[str, str] | None = None) -> None:
    """Install anything that does not import yet. Idempotent per process."""
    global _ENSURED
    if _ENSURED:
        return
    _ENSURED = True
    if os.environ.get("BUILDCACHE_NO_AUTO_INSTALL") == "1":
        return
    required = required or REQUIRED
    missing = []
    for module, spec in required.items():
        try:
            importlib.import_module(module)
        except ImportError:
            missing.append(spec)
    if not missing:
        return
    print(f"installing missing dependencies: {', '.join(missing)}", flush=True)
    cmd = [sys.executable, "-m", "pip", "install", *missing]
    env = os.environ.copy()
    proxy = os.environ.get("BUILDCACHE_PROXY", "").strip()
    if proxy:
        env["HTTP_PROXY"] = env["HTTPS_PROXY"] = proxy
    result = subprocess.run(cmd, env=env, text=True, capture_output=True)
    if result.returncode != 0:
        sys.stderr.write(result.stdout or "")
        sys.stderr.write(result.stderr or "")
        raise RuntimeError(
            f"cannot install required dependencies ({', '.join(missing)}); "
            f"install them manually: {sys.executable} -m pip install {' '.join(missing)}"
        )
    for module in required:
        importlib.invalidate_caches()
        try:
            importlib.import_module(module)
        except ImportError as error:  # pragma: no cover - installation lied
            raise RuntimeError(f"{module} still missing after installation") from error
