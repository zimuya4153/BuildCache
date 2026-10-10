"""LeviLamina SDK plugin.

The framework knows nothing about LeviLamina: this module answers *which*
versions exist, *how* each one must be built and *which* recipes have to be
vendored under our own package prefix.
"""

from __future__ import annotations

import re
from pathlib import Path

from lib import repos, semver, util

# Where the LL recipe lives upstream (LiteLDev's package repository).
RECIPE_SOURCE = "levimc-repo"
RECIPE_PACKAGE = "levilamina"
RECIPE_VERSIONS_FILE = "packages/l/levilamina/versions/versions.txt"

# The project whose releases define our cache entries.
UPSTREAM_MIRROR = "levilamina"
UPSTREAM_URL = "https://github.com/LiteLDev/LeviLamina.git"
UPSTREAM_REPO = "LiteLDev/LeviLamina"

# Packages that are only referenced from LeviLamina's own xmake.lua. Old LL
# versions never pinned them, so we track them explicitly to complete the
# closure that consumers receive precompiled.
EXTRA_PACKAGES = [
    "mimalloc",
    "cpr",
    "libhat",
    "demangler",
    "preloader",
    "trampoline",
    "symbolprovider",
]

_REQUIRE_PATTERN = re.compile(r"add_requires\(\s*\"([^\"]+)\"")


def metadata() -> dict:
    return {
        "id": "ll",
        "display": "LeviLamina",
        "package": RECIPE_PACKAGE,
        "prefix_package": True,
        "upstream": UPSTREAM_REPO,
        "extra_packages": EXTRA_PACKAGES,
    }


def _upstream_dir(ctx):
    return ctx.mirror(UPSTREAM_MIRROR, UPSTREAM_URL)


def _recipe_dir(ctx):
    source = ctx.recipe_source(RECIPE_SOURCE)
    if source is None:
        raise RuntimeError(f"recipe source {RECIPE_SOURCE} is not configured")
    return source


def list_versions(ctx) -> list[dict]:
    """SDK versions tracked by the recipe repository, newest first.

    ``versions.txt`` is the authoritative list: LeviLamina tags that did not
    change the SDK were never added there, so they are intentionally absent.
    """
    text = repos.file_at(_recipe_dir(ctx), "HEAD", RECIPE_VERSIONS_FILE)
    entries = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            entries.append({"version": parts[0], "commit": parts[1]})
    entries.sort(key=lambda item: semver.key(item["version"]), reverse=True)
    return entries


def commit_date(ctx, commit: str) -> str:
    from lib import util

    directory = _upstream_dir(ctx)
    return util.run_out(
        ["git", "-C", str(directory), "show", "-s", "--format=%cI", commit], check=False
    ) or "1970-01-01T00:00:00Z"


def upstream_xmake_lua(ctx, commit: str) -> str:
    return repos.file_at(_upstream_dir(ctx), commit, "xmake.lua")


def toolchain(ctx, version: str, commit: str) -> str:
    """Toolchain used to *build* the cached packages.

    xmake only consults precompiled artifacts for packages resolved under an MSVC
    toolchain, and the toolchain is part of the package build hash. Building the
    cache with MSVC therefore produces artifacts that a normal consumer project
    can actually select — LeviLamina's own source is still compiled with clang-cl
    because the upstream ``xmake.lua`` forces it for its target.
    """
    return "msvc"


def native_toolchain(ctx, version: str, commit: str) -> str:
    """What upstream LeviLamina uses itself (informational, recorded in the index)."""
    text = upstream_xmake_lua(ctx, commit)
    if 'set_toolchains("clang-cl")' in text or "set_toolchains('clang-cl')" in text:
        return "clang-cl"
    return "msvc"


def xmake_version(ctx, version: str, commit: str) -> str:
    """The xmake of that era: newest release published before the LL commit.

    Deliberately *not* clamped to a recent release: an old LeviLamina tag was
    written against the xmake of its own time. Mixing eras breaks builds in ways
    that are invisible in a diff — for example xmake renamed ``buildir`` to
    ``builddir`` at some point, and both the generated configs and the link
    scripts (levibuildscript) refer to that directory name, so a modern xmake
    driving an old recipe simply cannot agree with it.
    """
    from lib import xmake_env

    return xmake_env.xmake_version_for_date(
        ctx.config, commit_date(ctx, commit), floor="0.0.0"
    )


def recipe_cutoff(ctx, version: str, commit: str) -> str:
    """Date the recipe repositories are rolled back to for this build.

    The whole point of the git interception is to rebuild a version against the
    ecosystem it was released into: the recipes, the recipes' own dependency
    pins and every unpinned clone have to match that date. A small offset keeps
    a build from landing just before the recipe for that very release existed.
    """
    offset = int(ctx.config.get("recipes", {}).get("cutoffOffsetDays", 3))
    return _shift(commit_date(ctx, commit), offset)


def git_cutoff(ctx, version: str, commit: str) -> str:
    """Date every unpinned git clone inside the sandbox is rewound to."""
    offset = int(ctx.config.get("gitShim", {}).get("offsetDays", 3))
    return _shift(commit_date(ctx, commit), offset)


def _shift(iso: str, days: int) -> str:
    if not days:
        return iso
    from datetime import datetime, timedelta

    parsed = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return (parsed + timedelta(days=days)).isoformat()


def configs(ctx, version: str, commit: str) -> dict:
    """Package configs used for every cached artifact of this version."""
    return {"target_type": "server"}


def entries(ctx, version: str, commit: str) -> list[dict]:
    """LeviLamina builds a *server* and a *client* SDK; both are packaged.

    This is project knowledge, so it lives here and not in the framework: the
    server/client split is reflected in the package configs, which gives each
    build its own hash and therefore its own set of artifacts.
    """
    entries = []
    for target_type in ("server", "client"):
        for toolchain in ("msvc", "clang-cl"):
            entries.append(
                {
                    "id": f"{target_type}-{toolchain}",
                    "configs": {"target_type": target_type},
                    "toolchain": toolchain,
                    "fields": {"target_type": target_type, "toolchain": toolchain},
                }
            )
    return entries


def extra_deps(ctx, version: str, commit: str) -> list[dict]:
    """Dependencies only named by LeviLamina's own xmake.lua."""
    text = upstream_xmake_lua(ctx, commit)
    if not text:
        return []
    wanted = set(metadata()["extra_packages"])
    found: dict[str, str] = {}
    for match in _REQUIRE_PATTERN.finditer(text):
        spec = match.group(1)
        spec = spec.split(",", 1)[0].strip()
        parts = spec.split()
        if not parts:
            continue
        name = parts[0].split("[", 1)[0].strip()
        if name not in wanted:
            continue
        version = parts[1] if len(parts) > 1 else ""
        found[name] = version.lstrip("v") if version else ""
    return [{"name": name, "version": found[name]} for name in sorted(found)]


def report_skipped(ctx, known: list[dict]) -> list[str]:
    """Upstream tags that never made it into the recipe repository."""
    from lib import util

    directory = _upstream_dir(ctx)
    known_versions = {item["version"] for item in known}
    known_commits = {item["commit"] for item in known}
    tags = util.run_out(
        ["git", "-C", str(directory), "tag", "--sort=-v:refname"], check=False
    ).splitlines()
    skipped = []
    for tag in tags:
        tag = tag.strip()
        if not tag:
            continue
        plain = tag.lstrip("v")
        if plain in known_versions:
            continue
        sha = util.run_out(
            ["git", "-C", str(directory), "rev-list", "-n", "1", tag], check=False
        )
        if sha and sha in known_commits:
            continue
        skipped.append(tag)
    return skipped

def repair_recipes(config, source_name: str, snapshot) -> list[str]:
    """Project-specific recipe repair hook.

    Runs for every recipe snapshot before vendoring. LeviLamina-era data
    packages (`bdslibrary`, old `bedrockdata`) declare four-component
    versions such as `1.21.3.01` which xmake's own semver parser rejects,
    making the whole repository unloadable. The repair belongs here rather than
    in the framework: rewrite those versions to a legal, ordered form
    (`1.21.3.01` -> `1.21.3-r01`), pin the upstream tag in a literal URL
    alias, and rename the matching dependency strings in our vendored version
    files.
    """
    return _repair_legacy_versions(snapshot)


_LEGACY_VERSION = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)\.(\d+)(.*)$")
_ADD_VERSIONS = re.compile(r'add_versions\(\s*"([^"]+)"\s*,\s*"([^"]*)"\s*\)')
_ADD_VERSIONFILES = re.compile(r'add_versionfiles\(\s*"([^"]+)"\s*\)')
_ADD_URLS = re.compile(r'add_urls\(\s*"([^"]+)"')


def _legal_version(version: str) -> str | None:
    """``1.21.3.01`` -> ``1.21.3-r01`` (valid SemVer, keeps the ordering)."""
    match = _LEGACY_VERSION.match(version)
    if not match:
        return None
    major, minor, patch, fourth, rest = match.groups()
    return f"{major}.{minor}.{patch}-r{fourth}{rest}"


def _repair_legacy_versions(snapshot) -> list[str]:
    """Make four-component versions usable again, in the recipe snapshot.

    xmake's own parser rejects versions such as ``1.21.3.01`` and refuses to
    load the whole repository. They cannot simply be renamed either, because the
    upstream release tag is part of the download URL — so each one gets a
    literal URL (the tag stays) behind its own alias, and is re-added under a
    legal name. The dependency strings in LeviLamina's own version files are
    renamed to match, which keeps the two sides consistent.
    """
    snapshot = Path(snapshot)
    notes: list[str] = []
    renames: dict[str, dict[str, str]] = {}
    for recipe in sorted(snapshot.glob("packages/*/*/xmake.lua")):
        text = util.read_text(recipe)
        templates = [url for url in _ADD_URLS.findall(text) if "$(version)" in url]
        if not templates:
            continue
        entries: dict[str, str] = {}
        for match in _ADD_VERSIONS.finditer(text):
            entries[match.group(1)] = match.group(2)
        for match in _ADD_VERSIONFILES.finditer(text):
            listing = recipe.parent / match.group(1)
            if listing.is_file():
                for line in util.read_text(listing).splitlines():
                    parts = line.split()
                    if len(parts) >= 2:
                        entries[parts[0]] = parts[1]
        mapping = {}
        shas: dict[str, str] = {}
        for version in entries:
            # Recipes may prefix a version with a url alias ("old:v1.21.0.3");
            # dependency strings in other recipes only use the plain tag, so the
            # mapping has to be keyed by the tag to keep both sides in sync.
            tag = version.split(":", 1)[-1]
            legal = _legal_version(tag)
            if legal and legal != tag:
                mapping[tag] = legal
                shas[tag] = entries[version]
        if not mapping:
            continue

        additions = []
        for index, (version, legal) in enumerate(sorted(mapping.items())):
            alias = f"legacy{index}"
            for template in templates:
                literal = template.replace("$(version)", version)
                additions.append(f'    add_urls("{literal}", {{alias = "{alias}"}})')
            additions.append(f'    add_versions("{alias}:{legal}", "{shas[version]}")')

        updated = text
        for version in mapping:
            for original in (
                version,
                f"old:{version}",
                f"new:{version}",
                f"git:{version}",
                f"release:{version}",
            ):
                updated = updated.replace(
                    f'add_versions("{original}", "{shas[version]}")', ""
                )
        updated = updated.rstrip("\n") + "\n\n    -- BuildCache: legacy versions repaired\n" + "\n".join(additions) + "\n"
        util.write_text(recipe, updated)
        for match in _ADD_VERSIONFILES.finditer(text):
            listing = recipe.parent / match.group(1)
            if not listing.is_file():
                continue
            kept = [
                line
                for line in util.read_text(listing).splitlines()
                if line.split() and line.split()[0].split(":", 1)[-1] not in mapping
            ]
            util.write_text(listing, "\n".join(kept) + "\n")
        renames[recipe.parent.name] = mapping
        notes.append(
            f"{snapshot.name}/{recipe.parent.name}: repaired {len(mapping)} legacy version(s), "
            f"e.g. {sorted(mapping.items())[0][0]} -> {sorted(mapping.items())[0][1]}"
        )

    if renames:
        notes += _rename_dependents(snapshot, renames)
        notes += _patch_upstream_sources(snapshot, renames)
    return notes


def _rename_dependents(snapshot, renames: dict[str, dict[str, str]]) -> list[str]:
    """Rewrite dependency strings that point at a repaired version."""
    notes = []
    for path in sorted(Path(snapshot).glob("packages/l/levilamina/versions/*.lua")):
        text = util.read_text(path)
        updated = text
        for package, mapping in renames.items():
            for old, new in mapping.items():
                updated = updated.replace(f'"{package} {old}"', f'"{package} {new}"')
                updated = updated.replace(f'"{package} v{old}"', f'"{package} v{new}"')
        if updated != text:
            util.write_text(path, updated)
            notes.append(f"{path.name}: dependency versions updated")
    return notes


def should_skip(ctx, version: str, commit: str) -> tuple[bool, str]:
    """Skip versions unless we are running on an image of the right generation.

    An ancient tag ships an ancient xmake, and that xmake predates support for a
    modern MSVC: it hands the STL headers a language standard they reject, so the
    *dependencies* fail with errors inside type_traits/utility. Each era therefore
    needs the CI image that carries an MSVC of the same generation, and local runs
    (no ``ImageOS``) only take the eras this machine can actually serve.
    """
    import os

    plan = toolchain_plan(ctx, version, commit)
    imageos = os.environ.get("ImageOS", "")
    if imageos and plan["imageos"] and imageos >= plan["imageos"]:
        return False, ""
    if not imageos and plan["imageos"] == LOCAL_IMAGEOS:
        return False, ""
    return True, (
        f"era xmake {plan['era_xmake']} needs a {plan['image']} image "
        f"(msvc {plan['vs_toolset'] or 'any'}); run it on CI"
    )


# Which CI image (and MSVC generation) an era's xmake can drive. Floors are
# checked newest first; "win25" means the current windows-latest image.
ERA_IMAGES = [
    # era xmake floor, runner label, GitHub ImageOS value, msvc toolset
    ("3.1.0", "windows-latest", "win25", None),
    ("3.0.0", "windows-2022", "win22", "14.38"),
    ("2.9.0", "windows-2022", "win22", "14.34"),
    ("0.0.0", "windows-2019", "win19", "14.29"),
]
LOCAL_IMAGEOS = "win25"


def toolchain_plan(ctx, version: str, commit: str) -> dict:
    """Era xmake plus the image/toolset that can serve it."""
    from lib import semver, xmake_env

    era = xmake_env.xmake_version_for_date(
        ctx.config, commit_date(ctx, commit), floor="0.0.0"
    )
    for floor, runner, imageos, toolset in ERA_IMAGES:
        if semver.ge(era, floor):
            return {
                "era_xmake": era,
                "image": runner,
                "imageos": imageos,
                "vs_toolset": toolset,
            }
    return {"era_xmake": era, "image": "windows-2019", "imageos": "win19", "vs_toolset": "14.29"}


def _patch_upstream_sources(snapshot, renames: dict[str, dict[str, str]]) -> list[str]:
    """Rename legacy dependencies inside the upstream project's own xmake.lua.

    LeviLamina tags from the 0.8-0.13 era pin their data package directly in the
    project file, so renaming it in the recipe alone leaves both sides
    disagreeing (``version(1.20.61.01) not found``). The recipe we ship therefore
    rewrites those strings while it builds the project.
    """
    recipe = Path(snapshot) / "packages" / "l" / "levilamina" / "xmake.lua"
    if not recipe.is_file():
        return []
    text = util.read_text(recipe)
    if "BuildCache: upstream dependency renames" in text:
        return []
    lines = []
    for package, mapping in renames.items():
        for old, new in mapping.items():
            lines.append(
                f'        io.replace("xmake.lua", "{package} {old}", '
                f'"{package} {new}", {{plain = true}})'
            )
            lines.append(
                f'        io.replace("xmake.lua", "{package} v{old}", '
                f'"{package} v{new}", {{plain = true}})'
            )
    anchor = "    on_install(function(package)"
    if not lines or anchor not in text:
        return []
    injection = (
        "\n        -- BuildCache: upstream dependency renames\n"
        + "\n".join(lines)
        + "\n"
    )
    util.write_text(recipe, text.replace(anchor, anchor + injection, 1))
    return [f"{recipe.parent.name}: upstream dependency renames injected ({len(lines)})"]
