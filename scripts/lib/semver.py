"""Minimal version helpers for xmake-style version strings."""

from __future__ import annotations

import re

_PATTERN = re.compile(r"^v?(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:[-+](.*))?$")


def key(version: str):
    match = _PATTERN.match(str(version).strip())
    if not match:
        return (0, 0, 0, str(version))
    major, minor, patch, suffix = match.groups()
    return (int(major), int(minor or 0), int(patch or 0), suffix or "")


def sort_desc(versions):
    return sorted(versions, key=key, reverse=True)


def compare(a: str, b: str) -> int:
    """Prefer the `semver` package when it is installed, fall back to `key()`."""
    left, right = _library(a), _library(b)
    if left is not None and right is not None:
        return left.__gt__(right) - (left < right)
    ka, kb = key(a), key(b)
    return (ka > kb) - (ka < kb)


try:  # optional dependency: pip install semver
    import semver as _semver_package
except ImportError:  # pragma: no cover - dependency is optional
    _semver_package = None


def _library(version: str):
    if _semver_package is None:
        return None
    try:
        return _semver_package.Version.parse(str(version).strip().lstrip("v"))
    except (ValueError, TypeError):
        return None


def gt(a: str, b: str) -> bool:
    return compare(a, b) > 0


def lt(a: str, b: str) -> bool:
    return compare(a, b) < 0
