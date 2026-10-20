"""GitHub CLI wrapper: API access, release sharding and asset uploads."""

from __future__ import annotations

import json
import time
from pathlib import Path

from . import util


def gh(args: list, check: bool = True, config: dict | None = None) -> str:
    env = util.subprocess_env(config or util.load_config())
    result = util.run(["gh"] + list(args), capture=True, check=check, env=env, quiet=True)
    return (result.stdout or "").strip()


def api(path: str, config: dict | None = None, cache: bool = False, refresh: bool = False):
    """Call the GitHub REST API and parse the JSON response.

    ``cache`` stores the response under ``.cache/gh`` and is off by default: what
    this framework asks for (a release's assets, its digests) is exactly the state
    a round has to see fresh, and a cached listing would quietly describe the
    earlier one.
    """
    config = config or util.load_config()
    cache_file = None
    if cache:
        cache_dir = util.cache_dir(config, "gh")
        safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in path)[:120]
        cache_file = cache_dir / f"{safe}.json"
        if cache_file.is_file() and not refresh:
            return json.loads(cache_file.read_text(encoding="utf-8"))
    raw = gh(["api", path], config=config)
    data = json.loads(raw) if raw else None
    if cache_file is not None:
        util.write_json(cache_file, data)
    return data


def api_or_none(path: str, config: dict | None = None):
    """Like :func:`api` but returns None for 404 instead of failing."""
    config = config or util.load_config()
    result = util.run(
        ["gh", "api", path],
        capture=True,
        check=False,
        env=util.subprocess_env(config),
        quiet=True,
    )
    if result.returncode != 0:
        return None
    text = (result.stdout or "").strip()
    return json.loads(text) if text else None


def release_assets(owner: str, repo: str, tag: str, config: dict | None = None) -> dict | None:
    """Return {asset name: asset info} for a release, or None when it does not exist."""
    data = api_or_none(f"repos/{owner}/{repo}/releases/tags/{tag}", config=config)
    if not isinstance(data, dict) or "assets" not in data:
        return None
    return {asset["name"]: asset for asset in data["assets"]}


def ensure_release(
    owner: str,
    repo: str,
    tag: str,
    target: str | None = None,
    title: str | None = None,
    notes: str | None = None,
    config: dict | None = None,
) -> None:
    """Create the release (and its tag) on ``target`` so ``main`` stays clean."""
    if release_assets(owner, repo, tag, config=config) is not None:
        return
    config = config or util.load_config()
    title = title or release_title(tag)
    notes = notes or release_notes(tag)
    args = ["gh", "release", "create", tag, "--repo", f"{owner}/{repo}",
            "--title", title, "--notes-file", str(stage_notes(config, tag, notes))]
    if target:
        args += ["--target", target]
    util.run(
        args,
        env=util.subprocess_env(config),
        capture=True,
        check=False,
        quiet=True,
    )


def stage_notes(config: dict, tag: str, notes: str) -> Path:
    """Write a release body to a file for ``gh`` to read.

    A shard's body is a table that grows with every version it caches, and
    Windows refuses a command line past roughly 32k characters (``WinError
    206: The filename or extension is too long``) -- which is exactly how the
    finalize step of a full round died once the LL shard reached fifteen
    versions. ``--notes-file`` has no such limit, so the body never travels
    through argv.
    """
    directory = util.ensure_dir(util.sandbox_root(config) / "notes")
    path = directory / f"{tag}.md"
    util.write_text(path, notes)
    return path


def asset_info(asset: dict | None) -> dict:
    """``{sha256, size}`` for a release asset, empty when GitHub reports none."""
    if not asset:
        return {}
    digest = str(asset.get("digest") or "")
    return {
        "sha256": digest.split(":", 1)[1] if ":" in digest else digest,
        "size": asset.get("size"),
    }


def upload_asset(
    owner: str,
    repo: str,
    tag: str,
    path: Path,
    config: dict | None = None,
    replace: bool = False,
) -> dict:
    """Attach an archive to its shard; return what consumers will download.

    The name *is* the identity -- package, build hash and MSVC generation -- and
    two builds of one identity can still differ byte for byte (another era's
    xmake, a round that re-packs a shared dependency). Replacing the published
    file leaves the index and every manifest naming the previous checksum until
    the next finalize, and a consumer that downloads in between fails with
    ``unmatched checksum``. So the archive that is already published wins: a
    caller that builds it again adopts that digest instead of overwriting it.

    ``replace`` is for the deliberate case -- a revision bump exists to publish
    the bytes of a new framework revision -- and only then may an existing name
    be clobbered.

    A round also shares its dependencies with the rounds around it, so an upload
    can meet a shard another job is creating at that very moment ("HTTP 404" on
    the upload endpoint), a flaky connection, or the same artifact that job has
    just published. Re-resolve the shard and try again rather than fail a build
    that has already compiled everything.
    """
    config = config or util.load_config()
    local = {"sha256": util.sha256_file(path), "size": Path(path).stat().st_size}
    if not replace:
        published = asset_info((release_assets(owner, repo, tag, config=config) or {}).get(path.name))
        if published.get("sha256"):
            if published["sha256"] != local["sha256"]:
                util.warn(
                    f"{tag}: {path.name} is already published with a different archive "
                    f"({published['sha256'][:8]} vs this build's {local['sha256'][:8]}); "
                    "keeping the published one"
                )
            return published

    env = util.subprocess_env(config)
    args = ["gh", "release", "upload", tag, str(path), "--repo", f"{owner}/{repo}"]
    if replace:
        args.append("--clobber")
    attempts = 3
    result = None
    for attempt in range(1, attempts + 1):
        result = util.run(args, env=env, capture=True, check=False, quiet=True)
        if result.returncode == 0:
            return local
        # Another job may have published this very artifact while this one was
        # packing it. Its bytes are the ones the manifests will name.
        published = asset_info((release_assets(owner, repo, tag, config=config) or {}).get(path.name))
        if published.get("sha256"):
            if published["sha256"] != local["sha256"]:
                util.warn(
                    f"{tag}: {path.name} went online while this job was uploading it "
                    f"({published['sha256'][:8]}); keeping the published one"
                )
            return published
        util.warn(
            f"{tag}: upload of {path.name} failed (attempt {attempt}/{attempts}): "
            + (result.stderr or result.stdout or "").strip().splitlines()[-1][:160]
        )
        if attempt < attempts:
            # The shard may simply have been (re)created by another job.
            ensure_release(owner, repo, tag, target=target_of(config), config=config)
            time.sleep(3 * attempt)
    raise RuntimeError(f"{tag}: could not upload {path.name}: {result.stderr or result.stdout}")


def target_of(config: dict) -> str | None:
    return (config.get("release") or {}).get("targetBranch")


def asset_url(owner: str, repo: str, tag: str, name: str) -> str:
    return f"https://github.com/{owner}/{repo}/releases/download/{tag}/{name}"


def release_title(tag: str) -> str:
    """Release titles carry only the library name, the shard id lives in the body."""
    library, _, shard = tag.rpartition("_")
    return library or tag


def release_notes(tag: str, rows: list[dict] | None = None, table=None) -> str:
    """Release bodies carry the metadata that no longer lives in asset names.

    The columns come from :mod:`lib.reports`: the framework supplies the mandatory
    core and the project's plugin registers whatever else its packages need, so
    nothing project specific is hardcoded here.
    """
    from . import reports

    config = util.load_config()
    cache = (config.get("repos") or {}).get("cache") or {}
    source = ""
    if cache.get("owner") and cache.get("repo"):
        source = f"https://github.com/{cache['owner']}/{cache['repo']}"
    lines = [
        "Prebuilt SDK artifacts published by "
        + (f"[BuildCache]({source})." if source else "BuildCache."),
        "",
        f"- Package: `{release_title(tag)}`",
        f"- Index id: `{tag}`",
    ]
    if rows:
        table = table or reports.Table()
        lines += table.render(rows, title="Cached builds")
    return "\n".join(lines)


def update_release_notes(
    owner: str, repo: str, tag: str, notes: str, config: dict | None = None
) -> bool:
    """Replace a release body. Returns whether the shard accepted it."""
    config = config or util.load_config()
    path = stage_notes(config, tag, notes)
    result = util.run(
        ["gh", "release", "edit", tag, "--repo", f"{owner}/{repo}", "--notes-file", str(path)],
        env=util.subprocess_env(config),
        capture=True,
        check=False,
        quiet=True,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        util.warn(f"{tag}: could not refresh the release body: {detail[-1][:160] if detail else ''}")
        return False
    return True


def assign_release(
    package: str,
    asset_names: list[str],
    owner: str,
    repo: str,
    max_assets: int,
    config: dict | None = None,
) -> str:
    """Find the first ``<package>_<n>`` shard that can host the given assets.

    Idempotent: when every asset already exists in a shard, that shard is reused.
    """
    wanted = set(asset_names)
    index = 0
    while index < 1000:
        tag = f"{package}_{index}"
        existing = release_assets(owner, repo, tag, config=config)
        if existing is not None and wanted.issubset(existing.keys()):
            return tag
        if existing is None or len(existing) + len(asset_names) <= max_assets:
            return tag
        index += 1
    raise RuntimeError(f"no release slot available for {package}")
