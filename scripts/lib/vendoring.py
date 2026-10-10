"""Vendor upstream recipes into our own prefixed package namespace.

Consumers must never have upstream packages silently replaced, so every package
we publish lives under our own prefix (``llsdk-fmt`` instead of ``fmt``). This
module copies the upstream recipe, renames the package and rewrites the
references between packages we own. Everything we do not own keeps its original
name and keeps being resolved from the official repositories.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

from . import recipefix, repos, util

_STRING_PATTERN = re.compile(r'"((?:[^"\\]|\\.)*)"')

# Dependency references always go through one of these entry points, so the
# rewrite stays targeted instead of touching arbitrary string literals (a file
# path like "glm" must never be renamed to "mifcahce_glm").
_DEP_CALLS = ("add_deps", "add_requires", "add_packages", "set_base")
_DEP_CALL_PATTERN = re.compile(r"\b(" + "|".join(_DEP_CALLS) + r")\s*\(([^()]*)\)")
_PACKAGE_ADD_PATTERN = re.compile(r':add\(\s*"(deps|systemdeps)"\s*,\s*"([^"]+)"\s*\)')
_PACKAGE_DECL_PATTERN = re.compile(r'\bpackage\(\s*"([^"]+)"\s*\)')


def vendor(config: dict, ctx, plugin, refresh: bool = False) -> dict:
    """Regenerate the prefixed recipes inside the package repository working tree."""
    package_repo = Path(ctx.package_repo)
    package_cfg = config["repos"]["package"]
    from . import gitops

    gitops.ensure_checkout(
        config,
        f"https://github.com/{package_cfg['owner']}/{package_cfg['repo']}.git",
        package_repo,
        branch=package_cfg.get("branch"),
    )
    prefix = ctx.prefix
    target = plugin.call("metadata")["package"]
    env = util.subprocess_env(config, proxy=ctx.proxy)
    sources = _source_snapshots(config, ctx, env=env, refresh=refresh)

    owned = _collect_owned(config, ctx, plugin, sources, target)
    util.log(f"vendoring {len(owned)} packages under prefix '{prefix}': {', '.join(sorted(owned))}")

    copied: dict[str, str] = {}
    for name in sorted(owned):
        origin = _find_recipe_dir(sources, name)
        if origin is None:
            util.warn(f"no recipe for '{name}' in the configured sources; skipped")
            continue
        dest = _package_dir(package_repo, prefix + name)
        util.rmtree(dest)
        util.ensure_dir(dest.parent)
        shutil.copytree(origin, dest)
        _rewrite_tree(dest, owned, prefix)
        for suspicious in _verify_tree(dest, owned, prefix):
            util.warn(f"{prefix}{name}: dependency reference left unprefixed: {suspicious}")
        copied[name] = str(origin)

    extras = _extras_per_version(ctx, plugin, sources, owned)
    version_files = _version_files(package_repo, prefix + target)
    for path in version_files:
        extra = extras.get(path.name, [])
        if extra:
            _append_extra_deps(path, extra, prefix)

    marker = {
        "generated_at": util.run_out(["git", "-C", str(util.ROOT), "rev-parse", "HEAD"], check=False),
        "prefix": prefix,
        "sources": {name: repos.head(directory, env=env) for name, directory in sources.items()},
        "packages": copied,
        "versions_with_extras": {name: extra for name, extra in extras.items() if extra},
    }
    util.write_json(package_repo / ".buildcache-recipes.json", marker)
    return marker


def _source_snapshots(config: dict, ctx, env: dict | None = None, refresh: bool = False) -> dict[str, Path]:
    """Snapshot every configured recipe source at its current HEAD."""
    root = util.absolute(config.get("_vendor_root", ".vendor"))
    mode = str(config.get("recipes", {}).get("snapshotMode", "head"))
    env = env or util.subprocess_env(config, proxy=ctx.proxy)
    snapshots = {}
    for source in config.get("recipes", {}).get("sources", []):
        mirror = ctx.mirror(source["name"], source["url"])
        commit = repos.commit_at(mirror, env=env)
        dest = root / f"{source['name']}@{commit[:12]}"
        if mode in ("head", "version") and not dest.is_dir():
            util.log(f"snapshot {source['name']} @ {commit[:12]}")
            repos.snapshot(mirror, dest, commit=commit, env=env)
        target = dest if dest.is_dir() else mirror
        for note in recipefix.apply_fixups(config, source["name"], target):
            util.log(note)
        for note in recipefix.normalize_tree(target):
            util.log(note)
        snapshots[source["name"]] = target
    return snapshots


def _collect_owned(config: dict, ctx, plugin, sources: dict[str, Path], target: str) -> set[str]:
    owned = {target}
    recipe = _find_recipe_dir(sources, target)
    if recipe is None:
        util.die(f"recipe for '{target}' not found in the configured sources")
    for name, versioned in _dependency_names(recipe).items():
        if _find_recipe_dir(sources, name) is not None:
            owned.add(name)
        elif versioned:
            _warn_once(f"dependency '{name}' has no recipe in the configured sources")
    for name in plugin.call("metadata").get("extra_packages", []):
        if _find_recipe_dir(sources, name) is not None:
            owned.add(name)
    return owned


def _dependency_names(recipe_dir: Path) -> dict[str, bool]:
    """Map candidate package name -> whether the spec pinned a version."""
    names: dict[str, bool] = {}
    versions_dir = Path(recipe_dir) / "versions"
    files = sorted(versions_dir.glob("*.lua")) if versions_dir.is_dir() else []
    files.append(Path(recipe_dir) / "xmake.lua")
    for path in files:
        if not path.is_file():
            continue
        for content in _specs_in_file(path):
            parts = content.split(" ", 1)
            token = parts[0].split("[", 1)[0].strip()
            if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.\-]*", token or ""):
                names[token] = names.get(token, False) or (len(parts) > 1 and bool(parts[1].strip()))
    return names


def _specs_in_file(path: Path) -> list[str]:
    """Quoted strings that are dependency specs, not paths or code fragments."""
    text = util.read_text(path)
    if path.parent.name == "versions":
        return [match.group(1) for match in _STRING_PATTERN.finditer(text)]
    specs = []
    for match in _DEP_CALL_PATTERN.finditer(text):
        specs += [item.group(1) for item in _STRING_PATTERN.finditer(match.group(2))]
    specs += [match.group(2) for match in _PACKAGE_ADD_PATTERN.finditer(text)]
    return specs


def _extras_per_version(ctx, plugin, sources: dict[str, Path], owned: set[str]) -> dict[str, list[dict]]:
    """Per version-file extra deps, filtered by what we could actually resolve."""
    result: dict[str, list[dict]] = {}
    versions = plugin.call("list_versions", ctx)
    if not versions:
        return result
    commits = {f"{item['version'].replace('.', '_')}.lua": item["commit"] for item in versions}
    commits["old.lua"] = versions[-1]["commit"]
    commits["main.lua"] = versions[0]["commit"]
    desired = plugin.call("metadata").get("extra_packages", [])
    for filename, commit in commits.items():
        found = plugin.call("extra_deps", ctx, filename[:-4], commit) or []
        resolved = []
        for item in found:
            name = item["name"]
            if name not in owned or name not in desired:
                continue
            version = _resolve_extra_version(ctx, sources, name, item.get("version", ""))
            resolved.append({"name": name, "version": version})
        if resolved:
            result[filename] = resolved
    return result


def _resolve_extra_version(ctx, sources: dict[str, Path], name: str, wanted: str) -> str:
    origin = _find_recipe_dir(sources, name)
    available = _recipe_versions(origin) if origin else set()
    if not wanted:
        return ""
    if any(char in wanted for char in "~^><*|"):
        # A range such as "~1.6.2" is left to xmake, which resolves the newest match.
        return wanted
    if wanted in available:
        return wanted
    if f"v{wanted}" in available:
        return f"v{wanted}"
    _warn_once(f"recipe for '{name}' has no version {wanted}; using the newest available")
    return ""


_WARNED: set[str] = set()


def _warn_once(message: str) -> None:
    if message not in _WARNED:
        _WARNED.add(message)
        util.warn(message)


def _recipe_versions(recipe_dir: Path) -> set[str]:
    versions: set[str] = set()
    text = util.read_text(Path(recipe_dir) / "xmake.lua")
    for match in re.finditer(r'add_versions\(\s*"([^"]+)"', text):
        versions.add(_strip_alias(match.group(1)))
    for match in re.finditer(r'add_versionfiles\(\s*"([^"]+)"', text):
        listing = Path(recipe_dir) / match.group(1)
        if listing.is_file():
            for line in util.read_text(listing).splitlines():
                parts = line.split()
                if parts:
                    versions.add(_strip_alias(parts[0]))
    return versions


def _strip_alias(version: str) -> str:
    """``release:v1.15.5`` / ``git:v1.6.3`` pick a URL alias, not a version."""
    prefix, sep, rest = version.partition(":")
    if sep and rest and "//" not in version:
        return rest
    return version


def _find_recipe_dir(sources: dict[str, Path], name: str) -> Path | None:
    for directory in sources.values():
        candidate = Path(directory) / "packages" / name[0].lower() / name.lower()
        if (candidate / "xmake.lua").is_file():
            return candidate
    return None


def _package_dir(package_repo: Path, name: str) -> Path:
    return Path(package_repo) / "packages" / name[0].lower() / name.lower()


def _rewrite_tree(directory: Path, owned: set[str], prefix: str) -> None:
    for path in sorted(Path(directory).rglob("*")):
        if path.is_file() and path.suffix in (".lua", ".txt"):
            text = util.read_text(path)
            # `versions/*.lua` are plain dependency tables, everything else is a
            # real recipe where only dependency calls may be rewritten.
            pure_table = path.parent.name == "versions" and path.suffix == ".lua"
            rewritten = rewrite_lua(text, owned, prefix, pure_table=pure_table)
            if rewritten.count("(") != text.count("(") or rewritten.count(")") != text.count(")"):
                raise RuntimeError(f"rename changed parenthesis balance in {path}")
            if rewritten != text:
                util.write_text(path, rewritten)


def rewrite_lua(text: str, owned: set[str], prefix: str, pure_table: bool = False) -> str:
    """Prefix dependency references that point at packages we own."""

    if pure_table:
        return _rewrite_strings(text, owned, prefix)

    def replace_call(match: re.Match) -> str:
        name, args = match.group(1), match.group(2)
        return f"{name}({_rewrite_strings(args, owned, prefix)})"

    text = _DEP_CALL_PATTERN.sub(replace_call, text)
    text = _PACKAGE_ADD_PATTERN.sub(
        lambda match: (
            f':add("{match.group(1)}", "{_prefix_spec(match.group(2), owned, prefix)}")'
        ),
        text,
    )
    return _PACKAGE_DECL_PATTERN.sub(
        lambda match: f'package("{_prefix_spec(match.group(1), owned, prefix)}")', text
    )


def _rewrite_strings(text: str, owned: set[str], prefix: str) -> str:
    return _STRING_PATTERN.sub(
        lambda match: f'"{_prefix_spec(match.group(1), owned, prefix)}"', text
    )


def _prefix_spec(content: str, owned: set[str], prefix: str) -> str:
    head, sep, tail = content.partition(" ")
    base = head.split("[", 1)[0]
    if base in owned:
        return f"{prefix}{head}{sep}{tail}"
    return content


def _verify_tree(directory: Path, owned: set[str], prefix: str) -> list[str]:
    """Report dependency-looking strings that still use an upstream name."""
    problems: list[str] = []
    for path in sorted(Path(directory).rglob("*.lua")):
        for content in _specs_in_file(path):
            token = content.split(" ", 1)[0].split("[", 1)[0]
            if token in owned:
                problems.append(f"{path.name}: \"{content}\"")
    return problems



def _version_files(package_repo: Path, package: str) -> list[Path]:
    directory = _package_dir(package_repo, package) / "versions"
    return sorted(directory.glob("*.lua")) if directory.is_dir() else []


def _append_extra_deps(path: Path, extra: list[dict], prefix: str) -> None:
    lines = [
        "",
        "-- BuildCache: extra dependency closure (generated, do not edit)",
        "local _buildcache_extra = {",
    ]
    for item in extra:
        spec = f"{prefix}{item['name']}"
        if item.get("version"):
            spec += f" {item['version']}"
        lines.append(f'    "{spec}",')
    lines += [
        "}",
        "do",
        "    local _buildcache_original_load = load",
        "    function load(package)",
        "        _buildcache_original_load(package)",
        "        for _, dep in ipairs(_buildcache_extra) do",
        '            package:add("deps", dep)',
        "        end",
        "    end",
        "end",
        "",
    ]
    util.write_text(path, util.read_text(path).rstrip("\n") + "\n" + "\n".join(lines))
