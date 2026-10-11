"""Repair legacy recipe patches that no modern git can apply.

Older recipes in the wild contain ``.patch`` files whose blank context lines
lost their leading space (usually by an editor trimming trailing whitespace).
GNU patch tolerates that, ``git apply`` does not and fails with
``corrupt patch`` / ``patch does not apply``. We repair the referenced patch
files in place and update the checksum the recipe declares for them.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

from . import util

_ADD_PATCHES = re.compile(
    r'add_patches\(\s*("[^"]+"|\'[^\']+\')\s*,\s*"([^"]+)"\s*,\s*"([0-9a-fA-F]{64})"\s*\)'
)
_ADD_VERSIONS = re.compile(r'^(\s*)add_versions\(\s*"([^"]+)"', re.M)
_ADD_VERSIONFILES = re.compile(r'add_versionfiles\(\s*"([^"]+)"')
_SEMVER = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.\-+]*)?$")
_FOUR_PART = re.compile(r"^\d+\.\d+\.\d+\.\d+")
_HUNK = re.compile(r"^@@\s*-(\d+)(?:,(\d+))?\s*\+(\d+)(?:,(\d+))?\s*@@")


def repair_patch(text: str) -> tuple[str, int]:
    """Give blank context lines their leading space back. Returns (text, repairs)."""
    match = re.search(r"\r\n|\n", text)
    newline = match.group(0) if match else "\n"
    lines = text.split(newline)
    out: list[str] = []
    remaining_old = remaining_new = 0
    repairs = 0
    for line in lines:
        hunk = _HUNK.match(line)
        if hunk:
            remaining_old = int(hunk.group(2) or 1)
            remaining_new = int(hunk.group(4) or 1)
        elif remaining_old > 0 or remaining_new > 0:
            stripped = line[:-1] if line.endswith("\r") else line
            if stripped == "":
                out.append(" " + (line[-1:] if line.endswith("\r") else ""))
                remaining_old -= 1
                remaining_new -= 1
                repairs += 1
                continue
            marker = stripped[:1]
            if marker == " ":
                remaining_old -= 1
                remaining_new -= 1
            elif marker == "-":
                remaining_old -= 1
            elif marker == "+":
                remaining_new -= 1
        out.append(line)
    return newline.join(out), repairs


def apply_fixups(config: dict, source_name: str, snapshot: Path) -> list[str]:
    """Overlay our own corrected recipe files over a freshly made snapshot."""
    fixup_root = util.ROOT / str(config.get("recipes", {}).get("fixupDir", "recipes/fixups"))
    source_root = fixup_root / source_name
    if not source_root.is_dir():
        return []
    notes: list[str] = []
    for path in sorted(source_root.rglob("*")):
        if not path.is_file():
            continue
        target = Path(snapshot) / path.relative_to(source_root)
        target.parent.mkdir(parents=True, exist_ok=True)
        was = util.sha256_file(target) if target.is_file() else None
        shutil.copyfile(path, target)
        if was != util.sha256_file(target):
            notes.append(f"{source_name}: fixup {path.relative_to(source_root)}")
    return notes


def plugin_repair(config: dict, plugin, source_name: str, snapshot: Path) -> list[str]:
    """Give the plugin a chance to fix project-specific recipe problems.

    The framework only does mechanical repairs; anything that needs knowledge of
    a particular project (legacy version numbering, renamed upstream assets, …)
    is implemented by that project's plugin in ``repair_recipes``.
    """
    if plugin is None:
        return []
    module = getattr(plugin, "module", plugin)
    handler = getattr(module, "repair_recipes", None)
    if handler is None:
        return []
    notes = handler(config, source_name, snapshot) or []
    return [str(note) for note in notes]


def _report_unparseable_versions(recipe: Path, text: str) -> list[str]:
    """Point out versions xmake cannot parse, without touching them."""
    bad = [
        match.group(2)
        for match in _ADD_VERSIONS.finditer(text)
        if not _semver_ok(match.group(2))
    ]
    for match in _ADD_VERSIONFILES.finditer(text):
        candidate = recipe.parent / match.group(1)
        if candidate.is_file():
            bad += [
                line.split()[0]
                for line in util.read_text(candidate).splitlines()
                if line.split() and not _semver_ok(line.split()[0])
            ]
    if not bad:
        return []
    return [
        f"{recipe.parent.name}: {len(bad)} version(s) xmake cannot parse "
        f"(e.g. {bad[0]}) — add a repair_recipes hook in the plugin"
    ]


def normalize_tree(root: Path, drop_unparseable: bool = False) -> list[str]:
    """Generic, mechanical repairs only.

    * malformed patch files get their blank context lines back
    * declared checksums are refreshed to match the files on disk

    Anything project-specific (for example legacy version numbering) belongs in
    a plugin's ``repair_recipes`` hook, which runs right after this.
    """
    root = Path(root)
    notes: list[str] = []
    for recipe in sorted(root.glob("packages/*/*/xmake.lua")):
        text = util.read_text(recipe)
        if "add_patches" not in text and "add_versions" not in text and "add_versionfiles" not in text:
            continue
        if drop_unparseable:
            updated, version_notes = _drop_unparseable_versions(recipe, text)
        else:
            updated, version_notes = text, _report_unparseable_versions(recipe, text)
        notes.extend(version_notes)
        if "add_patches" not in text:
            if updated != text:
                util.write_text(recipe, updated)
            continue
        for match in _ADD_PATCHES.finditer(text):
            relative, declared = match.group(2), match.group(3)
            patch = recipe.parent / relative
            if not patch.is_file():
                continue
            content = util.read_text(patch)
            repaired, count = repair_patch(content)
            if not count:
                digest = util.sha256_file(patch)
                if digest != declared:
                    updated = updated.replace(declared, digest)
                    notes.append(
                        f"{recipe.parent.name}/{relative}: checksum {declared[:12]} -> {digest[:12]}"
                    )
                continue
            util.write_text(patch, repaired)
            digest = util.sha256_file(patch)
            updated = updated.replace(declared, digest)
            notes.append(
                f"{recipe.parent.name}/{relative}: repaired {count} blank context line(s), "
                f"checksum {declared[:12]} -> {digest[:12]}"
            )
        if updated != text:
            util.write_text(recipe, updated)
    return notes


def _drop_unparseable_versions(recipe: Path, text: str) -> tuple[str, list[str]]:
    """Remove version entries xmake's semver parser rejects (legacy 4-part ones).

    Old recipes in this ecosystem declare versions such as ``1.21.70.04-server.2``.
    xmake parses every declared version while resolving a package and aborts with
    ``unable to parse semver``, which makes the whole repository unusable. Such
    versions are dropped here; anything pinned to them is reported instead of
    failing silently.
    """
    notes: list[str] = []
    kept_lines: list[str] = []
    dropped: list[str] = []
    for line in text.split("\n"):
        match = _ADD_VERSIONS.match(line)
        if match and not _semver_ok(match.group(2)):
            dropped.append(match.group(2))
            continue
        kept_lines.append(line)
    updated = "\n".join(kept_lines)
    for match in list(_ADD_VERSIONFILES.finditer(text)):
        listing = recipe.parent / match.group(1)
        if not listing.is_file():
            continue
        original = util.read_text(listing)
        kept = []
        for line in original.split("\n"):
            parts = line.split()
            if parts and not _semver_ok(parts[0]):
                dropped.append(parts[0])
                continue
            kept.append(line)
        if kept != original.split("\n"):
            util.write_text(listing, "\n".join(kept))
    if dropped:
        notes.append(
            f"{recipe.parent.name}: dropped {len(dropped)} unparseable version(s), "
            f"e.g. {dropped[0]}"
        )
    return updated, notes


def _semver_ok(version: str) -> bool:
    """Only reject the known-bad legacy form: four numeric components."""
    candidate = version.split(":", 1)[1] if ":" in version and "//" not in version else version
    candidate = candidate.lstrip("v")
    if _SEMVER.match(candidate):
        return True
    return not _FOUR_PART.match(candidate)
