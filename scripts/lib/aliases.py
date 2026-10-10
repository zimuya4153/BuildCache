"""One artifact, several consumer addresses.

xmake addresses a precompiled artifact by a hash that includes the toolchain the
*consumer* resolves the package with, so an artifact can only be picked up by a
consumer that compiles with the same toolchain. Projects differ (one LeviLamina
plugin uses msvc, the next clang-cl), yet the artifacts are ABI compatible across
those toolchains on a platform where they share one ABI: only the address
differs. Rebuilding the whole closure per combination would be wasteful, so a
second manifest entry pointing at the very same url is published instead, with
the hash recomputed for that profile.

Which profiles make sense is project knowledge and comes from the plugin
(``toolchain_aliases``). Everything here is *derived*: an alias is only produced
when the recomputation of the profile the artifact was really built with
reproduces the artifact's own hash. That check is what makes it safe to publish a
hash we never built -- if it fails, the recipe/configs we would hash with are not
the ones that produced the artifact, and no entry is written.
"""

from __future__ import annotations

from pathlib import Path

from . import hashprobe, manifest as manifest_lib, util


#: Toolchains that share one ABI on Windows, so an artifact built with one of
#: them is addressable by a consumer that declares the other.
MSVC_ABI_TOOLCHAINS = ("msvc", "clang-cl")


def closure_profiles(toolchain: str) -> list[dict]:
    """Consumer toolchains a *dependency* artifact can answer to.

    A dependency is built with the toolchain of the build and, on Windows, is a
    static library that a clang-cl project can link just as well as an msvc one:
    the two share the ABI. That is platform knowledge, not project knowledge, and
    it must not depend on which version of the project pulled the package in.

    The project's *own* artifact is a different story -- whether it can be served
    to a clang-cl project is project knowledge, so that one is decided by the
    plugin (``toolchain_aliases``).
    """
    if toolchain not in MSVC_ABI_TOOLCHAINS:
        return []
    return [{"id": name, "toolchain": name} for name in MSVC_ABI_TOOLCHAINS]


def for_artifact(
    ctx,
    plugin,
    sandbox,
    repos: list[tuple[str, Path]],
    plat: str,
    arch: str,
    *,
    package: str,
    version: str,
    commit: str,
    hash_dir: Path,
    toolset: str | None,
    profiles: list[dict] | None = None,
) -> list[dict]:
    """Manifest entries that make ``hash_dir`` reachable for other toolchains."""
    if profiles is None:
        profiles = plugin.call("toolchain_aliases", ctx, version, commit) or []
    if not profiles:
        return []
    manifest = Path(hash_dir) / "manifest.txt"
    if not manifest.is_file():
        util.warn(f"{package} {version}: no install manifest, cannot derive aliases")
        return []

    wanted = [{"id": hashprobe.PRIMARY}]
    for item in profiles:
        # A named toolchain is set explicitly; "no toolchain" (msvc) has to be
        # just as explicit, hence the None.
        wanted.append({"id": item["id"], "toolchains": item.get("toolchain") or None})

    hashes: dict[str, dict] = {}
    error = "no recipe candidate"
    for recipe_dir in recipe_candidates(repos, package, version):
        hashes, error = hashprobe.build_hashes(
            sandbox, package, version, recipe_dir, plat, arch, manifest, wanted,
            repo_dirs=[directory for _name, directory in repos],
        )
        if hashes and hashprobe.verify(hashes, Path(hash_dir).name):
            break
        error = (
            f"recomputation of {recipe_dir.parent.parent.name}/{recipe_dir.name} gives "
            f"{hashprobe.digest(hashes, hashprobe.PRIMARY)}, built {Path(hash_dir).name}; "
            f"input={hashprobe.input_of(hashes, hashprobe.PRIMARY) or error}"
        )
        hashes = {}
    if not hashes:
        util.warn(f"{package} {version}: alias skipped ({error})")
        return []

    found = []
    for profile in profiles:
        digest = hashprobe.digest(hashes, profile["id"])
        if not digest:
            util.warn(f"{package} {version}: no hash for alias {profile['id']}")
            continue
        if digest == Path(hash_dir).name:
            # The toolchain does not take part in this package's hash (header-only
            # or host binary), so the entry we already have serves every consumer.
            continue
        found.append(
            {
                "id": profile["id"],
                "toolchain": profile["toolchain"],
                "buildhash": digest,
                "key": manifest_lib.build_id(plat, arch, toolset, digest),
                "toolset": toolset,
            }
        )
    if found:
        util.log(
            f"{package} {version} ({Path(hash_dir).name}): "
            + ", ".join(f"{item['id']} -> {item['buildhash']}" for item in found)
        )
    return found


def recipe_candidates(
    repos: list[tuple[str, Path]], package: str, version: str
) -> list[Path]:
    """Recipe directories that could have produced this package, best first.

    xmake searches the project's own repositories before the main one, so the
    caller passes them in that order. Recipes that never mention the version are
    pushed back, but kept: the caller validates against the artifact's hash, so a
    wrong candidate is detected rather than trusted.
    """
    found: list[tuple[bool, int, Path]] = []
    for index, (_name, directory) in enumerate(repos):
        candidate = Path(directory) / "packages" / package[0].lower() / package.lower()
        if not (candidate / "xmake.lua").is_file():
            continue
        found.append((not _mentions(candidate, version), index, candidate))
    found.sort(key=lambda item: (item[0], item[1]))
    return [entry[2] for entry in found]


def _mentions(recipe_dir: Path, version: str) -> bool:
    texts = [util.read_text(recipe_dir / "xmake.lua")]
    texts += [util.read_text(path) for path in sorted(recipe_dir.glob("versions/*.txt"))]
    return any(version in text for text in texts if text)
