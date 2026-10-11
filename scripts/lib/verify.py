"""Verify that what the index claims is what the releases actually serve.

The failure this guards against is subtle: an artifact can be re-uploaded while
the manifest keeps the old checksum. xmake then rejects the download and
*silently* falls back to compiling from source, so nothing looks broken.
"""

from __future__ import annotations

from pathlib import Path

from . import gh, index_store, util


def published_digests(config: dict, owner: str, repo: str) -> dict[str, dict]:
    """Map ``asset name -> {sha256, size, release}`` for every shard we know."""
    found: dict[str, dict] = {}
    page = 1
    while page <= 20:
        releases = gh.api(
            f"repos/{owner}/{repo}/releases?per_page=100&page={page}", config=config, cache=False
        )
        if not isinstance(releases, list) or not releases:
            break
        for release in releases:
            for asset in release.get("assets") or []:
                digest = (asset.get("digest") or "").removeprefix("sha256:")
                found[asset["name"]] = {
                    "sha256": digest or None,
                    "size": asset.get("size"),
                    "release": release.get("tag_name"),
                    "state": asset.get("state"),
                }
        if len(releases) < 100:
            break
        page += 1
    return found


def check_index(config: dict, index_root: Path) -> list[str]:
    """Return a list of problems; empty means the published state is consistent."""
    return [
        f"{item['asset']}: {item['problem']}"
        for item in check_index_entries(config, index_root)
    ]


def check_index_entries(config: dict, index_root: Path) -> list[dict]:
    """Structured version of :func:`check_index` (package/version/entry aware)."""
    cache_cfg = config["repos"]["cache"]
    owner, repo = cache_cfg["owner"], cache_cfg["repo"]
    available = published_digests(config, owner, repo)
    problems: list[dict] = []
    root = Path(index_root)
    if not root.is_dir():
        return [{"asset": "", "problem": f"index directory missing: {root}"}]
    for directory in sorted(root.iterdir()):
        if not directory.is_dir() or directory.name.startswith("."):
            continue
        for meta in index_store.all_version_meta(root, directory.name):
            version = str(meta.get("version"))
            for entry_id, entry in index_store.entries(meta).items():
                for artifact in entry.get("artifacts") or []:
                    asset = artifact.get("asset")
                    expected = (artifact.get("sha256") or "").lower()
                    info = available.get(asset)
                    problem = None
                    if info is None:
                        problem = "not present in any release"
                    elif info.get("state") and info["state"] != "uploaded":
                        problem = f"release asset state is {info['state']}"
                    else:
                        actual = (info.get("sha256") or "").lower()
                        if actual and actual != expected:
                            problem = (
                                f"published sha256 {actual[:12]} != index {expected[:12]}"
                            )
                        elif not actual and info.get("size") and artifact.get("size"):
                            if int(info["size"]) != int(artifact["size"]):
                                problem = (
                                    f"size {info['size']} != index {artifact['size']}"
                                )
                    if problem:
                        problems.append(
                            {
                                "asset": asset,
                                "package": directory.name,
                                "version": version,
                                "entry": entry_id,
                                "problem": problem,
                            }
                        )
    return problems


def adopt_published_digests(config: dict, index_root: Path) -> list[dict]:
    """Make the index describe the bytes that are actually published.

    One dependency can be built by more than one job: the server and the client
    entry of the same version share their whole dependency closure, i.e. the same
    build hash and therefore the same asset name. The archives are not byte
    identical -- packing writes no timestamps, but the object files inside carry
    their own build metadata -- so the upload clobbers and whichever job finished
    last wins.

    The loser's digest is what its build result records, so an index built from
    those results would describe a file that no longer exists -- and because
    xmake *verifies* sha256, every consumer would silently fall back to compiling
    the package from source. What is published is the truth, so adopt it.

    Returns the changes, so the caller can report them.
    """
    cache_cfg = config["repos"]["cache"]
    available = published_digests(config, cache_cfg["owner"], cache_cfg["repo"])
    root = Path(index_root)
    adopted: list[dict] = []
    if not root.is_dir():
        return adopted
    for directory in sorted(root.iterdir()):
        if not directory.is_dir() or directory.name.startswith("."):
            continue
        for meta in index_store.all_version_meta(root, directory.name):
            version = str(meta.get("version"))
            path = index_store.package_dir(root, directory.name) / "versions" / f"{version}.json"
            document = util.read_json(path, None)
            if not document:
                continue
            changed = False
            for entry in index_store.entries(document).values():
                for artifact in entry.get("artifacts") or []:
                    info = available.get(artifact.get("asset") or "") or {}
                    published = (info.get("sha256") or "").lower()
                    recorded = (artifact.get("sha256") or "").lower()
                    if not published or not recorded or published == recorded:
                        continue
                    adopted.append(
                        {
                            "asset": artifact.get("asset"),
                            "recorded": recorded,
                            "published": published,
                        }
                    )
                    artifact["sha256"] = published
                    if info.get("size"):
                        artifact["size"] = int(info["size"])
                    changed = True
            if changed:
                util.write_json(path, document)
    return adopted


def requeue(config: dict, index_root: Path, problems: list[dict]) -> list[str]:
    """Mark affected entries as stale so the next discover rebuilds them.

    The build revision is what ``discover`` compares against, so writing 0 is
    enough: the entry looks older than the current revision and gets queued
    again. This is how a deleted or replaced release asset heals itself instead
    of only being reported.
    """
    touched: list[str] = []
    grouped: dict[tuple[str, str, str], None] = {}
    for item in problems:
        if not item.get("package") or not item.get("entry"):
            continue
        grouped[(item["package"], item["version"], item["entry"])] = None
    for package, version, entry_id in grouped:
        path = Path(index_root) / package / "versions" / f"{version}.json"
        document = util.read_json(path, None)
        if not document:
            continue
        entries = index_store.entries(document)
        entry = entries.get(entry_id)
        if entry is None:
            continue
        entry["revision"] = 0
        document["entries"] = entries
        document.pop("variants", None)
        util.write_json(path, document)
        touched.append(f"{package}@{version} [{entry_id}]")
    return touched


def verify_or_die(config: dict, index_root: Path) -> None:
    problems = check_index(config, index_root)
    if not problems:
        util.log("verify: every indexed artifact matches its published release asset")
        return
    for problem in problems:
        util.warn(problem)
    util.die(f"verify: {len(problems)} published artifact(s) do not match the index")
