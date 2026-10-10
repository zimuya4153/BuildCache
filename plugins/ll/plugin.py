"""LeviLamina SDK plugin.

The framework knows nothing about LeviLamina: this module answers *which*
versions exist, *how* each one must be built and *which* recipes have to be
vendored under our own package prefix.
"""

from __future__ import annotations

import re

from lib import repos, semver

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
    """The xmake of that era: newest release published before the LL commit."""
    from lib import xmake_env

    return xmake_env.xmake_version_for_date(ctx.config, commit_date(ctx, commit))


def recipe_cutoff(ctx, version: str, commit: str) -> str:
    """Cutoff used when ``recipes.snapshotMode`` is ``version``."""
    return commit_date(ctx, commit)


def git_cutoff(ctx, version: str, commit: str) -> str:
    """Date every unpinned git clone inside the sandbox is rewound to."""
    offset = int(ctx.config.get("gitShim", {}).get("offsetDays", 0))
    iso = commit_date(ctx, commit)
    if offset == 0:
        return iso
    from datetime import datetime, timedelta, timezone

    parsed = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return (parsed + timedelta(days=offset)).isoformat()


def configs(ctx, version: str, commit: str) -> dict:
    """Package configs used for every cached artifact of this version."""
    return {"target_type": "server"}


def variants(ctx, version: str, commit: str) -> list[dict]:
    """LeviLamina builds a *server* and a *client* SDK; both are packaged.

    The variant is part of the package configs, so each one gets its own build
    hash and therefore its own set of artifacts.
    """
    return [
        {"name": "server", "configs": {"target_type": "server"}},
        {"name": "client", "configs": {"target_type": "client"}},
    ]


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
