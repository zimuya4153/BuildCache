"""The append-only index that records what has already been published."""

from __future__ import annotations

from pathlib import Path

from . import semver, util


def package_dir(index_root: Path, package: str) -> Path:
    return Path(index_root) / package


def load_package(index_root: Path, package: str) -> dict:
    path = package_dir(index_root, package) / "index.json"
    return util.read_json(path, {"package": package, "latest": None, "versions": []})


def built_versions(index_root: Path, package: str) -> set[str]:
    return set(load_package(index_root, package).get("versions") or [])


def version_meta(index_root: Path, package: str, version: str) -> dict | None:
    path = package_dir(index_root, package) / "versions" / f"{version}.json"
    return util.read_json(path, None)


def entries(meta: dict | None) -> dict:
    """Build-entry map of a version document (default when a plugin defines none).

    Older documents used `variants` for the same thing; that key is still
    accepted so existing indexes keep working.
    """
    if not meta:
        return {}
    found = meta.get("entries") or meta.get("variants")
    if isinstance(found, dict) and found:
        return found
    return {}


def built_entries(index_root: Path, package: str, version: str) -> set[str]:
    return set(entries(version_meta(index_root, package, version)).keys())


def built_entries_at_revision(
    index_root: Path, package: str, version: str, revision: int
) -> set[str]:
    """Entries that are still current for the given build revision.

    Bumping ``revision`` in the config therefore marks every older build as
    stale, which is how a fix in a recipe or in a plugin gets re-published
    instead of being masked by "this version is already in the index".
    """
    found = set()
    for entry_id, entry in entries(version_meta(index_root, package, version)).items():
        if int(entry.get("revision") or 0) >= int(revision):
            found.add(entry_id)
    return found


def built_entries_with_profiles(
    index_root: Path, package: str, version: str, revision: int, profiles: list[str]
) -> set[str]:
    """Entries that are current for this revision *and* carry these profiles.

    The consumer profiles a plugin declares are part of what makes an entry
    current: when a plugin starts publishing another toolchain, the entries that
    were written before it existed have to be built again, and this is what
    notices. An entry is compared against the declared set rather than against
    the aliases found in its artifacts, because whether an artifact needs an
    alias at all is only known while it is being built.
    """
    found = set()
    wanted = sorted(profiles)
    for entry_id, entry in entries(version_meta(index_root, package, version)).items():
        if int(entry.get("revision") or 0) < int(revision):
            continue
        if sorted(entry.get("alias_profiles") or []) != wanted:
            continue
        found.add(entry_id)
    return found


def has_artifacts(index_root: Path, package: str, version: str, revision: int | None = None) -> bool:
    """Is there an artifact for this package version that a build could reuse?

    ``revision`` filters the same way a build's manifest repository does: only
    entries of the current revision are offered to xmake, so only those can be
    reused. A dependency that has nothing to offer is what makes a round plan the
    dependency's own build instead of letting the dependent compile it.
    """
    for entry in entries(version_meta(index_root, package, version)).values():
        if revision is not None and int(entry.get("revision") or 0) != int(revision):
            continue
        if entry.get("artifacts"):
            return True
    return False


def versions_with_artifacts(
    index_root: Path, package: str, revision: int | None = None
) -> set[str]:
    """The versions of a package the ledger can already serve.

    ``revision`` filters the same way a build's manifest repository does, so what
    this returns is what a round does *not* have to build again. ``discover`` uses
    it to tell a release line that has been built from one that has not.
    """
    found: set[str] = set()
    for meta in all_version_meta(index_root, package):
        version = str(meta.get("version"))
        for entry in entries(meta).values():
            if revision is not None and int(entry.get("revision") or 0) != int(revision):
                continue
            if entry.get("artifacts"):
                found.add(version)
                break
    return found


def entry_artifacts(index_root: Path, package: str, version: str, entry_id: str) -> list[dict]:
    entry = entries(version_meta(index_root, package, version)).get(entry_id) or {}
    return list(entry.get("artifacts") or [])


def all_artifacts(meta: dict) -> list[dict]:
    collected: list[dict] = []
    for entry in entries(meta).values():
        collected.extend(entry.get("artifacts") or [])
    return collected


def write_entry(index_root: Path, package: str, version: str, entry_id: str, meta: dict) -> None:
    """Store one built variant; artifacts are merged by manifest key."""
    path = package_dir(index_root, package) / "versions" / f"{version}.json"
    document = util.read_json(path, None) or {}
    existing = entries(document)
    previous = existing.get(entry_id) or {}
    merged = dict(meta)
    # A record only holds the artifacts of its own package: the build that
    # produced it also knows its whole closure, and storing all of it here would
    # make the project's record claim every dependency (and hand its toolchain
    # metadata to release notes about those dependencies).
    by_key = {
        item["key"]: item
        for item in previous.get("artifacts") or []
        if _owns(package, item)
    }
    for item in meta.get("artifacts", []):
        by_key[item["key"]] = item
    merged["artifacts"] = sorted(by_key.values(), key=lambda item: item["key"])
    if previous.get("built_at") and not merged.get("built_at"):
        merged["built_at"] = previous["built_at"]
    existing[entry_id] = merged
    document.update({key: value for key, value in meta.items() if key != "artifacts"})
    document["package"] = package
    document["version"] = version
    document["entries"] = existing
    document.pop("artifacts", None)
    util.write_json(path, document)
    index = load_package(index_root, package)
    versions = set(index.get("versions") or [])
    versions.add(version)
    ordered = semver.sort_desc(versions)
    index["package"] = package
    index["versions"] = ordered
    index["latest"] = ordered[0] if ordered else None
    util.write_json(package_dir(index_root, package) / "index.json", index)


def _owns(package: str, artifact: dict) -> bool:
    """Does this artifact belong in the record of ``package``?

    The ledger and the manifests are keyed by the package's own name, so a record
    only holds the artifacts of the package it is about -- the build that produced
    it knows its whole closure, which is recorded per dependency instead.
    """
    return artifact.get("package") == package


def write_single(index_root: Path, package: str, version: str, entry_id: str, meta: dict) -> None:
    """Store a package that has no build-entry dimension of its own.

    A dependency is the same artifact whichever entry of the project pulled it
    in, so it is recorded once. Anything another entry had written for it is
    dropped, which is what removes the ``server``/``client`` records that used to
    be created for every dependency simply because the project had entries.
    """
    path = package_dir(index_root, package) / "versions" / f"{version}.json"
    document = util.read_json(path, None) or {}
    document.update({key: value for key, value in meta.items() if key != "artifacts"})
    document["package"] = package
    document["version"] = version
    document["entries"] = {entry_id: meta}
    document.pop("variants", None)
    document.pop("artifacts", None)
    util.write_json(path, document)
    index = load_package(index_root, package)
    versions = set(index.get("versions") or [])
    versions.add(version)
    ordered = semver.sort_desc(versions)
    index["package"] = package
    index["versions"] = ordered
    index["latest"] = ordered[0] if ordered else None
    util.write_json(package_dir(index_root, package) / "index.json", index)


def all_version_meta(index_root: Path, package: str) -> list[dict]:
    directory = package_dir(index_root, package) / "versions"
    if not directory.is_dir():
        return []
    metas = []
    for path in sorted(directory.glob("*.json")):
        data = util.read_json(path, None)
        if data:
            metas.append(data)
    metas.sort(key=lambda item: semver.key(str(item.get("version", ""))), reverse=True)
    return metas


def summary(index_root: Path) -> str:
    root = Path(index_root)
    if not root.is_dir():
        return "(no index)"
    lines = []
    for directory in sorted(root.iterdir()):
        if not directory.is_dir() or directory.name.startswith("."):
            continue
        index = load_package(root, directory.name)
        count = len(index.get("versions") or [])
        lines.append(f"{directory.name}: {count} versions, latest {index.get('latest')}")
    return "\n".join(lines) or "(empty index)"
