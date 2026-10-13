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
_VERSION_COMMIT = re.compile(r'add_versions\(\s*"([^"]+)"\s*,\s*"([^"]+)"')
_ADD_VERSIONFILES = re.compile(r'add_versionfiles\(\s*"([^"]+)"')
_SEMVER = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.\-+]*)?$")
_FOUR_PART = re.compile(r"^\d+\.\d+\.\d+\.\d+")
_HUNK = re.compile(r"^@@\s*-(\d+)(?:,(\d+))?\s*\+(\d+)(?:,(\d+))?\s*@@")
_ADD_URLS = re.compile(r'add_urls\(\s*"([^"]+)"')
_ADD_VERSIONS_ONE = re.compile(r'add_versions\(\s*"([^"]+)"\s*\)')
_ON_INSTALL = re.compile(r"^\s*on_install\(function", re.M)

#: Runs inside a recipe, in the project's own directory, just before the project is
#: built. A require the project leaves *without* a version resolves to the newest
#: one there is -- or, when the project's own build sees a rewound repository, to
#: the newest of *that* moment, which is still not the one its sources were built
#: against (three days were enough for `preloader` v1.6.0 to break `levilamina`
#: 0.10.2). The versions come from the environment, so the framework decides them
#: (build.py::dependency_pins); a project that pins everything itself makes this a
#: no-op. Every project needs it -- LeviLamina's own recipe just as much as an
#: upstream plugin's, which is why it lives here and not in one plugin.
PIN_BLOCK = """        do
            -- BuildCache: pin what this project leaves unpinned to the SDK of its
            -- own era (the map comes from the environment; usually empty).
            local pins = os.getenv("BUILDCACHE_PINS")
            if pins and os.isfile("xmake.lua") then
                local data = io.readfile("xmake.lua")
                local updated = data
                for name, version in pins:gmatch("([%w_%-%.]+)=([^,;]+)") do
                    local pattern = 'add_requires%("' .. name .. '"'
                    local pinned = 'add_requires("' .. name .. " " .. version .. '"'
                    updated = updated:gsub(pattern, pinned)
                end
                if updated ~= data then
                    io.writefile("xmake.lua", updated)
                    print("BuildCache: pinned the open SDK versions: " .. pins)
                end
            end
        end"""


def pin_project_requires(recipe: Path) -> list[str]:
    """Add the block that pins the SDK versions a project leaves open.

    Idempotent: a recipe that already carries it is left alone, because the same
    snapshot is prepared more than once (the dependency pass prepares it again).
    """
    text = util.read_text(recipe)
    if "BuildCache: pin what this project leaves unpinned" in text:
        return []
    anchor = _ON_INSTALL.search(text)
    if anchor is None:
        return []
    # After the whole line: the anchor is the start of a call whose arguments
    # continue on that line.
    line_end = text.index("\n", anchor.end())
    util.write_text(recipe, text[: line_end + 1] + "\n" + PIN_BLOCK + "\n" + text[line_end + 1 :])
    return [f"{recipe.parent.name}: pins the SDK versions it leaves open"]

#: A repository's tags, by url, for as long as this process lives: one job asks
#: about the same repository for every version of every project it builds.
_TAGS: dict[str, dict[str, str]] = {}


def _tags_of(url: str) -> dict[str, str]:
    """``{tag: commit}`` for a repository, with annotated tags resolved."""
    if url in _TAGS:
        return _TAGS[url]
    safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in url)[-120:]
    cache = util.cache_dir({}, "gittags") / f"{safe}.json"
    found = util.read_json(cache, None)
    if found is None:
        found = {}
        env = util.subprocess_env(util.load_config(), proxy=util.proxy_url())
        listing = util.run_out(["git", "ls-remote", "--tags", url], env=env, check=False)
        for line in listing.splitlines():
            parts = line.split()
            if len(parts) != 2 or not parts[1].startswith("refs/tags/"):
                continue
            tag = parts[1][len("refs/tags/"):]
            # An annotated tag lists the tag object and the commit it points at
            # ("^{}"): the commit is what can be fetched and checked out.
            if tag.endswith("^{}"):
                found[tag[:-3]] = parts[0]
            else:
                found.setdefault(tag, parts[0])
        util.write_json(cache, found)
    _TAGS[url] = found
    return found


def backfill_commits(root: Path) -> list[str]:
    """Give a version declared without a commit the one its upstream tag has.

    A recipe may declare a version alone (``add_versions("0.12.1")``), and then
    xmake has nothing to check out: it clones the repository *by tag*, and for one
    that tags its releases with a "v" ("v0.12.1" against the recipe's "0.12.1")
    that clone fails before a single file is compiled --

        fatal: Remote branch 0.12.1 not found in upstream origin

    which says nothing about the recipe, and hides whatever else the build would
    have reported. The commit comes from the repository's own tags (asked once per
    repository, cached on disk); a version whose tag cannot be found is left
    exactly as it was, because a guess here is worse than the clone failure.
    """
    notes: list[str] = []
    for recipe in sorted(Path(root).glob("packages/*/*/xmake.lua")):
        text = util.read_text(recipe)
        missing = [
            match
            for match in _ADD_VERSIONS_ONE.finditer(text)
            if ":" not in match.group(1)
        ]
        if not missing:
            continue
        urls = [url for url in _ADD_URLS.findall(text) if "://" in url and url.endswith(".git")]
        if not urls:
            continue
        tags = _tags_of(urls[0])
        if not tags:
            # Silent so far, which is how a lookup that returns nothing looks the
            # same as "the recipe was already fine" -- and it is not: xmake will
            # clone by tag and fail in a way that says nothing about this.
            util.warn(
                f"{recipe.parent.name}: cannot read the tags of {urls[0]}; "
                f"versions without a commit are left as they are"
            )
            continue
        updated = text
        for match in missing:
            version = match.group(1)
            commit = tags.get(version) or tags.get("v" + version)
            if not commit:
                util.warn(f"{recipe.parent.name}: no tag for {version} in {urls[0]}")
                continue
            updated = updated.replace(
                match.group(0), f'add_versions("{version}", "{commit}")'
            )
            notes.append(
                f"{recipe.parent.name}: {version} pinned to {commit[:12]} (from its tag)"
            )
        if updated != text:
            util.write_text(recipe, updated)
    return notes


def declare_version(prepared: dict, package: str, version: str, commit: str) -> list[str]:
    """Make a snapshot's recipe declare the version it is being built for.

    A snapshot is taken *before* the version's release -- that is what a cutoff
    date means -- and the release is usually the very commit that adds the version
    to the recipe. The snapshot then declares every earlier version and not this
    one, and xmake, finding no source for a version of a git package, falls back to
    fetching it by *tag*: these projects tag with a "v" ("v0.12.1" against the
    recipe's "0.12.1"), so the fetch dies with

        fatal: Remote branch 0.12.1 not found in upstream origin

    before a single line is compiled -- and it says nothing about the recipe being
    older than the release. The commit added here is the one the recipe of today
    resolves that version to, i.e. exactly what the release commit wrote.
    """
    notes: list[str] = []
    for name, (directory, _sha) in (prepared or {}).items():
        recipe = Path(directory) / "packages" / package[0] / package / "xmake.lua"
        if not recipe.is_file():
            continue
        text = util.read_text(recipe)
        declared = _declared_commit(recipe, text, version)
        if declared:
            # The version is there but points somewhere else: the recipe of that
            # moment was written before the release moved, and its commit is the
            # source *before* the fix the release itself carries. `more-dimensions`
            # 0.2.1 is the example -- its era recipe says 82b58e1 while the tag (and
            # today's recipe) say 0b79e6a, and 82b58e1 is the commit whose own
            # "fix: fix compile" successor it is: the build then failed inside
            # MoreDimensions' own sources, in code its release does not have.
            if declared != commit:
                updated = _rewrite_commit(recipe, text, version, declared, commit)
                if updated:
                    notes.append(
                        f"{name}: {package} {version} moved {declared[:12]} -> {commit[:12]} "
                        f"(the recipe of today resolves it there)"
                    )
            continue
        listing = _ADD_VERSIONFILES.search(text)
        listing_file = recipe.parent / listing.group(1) if listing else None
        if listing_file is not None and listing_file.is_file():
            body = util.read_text(listing_file).rstrip("\n")
            util.write_text(listing_file, f"{body}\n{version} {commit}\n")
            notes.append(
                f"{name}: {package} {version} added to {listing.group(1)} "
                f"(the snapshot predates the release)"
            )
            continue
        rows = list(_ADD_VERSIONS.finditer(text))
        line = f'    add_versions("{version}", "{commit}")'
        if rows:
            end = text.find("\n", rows[-1].start())
            end = len(text) if end < 0 else end
            updated = text[:end] + "\n" + line + text[end:]
        else:
            updated = text.rstrip("\n") + "\n" + line + "\n"
        util.write_text(recipe, updated)
        notes.append(
            f"{name}: {package} {version} declared (the snapshot predates the release)"
        )
    return notes


def _declares_version(recipe: Path, text: str, version: str) -> bool:
    """Does this recipe already offer ``version``, inline or in a version file?"""
    for match in _ADD_VERSIONS.finditer(text):
        if match.group(2) == version:
            return True
    for match in _ADD_VERSIONFILES.finditer(text):
        listing = recipe.parent / match.group(1)
        if not listing.is_file():
            continue
        for line in util.read_text(listing).splitlines():
            parts = line.split()
            if parts and parts[0] == version:
                return True
    return False


def _declared_commit(recipe: Path, text: str, version: str) -> str:
    """The commit this recipe resolves ``version`` to; "" when it declares none."""
    for match in _VERSION_COMMIT.finditer(text):
        if match.group(1) == version:
            return match.group(2)
    for match in _ADD_VERSIONFILES.finditer(text):
        listing = recipe.parent / match.group(1)
        if not listing.is_file():
            continue
        for line in util.read_text(listing).splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[0] == version:
                return parts[1].split(":", 1)[-1]
    return ""


def _rewrite_commit(recipe: Path, text: str, version: str, old: str, new: str) -> bool:
    """Point a declared version at another commit. Returns whether anything moved."""
    updated = text.replace(
        f'add_versions("{version}", "{old}")', f'add_versions("{version}", "{new}")', 1
    )
    if updated != text:
        util.write_text(recipe, updated)
        return True
    for match in _ADD_VERSIONFILES.finditer(text):
        listing = recipe.parent / match.group(1)
        if not listing.is_file():
            continue
        original = util.read_text(listing)
        for separator in (" ", "\t"):
            if f"{version}{separator}{old}" in original:
                util.write_text(
                    listing, original.replace(f"{version}{separator}{old}", f"{version}{separator}{new}", 1)
                )
                return True
    return False


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


def plugin_repairs(config: dict, building, source_name: str, snapshot: Path) -> list[str]:
    """Every plugin gets to repair the snapshot, not only the one being built.

    A project is built *from* this snapshot together with its dependencies, and
    the plugin that knows how a package has to be repaired is not necessarily the
    one being built: `bsci` builds LeviLamina, and the era data packages LL needs
    (four-component versions such as `1.21.0.3-server`, which xmake's own parser
    rejects) are repaired by the LeviLamina plugin. Repairing them only when LL
    itself is the project left `bsci 0.1.6` unable to create
    `bedrock_runtime_api.lib` -- a failure that says nothing about its cause.

    Each plugin decides for itself which packages are its business, and the
    repairs are guarded, so a snapshot repaired twice is unchanged.

    The plugin being built goes *first*, and it has to run: it is the one that
    knows how this project's own recipe has to be prepared. It used to be filtered
    out -- the "already seen" set was seeded with its name, so the first candidate
    of the loop was always skipped -- and the visible effect was that a project
    pinning no SDK version (`bsci` >= 0.3.0 asks for a bare `levilamina`) was built
    against today's newest SDK instead of the one of its era: the SDK then got
    compiled a second time inside the dependent's job and packed and uploaded
    again, and the artifact itself was built against headers its sources never
    saw.
    """
    from . import plugins

    order = plugins.load_all(config)
    if building is not None:
        # The building plugin is normally in that list too; keep it first, and
        # keep it even when it is not (a probe builds a plugin that its config may
        # not list yet).
        order = [building] + [
            item for item in order if getattr(item, "name", None) != getattr(building, "name", None)
        ]
    seen: set = set()
    notes: list[str] = []
    # A repair is best effort by definition: it exists to make a package build at
    # all, so one that fails on a snapshot it does not recognise is reported and
    # the build goes on. Failing the whole round here would turn "this plugin
    # knows how to fix that project" into "nothing can be built".
    for candidate in order:
        if getattr(candidate, "name", None) in seen:
            continue
        seen.add(candidate.name)
        try:
            notes += plugin_repair(config, candidate, source_name, snapshot)
        except Exception as error:  # noqa: BLE001 - a repair must not fail a build
            util.warn(f"plugin {candidate.name}: repairing {source_name} failed: {error}")
    return notes


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
