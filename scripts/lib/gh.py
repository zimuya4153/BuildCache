"""GitHub CLI wrapper: API access, release sharding and asset uploads."""

from __future__ import annotations

import json
from pathlib import Path

from . import util


def gh(args: list, check: bool = True, config: dict | None = None) -> str:
    env = util.subprocess_env(config or util.load_config())
    result = util.run(["gh"] + list(args), capture=True, check=check, env=env, quiet=True)
    return (result.stdout or "").strip()


def api(path: str, config: dict | None = None, cache: bool = True, refresh: bool = False):
    """Call the GitHub REST API and parse the JSON response (cached on disk)."""
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


def file_content(repo: str, path: str, ref: str, config: dict | None = None) -> str | None:
    """Read a text file from a repository, returning None when absent."""
    config = config or util.load_config()
    cache_dir = util.cache_dir(config, "ghfile")
    safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in f"{repo}_{path}_{ref}")[:150]
    cache_file = cache_dir / f"{safe}.txt"
    if cache_file.is_file():
        text = cache_file.read_text(encoding="utf-8")
        return None if text == "\0" else text
    result = util.run(
        ["gh", "api", f"repos/{repo}/contents/{path}?ref={ref}",
         "-H", "Accept: application/vnd.github.raw"],
        capture=True,
        check=False,
        env=util.subprocess_env(config),
        quiet=True,
    )
    text = result.stdout if result.returncode == 0 else None
    util.write_text(cache_file, text if text is not None else "\0")
    return text


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
    title = title or release_title(tag)
    notes = notes or release_notes(tag)
    args = ["gh", "release", "create", tag, "--repo", f"{owner}/{repo}",
            "--title", title, "--notes", notes]
    if target:
        args += ["--target", target]
    util.run(
        args,
        env=util.subprocess_env(config or util.load_config()),
        capture=True,
        check=False,
        quiet=True,
    )


def upload_asset(owner: str, repo: str, tag: str, path: Path, config: dict | None = None) -> None:
    util.run(
        ["gh", "release", "upload", tag, str(path), "--repo", f"{owner}/{repo}", "--clobber"],
        env=util.subprocess_env(config or util.load_config()),
        capture=True,
        quiet=True,
    )


def asset_url(owner: str, repo: str, tag: str, name: str) -> str:
    return f"https://github.com/{owner}/{repo}/releases/download/{tag}/{name}"


def release_title(tag: str) -> str:
    """Release titles carry only the library name, the shard id lives in the body."""
    library, _, shard = tag.rpartition("_")
    return library or tag


def release_notes(tag: str, rows: list[dict] | None = None, prefix: str = "") -> str:
    """Release bodies carry the metadata that no longer lives in asset names."""
    lines = [
        "Prebuilt SDK artifacts published by [BuildCache]"
        "(https://github.com/zimuya4153/BuildCache).",
        "",
        f"- Package: `{release_title(tag)}`",
        f"- Index id: `{tag}`",
    ]
    if prefix:
        lines.append(f"- Package prefix: `{prefix}`")
    if rows:
        lines += [
            "",
            f"Cached builds ({len(rows)}):",
            "",
            "| version | variant | toolchain | toolset | build hash | asset |",
            "|---|---|---|---|---|---|",
        ]
        for row in rows:
            lines.append(
                "| `{version}` | `{variant}` | `{toolchain}` | `{toolset}` | "
                "`{buildhash}` | `{asset}` |".format(
                    version=row.get("version", ""),
                    variant=row.get("variant", ""),
                    toolchain=row.get("toolchain", ""),
                    toolset=row.get("toolset") or "-",
                    buildhash=row.get("buildhash", ""),
                    asset=row.get("asset", ""),
                )
            )
    return "\n".join(lines)


def update_release_notes(owner: str, repo: str, tag: str, notes: str, config: dict | None = None) -> None:
    util.run(
        ["gh", "release", "edit", tag, "--repo", f"{owner}/{repo}", "--notes", notes],
        env=util.subprocess_env(config or util.load_config()),
        capture=True,
        check=False,
        quiet=True,
    )


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
