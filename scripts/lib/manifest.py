"""Read/write xmake precompiled-artifact manifests.

The format is the one consumed by ``xmake``'s package resolver: a Lua table
living at ``packages/<letter>/<package>/<version>/manifest.txt`` whose keys are
``<plat>-<arch>-vc<toolset>-<buildhash>`` and whose values carry the download
URL plus sha256.
"""

from __future__ import annotations

from pathlib import Path

from . import util


def build_id(plat: str, arch: str, toolset: str | None, build_hash: str) -> str:
    """Compose the manifest key exactly like xmake's own artifacts mirror."""
    if toolset:
        # The toolset itself is carried by the entry's ``toolset`` field, which
        # is what xmake compares; the key only has to start with "vc".
        return f"{plat}-{arch}-vc-{build_hash}"
    return f"{plat}-{arch}-{build_hash}"


def entry(url: str, sha256: str, toolset: str | None = None) -> dict:
    data = {"urls": url, "sha256": sha256}
    if toolset:
        data["toolset"] = toolset
    return data


def manifest_path(repo_root: Path, package: str, version: str) -> Path:
    return Path(repo_root) / "packages" / package[0].lower() / package.lower() / version / "manifest.txt"


def merge(repo_root: Path, package: str, version: str, key: str, data: dict) -> Path:
    """Insert/update one entry, preserving every previously published entry."""
    path = manifest_path(repo_root, package, version)
    entries: dict[str, dict] = {}
    existing = _parse(util.read_text(path)) if path.is_file() else {}
    entries.update(existing)
    current = entries.get(key, {})
    current.update(data)
    entries[key] = current
    util.write_text(path, render(entries))
    return path


def write_all(repo_root: Path, package: str, version: str, entries: dict[str, dict]) -> Path:
    """Rewrite a manifest from scratch so it always mirrors the index."""
    path = manifest_path(repo_root, package, version)
    if not entries:
        path.unlink(missing_ok=True)
        return path
    util.write_text(path, render(entries))
    return path


def read(repo_root: Path, package: str, version: str) -> dict[str, dict]:
    path = manifest_path(repo_root, package, version)
    return _parse(util.read_text(path)) if path.is_file() else {}


def render(entries: dict[str, dict]) -> str:
    lines = ["{"]
    for key in sorted(entries):
        data = entries[key]
        lines.append(f'    ["{key}"] = {{')
        for field in ("toolset", "urls", "sha256"):
            if field in data:
                lines.append(f'        {field} = "{data[field]}",')
        lines.append("    },")
    lines.append("}")
    lines.append("")
    return "\n".join(lines)


_FIELDS = ("toolset", "urls", "sha256")


def _parse(text: str) -> dict[str, dict]:
    """Parse the manifest subset we generate (also tolerates hand edits)."""
    import re

    entries: dict[str, dict] = {}
    current_key = None
    for raw in text.splitlines():
        line = raw.strip()
        match = re.match(r'^\["([^"]+)"\]\s*=\s*\{', line)
        if match:
            current_key = match.group(1)
            entries[current_key] = {}
            continue
        if current_key is None:
            continue
        for field in _FIELDS:
            field_match = re.match(rf'^{field}\s*=\s*"(.*)",?$', line)
            if field_match:
                entries[current_key][field] = field_match.group(1)
                break
        if line.startswith("}"):
            current_key = None
    return {k: v for k, v in entries.items() if v}
