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
    cache_cfg = config["repos"]["cache"]
    owner, repo = cache_cfg["owner"], cache_cfg["repo"]
    available = published_digests(config, owner, repo)
    problems: list[str] = []
    root = Path(index_root)
    if not root.is_dir():
        return [f"index directory missing: {root}"]
    for directory in sorted(root.iterdir()):
        if not directory.is_dir() or directory.name.startswith("."):
            continue
        for meta in index_store.all_version_meta(root, directory.name):
            for artifact in index_store.all_artifacts(meta):
                asset = artifact.get("asset")
                expected = (artifact.get("sha256") or "").lower()
                info = available.get(asset)
                if info is None:
                    problems.append(f"{asset}: not present in any release")
                    continue
                if info.get("state") and info["state"] != "uploaded":
                    problems.append(f"{asset}: release asset state is {info['state']}")
                actual = (info.get("sha256") or "").lower()
                if actual and actual != expected:
                    problems.append(
                        f"{asset}: published sha256 {actual[:12]} != index {expected[:12]}"
                    )
                elif not actual and info.get("size") and artifact.get("size"):
                    if int(info["size"]) != int(artifact["size"]):
                        problems.append(
                            f"{asset}: size {info['size']} != index {artifact['size']}"
                        )
    return problems


def verify_or_die(config: dict, index_root: Path) -> None:
    problems = check_index(config, index_root)
    if not problems:
        util.log("verify: every indexed artifact matches its published release asset")
        return
    for problem in problems:
        util.warn(problem)
    util.die(f"verify: {len(problems)} published artifact(s) do not match the index")
