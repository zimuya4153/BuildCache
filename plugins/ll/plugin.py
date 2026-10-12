"""LeviLamina SDK plugin.

The framework knows nothing about LeviLamina: this module answers *which*
versions exist, *how* each one must be built, and how its sources have to be
repaired to build at all.
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


def recipe_dir(ctx, version: str = "", commit: str = ""):
    """Where this project's recipe lives, so a round can read its dependencies."""
    try:
        source = _recipe_dir(ctx)
    except RuntimeError:
        return None
    return source / "packages" / RECIPE_PACKAGE[0] / RECIPE_PACKAGE


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

    era = xmake_env.xmake_version_for_date(
        ctx.config, commit_date(ctx, commit), floor="0.0.0"
    )
    return line_floor(era)


#: Per release line: the oldest xmake of that line that still understands the CI
#: images we can actually get. xmake 2.9.4 asks CMake for the platform toolset
#: ``v144`` when it sees MSVC 14.4x -- but v144 belongs to Visual Studio 2026 and
#: the windows-2022 image only has v143, so every CMake package dies with MSB8020.
#: 2.9.7 is the same line (same recipes, same build layout) and maps it correctly.
#: Nothing older is bumped: the 2.8 line has to stay on 2.8, because a newer xmake
#: renamed directories that those projects' own link scripts refer to.
LINE_FLOORS = {"2.9": "2.9.7"}


def line_floor(era: str) -> str:
    """Raise an era's xmake to the floor of its own release line, if there is one."""
    from lib import semver

    floor = LINE_FLOORS.get(".".join(str(era).split(".")[:2]))
    if floor and not semver.ge(era, floor):
        return floor
    return era


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
    # One build per configuration pair. Additional *toolchain* flavours are not
    # built twice: they are published as manifest aliases for the same artifact,
    # because the build hash can be recomputed offline (see toolchain_aliases).
    found = [{"id": "server", "configs": {"target_type": "server"}}]
    if client_supported(upstream_xmake_lua(ctx, commit)):
        found.append({"id": "client", "configs": {"target_type": "client"}})
    return found


#: A tag only builds a client SDK if it has the option *and* honours it. Tags
#: from the 1.0-1.7 era keep the plumbing but hardcode ``local is_client =
#: false``, and their client-only dependencies were never published, so asking
#: for ``target_type=client`` dies while resolving (``bedrockdata ...-client ...
#: not found``); older tags (0.8.x, 0.13.x) have no ``target_type`` option at all
#: and would silently build the server flavour.
_CLIENT_OPTION = re.compile(r'^\s*option\("target_type"\)\s*$', re.M)
_CLIENT_DISABLED = re.compile(r"^\s*local\s+is_client\s*=\s*false\b", re.M)


def client_supported(xmake_lua: str) -> bool:
    """Does this tag of LeviLamina really build a client SDK?"""
    text = xmake_lua or ""
    if not _CLIENT_OPTION.search(text):
        return False
    return not _CLIENT_DISABLED.search(text)


def toolchain_aliases(ctx, version: str, commit: str) -> list[dict]:
    """Extra consumer profiles this project's artifacts can be aliased for.

    LeviLamina plugin projects resolve packages under different toolchains
    (msvc by default, clang-cl when the project asks for it). The artifact
    itself is ABI compatible across those, and its build id is a *hash of the
    configuration*, so serving the other profile is a matter of publishing one
    more manifest entry with the hash recomputed for that profile — not of
    building everything twice.
    """
    # The list has to cover how a consumer *declares* its toolchain, because the
    # declaration is part of the build hash:
    #
    #   * nothing declared (windows defaults to msvc) -> the plain artifact
    #   * set_toolchains("msvc")                     -> toolchains=msvc
    #   * set_toolchains("clang-cl")                 -> toolchains=clang-cl
    #
    # It has to sit in the project file: `xmake f --toolchain=clang-cl` hashes
    # the same, but xmake then never checks an MSVC toolchain, and its
    # precompiled-artifact selection (which requires a checked one) is skipped.
    found = [{"id": "msvc", "toolchain": "msvc"}]
    # LeviLamina only started compiling itself with clang-cl at 26.20; older tags
    # are msvc builds, and no clang-cl plugin project exists for them. Publishing
    # that address anyway would hand a project a build its own version never had.
    if native_toolchain(ctx, version, commit) == "clang-cl":
        found.append({"id": "clang-cl", "toolchain": "clang-cl"})
    return found


def register_release_columns(table, ctx=None) -> None:
    """The columns a LeviLamina release body needs, on top of the core ones.

    These are *this project's* concepts, so they live here rather than in the
    renderer: the framework's table is only version, build hash and the artifact.
    A column whose value is empty in every row is not rendered, which is what
    keeps them out of a dependency's release body.
    """
    from lib import reports

    table.add(
        "entry",
        title="entry",
        # Beside the version: which build this is matters more to a reader than
        # the name of the file, and far more than how it was built.
        order=reports.ORDER_ENTRY,
        render=reports.code,
        note="*`entry`* is the build this artifact belongs to; a dependency is the\n"
        "same artifact either way and therefore has no entry.",
    )
    table.add(
        "compiled_with",
        title="compiled with",
        render=reports.code,
        note="*`compiled with`* is the compiler that really built the sources. It can\n"
        "differ from the toolchain the package was resolved under: LeviLamina's own\n"
        "xmake.lua forces clang-cl for its targets, so its SDK is a clang-cl build\n"
        "even though the package resolution (and therefore the build hash) uses msvc.",
    )
    table.add(
        "aliases",
        title="also addressable as",
        render=reports.listed,
        note="*`also addressable as`* lists the toolchain declarations this same\n"
        "archive answers to, as extra manifest entries: a project that declares one\n"
        "of them needs no second artifact.",
    )


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


def dependency_specs(ctx, version: str, commit: str) -> list[str]:
    """The dependency specs a *consumer* of this version resolves.

    A consumer resolves the version out of the upstream recipe -- today's one, as
    the Liteldev repository keeps it -- so the version file of that recipe is the
    list to ask for. On top of it come the dependencies only LeviLamina's own
    xmake.lua names: the project's build needs them, and our artifacts record
    them, which is how a consumer ends up installing them as well.

    This is the list the dependency pass builds with current sources and a current
    xmake, so that its addresses are the ones a consumer asks for.
    """
    specs = _recipe_specs(ctx, version)
    known = {spec.split()[0].split("[", 1)[0] for spec in specs}
    for item in extra_deps(ctx, version, commit) or []:
        name = item["name"]
        if name in known:
            continue
        known.add(name)
        specs.append(name + (f" {item['version']}" if item.get("version") else ""))
    return specs


#: Quoted strings in a version file that are dependency specs rather than values
#: of the loader around them ("server", "client", the config keys, ...).
_SPEC_LINE = re.compile(r'"([A-Za-z0-9][A-Za-z0-9_.\-]*[^\s"]*(?:\s+[^"]+)?)"')
_NOT_A_SPEC = {"server", "client", "default", "deps", "systemdeps", "target_type"}


def _recipe_specs(ctx, version: str) -> list[str]:
    """The dependency list of an upstream version file, as it is written today."""
    directory = _recipe_dir(ctx)
    name = version.replace(".", "_").replace("-", "_")
    path = f"packages/l/{RECIPE_PACKAGE}/versions/{name}.lua"
    text = repos.file_at(directory, "HEAD", path)
    if not text:
        return []
    specs: list[str] = []
    for match in _SPEC_LINE.finditer(text):
        spec = match.group(1).strip()
        if spec in _NOT_A_SPEC or spec.split()[0].split("[", 1)[0] in _NOT_A_SPEC:
            continue
        if spec not in specs:
            specs.append(spec)
    return specs


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

    Runs for every recipe snapshot before the build sees it. LeviLamina-era data
    packages (`bdslibrary`, old `bedrockdata`) declare four-component
    versions such as `1.21.3.01` which xmake's own semver parser rejects,
    making the whole repository unloadable. The repair belongs here rather than
    in the framework: rewrite those versions to a legal, ordered form
    (`1.21.3.01` -> `1.21.3-r01`), pin the upstream tag in a literal URL
    alias, and rename the matching dependency strings in our snapshot's version
    files.
    """
    return _repair_legacy_versions(snapshot) + _repair_era_sources(snapshot)


#: Headers of the old-era packages that use a type from the standard library
#: without including its header themselves: they relied on the transitive include
#: that the STL of their time happened to provide, and no STL does today (the same
#: failure with every toolset the image has). The repair is the one-line include.
_ERA_INCLUDES = {
    "libhat": [
        ("include/libhat/Process.hpp", "<string>", "std::string"),
    ],
    # The repair below spells the two tuple-like traits out itself, so the header
    # needs the names it now mentions: 0.13.x reached for MSVC STL internals and
    # got away without including their headers.
    "levilamina": [
        ("src/ll/api/base/Concepts.h", "<array>", "std::array"),
        ("src/ll/api/base/Concepts.h", "<ranges>", "std::ranges::subrange"),
    ],
}

#: The old era also reached *into* the STL: ``optional_ref::transform`` built its
#: result with ``std::_Construct_from_invoke_result_tag``, an MSVC internal that
#: newer STLs no longer expose (``C2248``, the private constructor). Constructing
#: the optional directly is the supported spelling of exactly that operation.
_ERA_EDITS = {
    "levilamina": [
        (
            "src/mc/common/wrapper/optional_ref.h",
            "std::optional<UnwrapT>{\n"
            "                    std::_Construct_from_invoke_result_tag{},\n"
            "                    std::forward<Fn>(fn),\n"
            "                    static_cast<T&>(*mPtr)\n"
            "                }",
            "std::optional<UnwrapT>{\n"
            "                    std::invoke(std::forward<Fn>(fn), static_cast<T&>(*mPtr))\n"
            "                }",
        ),
        # 0.13.x built its tuple-like notion out of MSVC STL internals. Newer STLs
        # do not expose them any more, and because this is a *header* it hits every
        # consumer that compiles against the SDK:
        #
        #     ll/api/base/Concepts.h(121): error C3878: syntax error ...
        #     ll/api/base/Concepts.h(121): error C2039: "_Is_std_array_v": is not a
        #         member of "std"
        #
        # (the syntax error is just the fallout of the unresolved name). The two
        # traits are spelled out here instead -- same notion, no STL internals --
        # and the expression that used them stays as it was.
        (
            "src/ll/api/base/Concepts.h",
            "template <class T>\n"
            "inline constexpr bool tuple_like_impl =\n"
            "    is_specialization_of_v<T, ::std::tuple> || is_specialization_of_v<T, ::std::pair> || ::std::_Is_std_array_v<T>\n"
            "    || ::std::_Is_subrange_v<T>;\n"
            "\n"
            "template <class T>\n"
            "concept TupleLike = tuple_like_impl<std::remove_cvref_t<T>> || (!Rangeable<T> && requires(T t) {\n"
            "                        std::tuple_size<std::remove_cvref_t<T>>::value;\n"
            "                        std::get<0>(t);\n"
            "                    });",
            "template <class T>\n"
            "struct is_std_array : ::std::false_type {};\n"
            "template <class T, ::std::size_t N>\n"
            "struct is_std_array<::std::array<T, N>> : ::std::true_type {};\n"
            "template <class T>\n"
            "inline constexpr bool is_std_array_v = is_std_array<::std::remove_cvref_t<T>>::value;\n"
            "\n"
            "template <class T>\n"
            "struct is_subrange : ::std::false_type {};\n"
            "template <class I, class S, ::std::ranges::subrange_kind K>\n"
            "struct is_subrange<::std::ranges::subrange<I, S, K>> : ::std::true_type {};\n"
            "template <class T>\n"
            "inline constexpr bool is_subrange_v = is_subrange<::std::remove_cvref_t<T>>::value;\n"
            "\n"
            "template <class T>\n"
            "inline constexpr bool tuple_like_impl =\n"
            "    is_specialization_of_v<T, ::std::tuple>\n"
            "    || is_specialization_of_v<T, ::std::pair> || is_std_array_v<T> || is_subrange_v<T>;\n"
            "\n"
            "template <class T>\n"
            "concept TupleLike = tuple_like_impl<std::remove_cvref_t<T>> || (!Rangeable<T> && requires(T t) {\n"
            "                        std::tuple_size<std::remove_cvref_t<T>>::value;\n"
            "                        std::get<0>(t);\n"
            "                    });",
        ),
    ],
}


def _repair_era_sources(snapshot) -> list[str]:
    """Repair the era's sources: missing includes and STL internals it reached into."""
    snapshot = Path(snapshot)
    notes: list[str] = []
    for package in {**_ERA_INCLUDES, **_ERA_EDITS}:
        recipe = snapshot / "packages" / package[0] / package / "xmake.lua"
        if not recipe.is_file():
            continue
        text = util.read_text(recipe)
        if "BuildCache: era includes" in text:
            continue
        # Every repair is conditional on the file being there at all: the paths
        # differ between versions (1.4.4 has no ``src/mc/common/wrapper``), and a
        # failing ``io.readfile`` inside a package script *raises* -- an edit the
        # version does not need would fail its install.
        lines = []
        for header, include, symbol in _ERA_INCLUDES.get(package, []):
            lines.append(
                f'        do\n'
                f'            local file = "{header}"\n'
                f'            if os.isfile(file) then\n'
                f'                local data = io.readfile(file)\n'
                f'                if data and not data:find("#include {include}", 1, true) then\n'
                f'                    io.writefile(file, "#include {include}\\n" .. data)\n'
                f'                    print("added {include} to {header} (it uses {symbol})")\n'
                f'                end\n'
                f'            end\n'
                f'        end'
            )
        for path, old, new in _ERA_EDITS.get(package, []):
            lines.append(
                f'        do\n'
                f'            local file = "{path}"\n'
                f'            if os.isfile(file) then\n'
                f'                local data = io.readfile(file)\n'
                f'                if data and data:find([==[{old}]==], 1, true) then\n'
                f"                    io.replace(file, [==[{old}]==], [==[{new}]==], {{plain = true}})\n"
                f'                    print("patched {path}")\n'
                f'                end\n'
                f'            end\n'
                f'        end'
            )
        match = re.search(r"^(\s*)on_install\(function", text, re.M)
        if not match:
            continue
        injection = "\n        -- BuildCache: era includes\n" + "\n".join(lines) + "\n"
        # Insert after the whole line: the anchor is the start of a call whose
        # arguments continue on that line.
        line_end = text.index("\n", match.end())
        util.write_text(recipe, text[: line_end + 1] + injection + text[line_end + 1 :])
        notes.append(
            f"{snapshot.name}/{package}: era source repairs injected "
            f"({len(lines)} fix(es))"
        )
    return notes


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
    """Rewrite dependency strings that point at a repaired version.

    They live in the recipe's per-version files, and -- in the oldest era -- in a
    version-to-dependencies map inside the recipe's own ``xmake.lua``. That map is
    read while the dependency is *resolved*, long before any install runs, so
    rewriting it only at install time is too late.
    """
    root = Path(snapshot) / "packages" / "l" / "levilamina"
    candidates = sorted(root.glob("versions/*.lua"))
    if (root / "xmake.lua").is_file():
        candidates.append(root / "xmake.lua")

    notes = []
    for path in candidates:
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


# The era-to-image mapping itself is shared with every other plugin that builds a
# version with the xmake of its own time (see lib/xmake_env.ERA_IMAGES).
LOCAL_IMAGEOS = "win25"


def toolchain_plan(ctx, version: str, commit: str) -> dict:
    """Era xmake plus the image/toolset that can serve it."""
    from lib import xmake_env

    era = xmake_env.xmake_version_for_date(
        ctx.config, commit_date(ctx, commit), floor="0.0.0"
    )
    runner, imageos = xmake_env.era_image(era)
    return {
        "era_xmake": era,
        "image": runner,
        "imageos": imageos,
        "vs_toolset": None,
        "llvm": needs_llvm22(version),
    }


#: LeviLamina's own build moved to LLVM 22 in August 2026 ("chore(ci): use LLVM
#: 22", first released with 26.32.0), so that toolchain is part of what those eras
#: *are* and the image the matrix names has to provide it. Older eras are built by
#: the compiler of their own time, which the image already carries: nothing is
#: downloaded for them, and nothing is compiled with a compiler their code never
#: saw.
LLVM_22_FROM = "26.32.0"


def needs_llvm22(version: str) -> bool:
    """Does this version build with LLVM 22 (anything that is not a version: yes)."""
    from lib import semver

    try:
        return semver.ge(str(version), LLVM_22_FROM)
    except ValueError:
        # A branch or alias ("main", "old"): assume the newest answers apply.
        return True


def _patch_upstream_sources(snapshot, renames: dict[str, dict[str, str]]) -> list[str]:
    """Rename legacy dependencies inside the upstream project's own xmake.lua.

    LeviLamina tags from the 0.8-0.13 era pin their data package directly in the
    project file (``add_requires("bdslibrary 1.20.61.01")``), so renaming it in
    the recipe alone leaves both sides disagreeing (``version(1.20.61.01) not
    found``, at configure time). The recipe we ship therefore rewrites those
    strings while it builds the project.
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
                f'        do\n'
                f'            local file = "xmake.lua"\n'
                f'            if os.isfile(file) then\n'
                f'                io.replace(file, "{package} {old}", '
                f'"{package} {new}", {{plain = true}})\n'
                f'            end\n'
                f'        end'
            )
            lines.append(
                f'        do\n'
                f'            local file = "xmake.lua"\n'
                f'            if os.isfile(file) then\n'
                f'                io.replace(file, "{package} v{old}", '
                f'"{package} v{new}", {{plain = true}})\n'
                f'            end\n'
                f'        end'
            )
    # The era that needs this writes ``on_install(function (package)`` -- with a
    # space -- so the anchor cannot be a literal string.
    anchor = re.search(r"^\s*on_install\(function", text, re.M)
    if not lines or anchor is None:
        return []
    injection = (
        "\n        -- BuildCache: upstream dependency renames\n"
        + "\n".join(lines)
        + "\n"
    )
    line_end = text.index("\n", anchor.end())
    util.write_text(recipe, text[: line_end + 1] + injection + text[line_end + 1 :])
    return [f"{recipe.parent.name}: upstream dependency renames injected ({len(lines)})"]
