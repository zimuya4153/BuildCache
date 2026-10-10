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
    """
    text = str(version).strip()
    if ":" in text and "//" not in text:
        text = text.split(":", 1)[1]
    text = text.lstrip("vV")
    head, _, tail = text.partition("-")
    parts = head.split(".")
    if parts and all(part.isdigit() for part in parts):
        head = ".".join(str(int(part)) for part in parts)
        text = head + (("-" + tail) if tail else "")
    try:
        return _semver.Version.parse(text)
    except ValueError as error:
        raise ValueError("cannot parse version " + repr(version)) from error


def key(version: str):
    """Sort key: numeric version plus prerelease (release sorts above pre)."""
    parsed = parse(version)
    prerelease = str(parsed.prerelease or "")
    return (parsed.major, parsed.minor, parsed.patch, 1 if not prerelease else 0, prerelease)


def sort_desc(versions):
    return sorted(versions, key=key, reverse=True)


def compare(a: str, b: str) -> int:
    pa, pb = parse(a), parse(b)
    return (pa > pb) - (pa < pb)


def gt(a: str, b: str) -> bool:
    return compare(a, b) > 0


def lt(a: str, b: str) -> bool:
    return compare(a, b) < 0