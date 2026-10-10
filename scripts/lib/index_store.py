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
    legacy = meta.get("artifacts")
    if legacy:
        return {
            "default": {
                "configs": {"target_type": "server"},
                "built_at": meta.get("built_at"),
                "xmake_version": meta.get("xmake_version"),
                "toolchain": meta.get("toolchain"),
                "vs_toolset": meta.get("vs_toolset"),
                "artifacts": legacy,
            }
        }
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
    by_key = {item["key"]: item for item in previous.get("artifacts") or []}
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


def write_version(index_root: Path, package: str, version: str, meta: dict) -> None:
    """Store/merge one built version; artifacts are merged by manifest key."""
    path = package_dir(index_root, package) / "versions" / f"{version}.json"
    previous = util.read_json(path, None) or {}
    merged = dict(meta)
    if previous.get("artifacts"):
        by_key = {item["key"]: item for item in previous["artifacts"]}
        for item in meta.get("artifacts", []):
            by_key[item["key"]] = item
        merged["artifacts"] = sorted(by_key.values(), key=lambda item: item["key"])
        for field in ("built_at",):
            if previous.get(field) and merged.get(field):
                continue
    util.write_json(path, merged)
    index = load_package(index_root, package)
    versions = set(index.get("versions") or [])
    versions.add(version)
    ordered = semver.sort_desc(versions)
    index["package"] = package
    index["versions"] = ordered
    index["latest"] = ordered[0] if ordered else None
    util.write_json(package_dir(index_root, package) / "index.json", index)


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
