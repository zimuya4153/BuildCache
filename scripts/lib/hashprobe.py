"""Recompute a package's xmake build hash for another consumer profile.

An artifact's directory name is ``_get_buildhash()`` of
``core/package/package.lua``:

    strhash128(plat .. arch .. [label] .. configs_str .. "_" .. sourcehashes .. "_" .. toolchains)

The toolchain is part of that input, so the very same artifact is addressed by a
*different* build hash under a consumer that compiles with clang-cl. Publishing a
second artifact is not needed for that: one more manifest entry pointing at the
existing URL is enough, but only if the hash is exactly the one the consumer
computes. Everything fragile about that computation -- which configs take part,
how a source hash is derived, how the hash itself is produced -- is therefore
delegated to xmake by running a small Lua script inside the sandbox:

* the configs come from the installed package's own ``manifest.txt`` (that file
  is written from ``package:configs()`` at install time, so it is the resolved
  truth, not a guess);
* the ignore-set for header-only/binary packages is derived from the recipe, the
  same way the require action derives it;
* the hash, finally, is asked of the very xmake that is running -- nothing about
  the algorithm, its name (``strhash128``/``uuid4``) or its compatibility
  branches is reimplemented here, so a build using an old era's xmake still gets
  that era's answer.

The result is verified against the hash the artifact actually carries: if the
recomputation of the built profile disagrees, the caller must not publish an
alias, because it would be a hash nobody can ever match.

Computing a hash is not the same as it being consulted, though: on Windows xmake
only looks at the manifests when it resolved a *checked* msvc toolchain, which
means the profiles worth declaring are the ones a project can name in its own
file (``set_toolchains``). A command line ``--toolchain=...`` hashes identically
and still gets nothing -- see ``plugins/ll/plugin.py::toolchain_aliases``.
"""

from __future__ import annotations

import re
from pathlib import Path

from . import util

#: Profile id reserved for "the way the artifact was really built".
PRIMARY = "primary"

_LUA = r"""
-- BuildCache: compute the build hash of one package for several consumer profiles.
-- The hash function is the package instance's own, so no algorithm is reimplemented.
import("core.package.package")

local package_name = "__PACKAGE__"
local version      = "__VERSION__"
local plat         = "__PLAT__"
local arch         = "__ARCH__"
local recipe_dir   = "__RECIPE_DIR__"
local package_file = "__PACKAGE_FILE__"
local manifest_file = "__MANIFEST__"
local profiles     = __PROFILES__
local repo_dirs    = __REPOS__

-- Recipes may delegate to another package: `package("gsl") set_base("microsoft-gsl")`.
-- The require action gives such a package its base instance, and everything the
-- derived script does not define itself (urls, versions, configs) is then read
-- from it, so the probe has to do the same.
local function find_package_dir(name)
    for _, root in ipairs(repo_dirs) do
        local dir = path.join(root, "packages", name:sub(1, 1):lower(), name:lower())
        if os.isfile(path.join(dir, "xmake.lua")) then
            return dir
        end
    end
end

local function resolve_base(instance, depth)
    depth = depth or 0
    local name = instance:get("base")
    if not name or depth > 4 then
        return
    end
    local dir = find_package_dir(name)
    if not dir or dir == instance:scriptdir() then
        return
    end
    local base = package.load_from_repository(name, dir,
        {packagefile = path.join(dir, "xmake.lua")})
    if base then
        instance._BASE = base
        resolve_base(base, depth + 1)
    end
end

-- The configs the package was really built with: xmake writes them into the
-- install directory's manifest.txt, so they are read rather than guessed.
local base_configs = {}
if manifest_file ~= "" then
    local manifest = io.load(manifest_file)
    if manifest and manifest.configs then
        base_configs = manifest.configs
    end
end

-- Mirrors _add_package_configurations() from
-- modules/private/action/require/impl/package.lua: the resolver injects these
-- builtin configs before hashing, and they take part in the string. Only
-- debug/shared/pic carry a default; the others stay nil and never show up.
local function add_builtin_configs(package)
    local boolean_defaults = {debug = false, shared = false, pic = true}
    for _, name in ipairs({"debug", "shared", "pic"}) do
        if package:extraconf("configs", name, "default") == nil then
            package:add("configs", name,
                {builtin = true, default = boolean_defaults[name], type = "boolean"})
        end
    end
    for _, name in ipairs({"lto", "asan", "runtimes", "vs_runtime", "toolchains",
                           "cflags", "cxflags", "cxxflags", "asflags", "ldflags", "shflags"}) do
        if package:extraconf("configs", name, "default") == nil then
            package:add("configs", name, {builtin = true})
        end
    end
end

-- Mirrors _init_requireinfo(): for header-only/module-only packages and for host
-- binaries these configs are dropped from the hash entirely.
local function ignored_configs(package)
    -- is_moduleonly only exists from 2.9 on.
    local moduleonly = package.is_moduleonly and package:is_moduleonly()
    if package:is_headeronly() or moduleonly or
       (package:is_binary() and not package:is_cross()) then
        return {"runtimes", "toolchains", "lto", "asan", "pic"}
    end
    return {}
end

-- The package's own build hash. xmake renamed the entry point over the years
-- (_compute_buildhash was added in 2.9.x), so ask for it defensively, but never
-- reimplement the computation itself.
local function buildhash(package)
    if package._compute_buildhash then
        package:_compute_buildhash()
    end
    return package:buildhash()
end

-- Only for the log line: the configs this profile was hashed with, in the one
-- serializer form every era understands (`pcall` is not available in a sandbox
-- script, so there is no way to try the fancier one defensively).
local function describe(configs)
    return tostring(string.serialize(configs, true))
end

for _, profile in ipairs(profiles) do
    -- A fresh instance per profile: the configs-for-buildhash cache lives on it.
    local instance = package.load_from_repository(package_name, recipe_dir,
        {packagefile = package_file})
    if not instance then
        print("HASHPROBE-ERROR unknown package " .. package_name)
        return
    end
    resolve_base(instance)
    instance:plat_set(plat)
    instance:arch_set(arch)
    instance:version_set(version)
    add_builtin_configs(instance)
    local configs = {}
    for key, value in pairs(base_configs) do
        configs[key] = value
    end
    -- A profile states its own toolchain; the built profile has no such field and
    -- keeps whatever the install manifest recorded.
    if profile.clear_toolchains then
        configs.toolchains = nil
    elseif profile.toolchains then
        configs.toolchains = profile.toolchains
    end
    instance:requireinfo_set({configs = configs, configs_overrided = {},
        ignored_configs_for_buildhash = ignored_configs(instance)})

    print("HASHPROBE " .. profile.id .. " " .. tostring(buildhash(instance)) ..
          " configs=" .. describe(configs))
end
"""


def build_hashes(
    sandbox,
    package: str,
    version: str,
    recipe_dir: str | Path,
    plat: str,
    arch: str,
    manifest_file: str | Path | None,
    profiles: list[dict],
    repo_dirs: list[str | Path] | None = None,
) -> tuple[dict[str, dict], str]:
    """Return ``({profile id: {"hash", "input"}}, "")`` or ``({}, error)``.

    ``profiles`` is a list of ``{"id": ..., "toolchains": str | None}``. The
    caller is expected to include the profile the artifact was built with, so
    :func:`verify` can be applied to the result.
    """
    recipe_dir = Path(recipe_dir)
    script = (
        _LUA.replace("__PACKAGE__", package)
        .replace("__VERSION__", version)
        .replace("__PLAT__", plat)
        .replace("__ARCH__", arch)
        .replace("__RECIPE_DIR__", recipe_dir.as_posix())
        .replace("__PACKAGE_FILE__", (recipe_dir / "xmake.lua").as_posix())
        .replace("__MANIFEST__", Path(manifest_file).as_posix() if manifest_file else "")
        .replace("__PROFILES__", _lua_profiles(profiles))
        .replace(
            "__REPOS__",
            "{" + ", ".join(f'"{Path(item).as_posix()}"' for item in repo_dirs or []) + "}",
        )
    )
    # `xmake l` resolves its argument as a module unless it is a plain relative
    # path, so the probe lives inside the sandbox project directory.
    name = f"buildcache_hash_{package.replace('-', '_')}"
    path = Path(sandbox.project_dir) / f"{name}.lua"
    util.write_text(path, script)
    result = sandbox.xmake(["l", f"{name}.lua"], check=False, capture=True, quiet=True)
    output = (result.stdout or "") + (result.stderr or "")
    found: dict[str, dict] = {}
    for line in output.splitlines():
        match = re.match(r"HASHPROBE (\S+) (\w+)(?: (.*))?$", line.strip())
        if match:
            found[match.group(1)] = {"hash": match.group(2), "input": match.group(3) or ""}
    if not found:
        errors = [line.strip() for line in output.splitlines() if "error" in line.lower()]
        tail = [line.strip() for line in output.splitlines() if line.strip()][-5:]
        return {}, (errors[-1] if errors else " | ".join(tail) or "probe produced no hash")
    return found, ""


def digest(hashes: dict[str, dict], profile: str) -> str | None:
    found = hashes.get(profile) or {}
    return found.get("hash")


def verify(hashes: dict[str, dict], built_hash: str) -> bool:
    """Did the recomputation of the built profile reproduce the artifact's hash?"""
    return digest(hashes, PRIMARY) == built_hash


def input_of(hashes: dict[str, dict], profile: str) -> str:
    """The exact string xmake hashed (useful when a probe has to be explained)."""
    found = hashes.get(profile) or {}
    return found.get("input") or ""


def _lua_profiles(profiles: list[dict]) -> str:
    items = []
    for profile in profiles:
        fields = [f'id = "{profile["id"]}"']
        if "toolchains" in profile:
            if profile["toolchains"]:
                fields.append(f'toolchains = "{profile["toolchains"]}"')
            else:
                fields.append("clear_toolchains = true")
        items.append("{ " + ", ".join(fields) + " }")
    return "{" + ", ".join(items) + "}"
