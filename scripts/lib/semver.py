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
    ka, kb = key(a), key(b)
    return (ka > kb) - (ka < kb)


def gt(a: str, b: str) -> bool:
    return compare(a, b) > 0


def lt(a: str, b: str) -> bool:
    return compare(a, b) < 0
