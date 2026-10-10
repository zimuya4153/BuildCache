"""Version helpers built on the `semver` package (installed automatically).

BuildCache compares real library versions and xmake releases (`11.2.0`,
`2025.02.05`, `v26.51.1-server.7`, ...); the library handles all of them, so
there is no hand-written comparison left.
"""

from __future__ import annotations

import semver as _semver


def parse(version: str) -> _semver.Version:
    """Parse a version, tolerating `v`, `alias:` prefixes and leading zeros.

    SemVer forbids leading zeros, but projects such as rapidjson or stb use date
    versions (`2025.02.05`), so the numeric components are normalised first.
    Incomplete versions are padded the same way: this ecosystem really does ship
    two-component versions (`leveldb 1.23`), and refusing to sort the build
    ledger over one of them would be a poor trade.
    """
    text = str(version).strip()
    if ":" in text and "//" not in text:
        text = text.split(":", 1)[1]
    text = text.lstrip("vV")
    head, _, tail = text.partition("-")
    parts = head.split(".")
    if parts and all(part.isdigit() for part in parts):
        while len(parts) < 3:
            parts.append("0")
        head = ".".join(str(int(part)) for part in parts)
        text = head + (("-" + tail) if tail else "")
    try:
        return _semver.Version.parse(text)
    except ValueError as error:
        raise ValueError("cannot parse version " + repr(version)) from error


def key(version: str):
    """Sort key: numeric version plus prerelease (release sorts above pre)."""
    try:
        parsed = parse(version)
    except ValueError:
        # Sorting the ledger must never fail over an unusual version string;
        # unparseable entries simply sort last, keeping the order stable.
        _warn_once(version)
        return (0, 0, 0, 0, str(version))
    prerelease = str(parsed.prerelease or "")
    return (parsed.major, parsed.minor, parsed.patch, 1 if not prerelease else 0, prerelease)


_WARNED: set[str] = set()


def _warn_once(version: str) -> None:
    if version in _WARNED:
        return
    _WARNED.add(version)
    from . import util

    util.warn(f"version {version!r} is not SemVer; ordering it by string instead")


def sort_desc(versions):
    return sorted(versions, key=key, reverse=True)


def compare(a: str, b: str) -> int:
    pa, pb = parse(a), parse(b)
    return (pa > pb) - (pa < pb)


def gt(a: str, b: str) -> bool:
    return compare(a, b) > 0


def lt(a: str, b: str) -> bool:
    return compare(a, b) < 0


def ge(a: str, b: str) -> bool:
    return compare(a, b) >= 0


def le(a: str, b: str) -> bool:
    return compare(a, b) <= 0
