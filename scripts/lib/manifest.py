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


def entries_for(artifacts: list[dict]) -> dict[str, dict]:
    """Manifest entries for published artifacts: each one, plus its aliases.

    An alias is the same archive under another consumer toolchain -- same url,
    same checksum, a build id recomputed for that toolchain -- so it becomes one
    more entry rather than one more artifact.
    """
    found: dict[str, dict] = {}
    for artifact in artifacts:
        found[artifact["key"]] = entry(
            artifact["url"], artifact["sha256"], artifact.get("toolset")
        )
        for alias in artifact.get("aliases") or []:
            found[alias["key"]] = entry(
                artifact["url"],
                artifact["sha256"],
                alias.get("toolset") or artifact.get("toolset"),
            )
    return found


def _reconciled(artifacts: list[dict], digests: dict[str, dict] | None) -> list[dict]:
    """Artifacts whose checksum is the one the release actually serves.

    Two jobs of a round share their dependency closure, so the same asset name is
    packed twice and uploaded twice, and the second upload wins. A build result
    then carries the digest of the upload that *lost* -- and xmake does not fall
    back to compiling when a checksum does not match, it fails the whole configure
    ("unmatched checksum"). So the digest comes from the release, and an artifact
    the release does not serve is left out entirely: an entry pointing at nothing
    fails the same way, while no entry means "build it", which always works.
    """
    if digests is None:
        return artifacts
    found = []
    for artifact in artifacts:
        info = digests.get(artifact.get("asset") or "")
        if info is None:
            # The release does not serve it at all.
            continue
        found.append(
            dict(
                artifact,
                # Which upload won is not recorded anywhere else, and an older
                # asset may predate the API's digest field: fall back to ours.
                sha256=info.get("sha256") or artifact.get("sha256"),
                size=info.get("size") or artifact.get("size"),
            )
        )
    return found


def write_repo(
    index_root: Path,
    repo_root: Path,
    *,
    revision: int | None = None,
    digests: dict[str, dict] | None = None,
) -> dict[str, set[str]]:
    """Write a manifest for every artifact the ledger holds.

    This is the generator behind both sides of a round: the consumer repository
    that ``finalize`` publishes, and the repository a *build* gives xmake so that
    its own dependencies install from the cache. The two must agree, which is why
    they are the same code and not two loops that look alike.

    ``revision`` restricts what is written to entries of that build revision. A
    build passes it, because a bumped revision means "everything built before me is
    stale" -- offering the older artifact would silently undo that. ``finalize``
    passes nothing: the artifacts that *are* published stay published, whatever
    revision produced them.

    A dependency is looked up by name in *every* repository xmake has loaded
    (``artifacts_manifest()`` scans them in order), so handing a build these
    manifests is what stops it from compiling the closure again: LeviLamina is a
    dependency of each of its plugins, and without this every plugin's round
    rebuilds the whole SDK.

    Returns ``{package: {version, ...}}``, i.e. what was written, so a caller that
    owns the destination (``finalize``) can prune whatever it left behind.
    """
    from . import index_store

    written: dict[str, set[str]] = {}
    root = Path(index_root)
    if not root.is_dir():
        return written
    for directory in sorted(root.iterdir()):
        if not directory.is_dir() or directory.name.startswith("."):
            continue
        package = directory.name
        for meta in index_store.all_version_meta(root, package):
            version = str(meta.get("version"))
            found: dict[str, dict] = {}
            for record in index_store.entries(meta).values():
                if revision is not None and int(record.get("revision") or 0) != int(revision):
                    continue
                found.update(entries_for(_reconciled(record.get("artifacts") or [], digests)))
            write_all(repo_root, package, version, found)
            if found:
                written.setdefault(package, set()).add(version)
    return written


def write_results(results, repo_root: Path, digests: dict[str, dict] | None = None) -> dict[str, set[str]]:
    """Add the artifacts of a round's build results to a manifest repository.

    A round is built by independent jobs, so one job cannot see what another one
    has just published: the archive is already on its release, but the manifest
    that points at it is only written by ``finalize``, at the end. The job that
    *depends* on it therefore builds its repository from the build results, which
    the workflow hands over as files -- same fields (key, url, sha256, toolset,
    aliases), so the result is the manifest a consumer would get, only earlier.

    Merged into whatever is already there: the ledger's entry for a package may
    carry an address this round did not build (an alias, another entry), and
    overwriting it would take a working address away.
    """
    written: dict[str, set[str]] = {}
    for result in results:
        if not isinstance(result, dict):
            continue
        for artifact in result.get("artifacts") or []:
            package = artifact.get("package")
            version = artifact.get("version")
            if not package or not version:
                continue
            fresh = entries_for(_reconciled([artifact], digests))
            if not fresh:
                continue
            found = read(repo_root, package, version)
            found.update(fresh)
            write_all(repo_root, package, version, found)
            written.setdefault(package, set()).add(version)
    return written


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
