"""Recompute a package's xmake build hash for another consumer profile.

The build id is a hash of the configuration (see ``_get_buildhash`` in
``core/package/package.lua``):

    strhash128(plat .. arch .. [label] .. configs_str .. "_" .. sourcehashes .. "_" .. toolchains)

Everything fragile about that — which configs exist, how a source hash is
derived, how the hash itself is computed — is delegated to xmake by running a
small Lua script inside the sandbox. Only the ordering/joining is ours.

The result is verified against a hash we already know from a real build
(``expected``), because a probe that silently computes something else would
publish manifest entries nobody can ever match.
"""

from __future__ import annotations

import re
from pathlib import Path

from . import util

_LUA = r"""
-- BuildCache: print the build hash of a package for a given consumer profile.
-- The hash function itself is the package instance's own (_strhash128), which
-- is exactly what xmake uses internally; no extra module import is needed.
import("core.package.package")

local function sorted_configs(package)
    local configs = package:_configs_for_buildhash() or {}
    local pairs_sorted = {}
    for k, v in pairs(configs) do
        if type(v) == "table" then
            v = string.serialize(v, {strip = true, indent = false, orderkeys = true})
        end
        table.insert(pairs_sorted, k .. "=" .. tostring(v))
    end
    table.sort(pairs_sorted)
    return string.serialize(pairs_sorted, true):gsub("\"", "")
end

local function sourcehashes(package)
    local found = {}
    for _, url in ipairs(package:urls()) do
        local alias = package:url_alias(url)
        local value = package:sourcehash(alias)
        if value then
            table.insert(found, value)
        end
    end
    table.sort(found)
    return table.concat(found, "_")
end

local function toolchains(package)
    local value = package:_config_for_buildhash("toolchains")
    if not value then
        return nil
    end
    local list = table.copy(table.wrap(value))
    table.sort(list)
    return table.concat(list, "_")
end

local name = "__PACKAGE__"
local version = "__VERSION__"
local instance = package.load_from_repository(name, "__RECIPE_DIR__")
if not instance then
    print("HASHPROBE-ERROR unknown package " .. name)
    return
end
instance:version_set(version)
instance:configs_set(__CONFIGS__)
instance:toolchains_set(__TOOLCHAINS__)

local str = instance:plat() .. instance:arch()
local label = instance:label()
if label then
    str = str .. label
end
str = str .. sorted_configs(instance)
local sources = sourcehashes(instance)
if sources ~= "" then
    str = str .. "_" .. sources
end
local tools = toolchains(instance)
if tools then
    str = str .. "_" .. tools
end
print("HASHPROBE-HASH " .. instance:_strhash128(str))
print("HASHPROBE-INPUT " .. str)
"""


def build_hash(
    sandbox,
    package: str,
    version: str,
    recipe_dir: str | Path,
    configs: dict | None = None,
    toolchains: list[str] | None = None,
) -> tuple[str | None, str]:
    """Return ``(hash, input string)`` or ``(None, error)``."""
    script = (
        _LUA.replace("__PACKAGE__", package)
        .replace("__VERSION__", version)
        .replace("__RECIPE_DIR__", str(recipe_dir).replace("\\", "/"))
        .replace("__CONFIGS__", _lua_table(configs or {}))
        .replace("__TOOLCHAINS__", _lua_list(toolchains or []))
    )
    # xmake's `l` action resolves the argument as a module unless it is a plain
    # relative path, so the probe lives inside the sandbox project directory.
    name = f"buildcache_hash_{package.replace('-', '_')}"
    path = sandbox.project_dir / f"{name}.lua"
    util.write_text(path, script)
    result = sandbox.xmake(["l", f"{name}.lua"], check=False)
    output = (result.stdout or "") + (result.stderr or "")
    match = re.search(r"HASHPROBE-HASH (\w+)", output)
    if match:
        data = re.search(r"HASHPROBE-INPUT (.*)", output)
        return match.group(1), (data.group(1) if data else "")
    errors = [line for line in output.splitlines() if "error" in line.lower()]
    return None, (errors[-1].strip() if errors else "probe produced no hash")


def _lua_table(mapping: dict) -> str:
    body = ", ".join(f"{key} = {_lua_value(value)}" for key, value in mapping.items())
    return "{ " + body + " }"


def _lua_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return f'"{value}"'


def _lua_list(values: list[str]) -> str:
    return "{" + ", ".join(f'"{value}"' for value in values) + "}"
