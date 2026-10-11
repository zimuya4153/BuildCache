"""Fast unit tests for the logic that used to be verified by a 15-minute run.

Run with ``pytest -q`` (pytest is installed automatically with the other
dependencies).
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import build  # noqa: E402
import finalize  # noqa: E402
from lib import (  # noqa: E402
    aliases,
    gh,
    hashprobe,
    index_store,
    manifest,
    plugins,
    recipefix,
    reports,
    schemas,
    semver,
    util,
)


# --------------------------------------------------------------------- versions
def test_sort_desc_orders_ll_versions():
    assert semver.sort_desc(["1.0.1", "26.51.6", "1.4.4", "0.13.5"]) == [
        "26.51.6",
        "1.4.4",
        "1.0.1",
        "0.13.5",
    ]


def test_date_versions_are_accepted():
    # semver would reject the leading zero; our adapter normalises it first.
    assert semver.compare("2025.02.05", "v1.1.0") > 0
    assert semver.sort_desc(["2025.02.05", "2024.11.7", "1.1.0"])[0] == "2025.02.05"


def test_prerelease_sorts_below_release():
    assert semver.compare("v26.51.1-server.7", "26.51.0") > 0
    assert semver.compare("1.2.3-rc.1", "1.2.3") < 0


def test_two_component_versions_are_padded():
    # leveldb really ships "1.23"; the ledger must keep sorting.
    assert semver.sort_desc(["1.23", "1.22.9", "11.2.0"]) == ["11.2.0", "1.23", "1.22.9"]
    assert semver.compare("1.23", "1.22") > 0


def test_unparseable_version_does_not_break_sorting():
    assert semver.sort_desc(["master", "1.23", "2.0.0"])[0] == "2.0.0"


# ---------------------------------------------------------------------- patches
def test_repair_patch_restores_blank_context_lines():
    broken = "@@ -1,2 +1,2 @@\n-old\n+new\n\n context\n"
    repaired, count = recipefix.repair_patch(broken)
    assert count == 1
    assert "\n \n" in repaired


def test_repair_patch_leaves_good_patch_alone():
    good = "@@ -1,1 +1,1 @@\n-old\n+new\n"
    repaired, count = recipefix.repair_patch(good)
    assert count == 0 and repaired == good


# --------------------------------------------------------------------- manifest
def test_manifest_round_trip(tmp_path: Path):
    entries = {
        "windows-x64-vc-abc": {"toolset": "14.44", "urls": "https://x/a.7z", "sha256": "ff" * 32}
    }
    manifest.write_all(tmp_path, "mifcache_fmt", "11.2.0", entries)
    assert manifest.read(tmp_path, "mifcache_fmt", "11.2.0") == entries


def test_build_id_matches_xmake_contract():
    # xmake matches on "<plat>-<arch>-vc..." prefix and a "-<buildhash>" suffix.
    key = manifest.build_id("windows", "x64", "14.44.35207", "deadbeef")
    assert key.startswith("windows-x64-vc")
    assert key.endswith("-deadbeef")


# ------------------------------------------------------------------ index/store
def test_revision_gates_rebuilds(tmp_path: Path):
    index_store.write_entry(
        tmp_path, "mifcache_fmt", "11.2.0", "server", {"revision": 1, "artifacts": []}
    )
    assert index_store.built_entries_at_revision(tmp_path, "mifcache_fmt", "11.2.0", 1) == {"server"}
    # a fix bumps the config revision -> the entry counts as stale again
    assert index_store.built_entries_at_revision(tmp_path, "mifcache_fmt", "11.2.0", 2) == set()


def test_legacy_variant_documents_still_read(tmp_path: Path):
    util.write_json(
        tmp_path / "mifcache_fmt" / "versions" / "11.2.0.json",
        {"package": "mifcache_fmt", "version": "11.2.0", "artifacts": [{"key": "k"}]},
    )
    meta = index_store.version_meta(tmp_path, "mifcache_fmt", "11.2.0")
    assert "default" in index_store.entries(meta)


def test_entry_keeps_the_profiles_it_was_built_for(tmp_path: Path):
    """`discover` compares the declared profiles against what an entry records."""
    index_store.write_entry(
        tmp_path,
        "mifcache_levilamina",
        "26.51.6",
        "server",
        {"revision": 1, "alias_profiles": ["msvc", "clang-cl"], "artifacts": []},
    )
    assert index_store.built_entries_with_profiles(
        tmp_path, "mifcache_levilamina", "26.51.6", 1, ["msvc", "clang-cl"]
    ) == {"server"}
    # a plugin that starts declaring another profile queues the entry again
    assert (
        index_store.built_entries_with_profiles(
            tmp_path, "mifcache_levilamina", "26.51.6", 1, ["msvc", "clang-cl", "gcc"]
        )
        == set()
    )


# ------------------------------------------------------------------------ config
def test_shipped_config_is_valid():
    config = schemas.validate(schemas.Config, util.load_config())
    assert config.recipes.snapshotMode == "version"
    assert config.xmake.installRetries >= 1
    assert config.bot.email.endswith("users.noreply.github.com")


def test_config_typo_is_rejected():
    import pytest

    with pytest.raises(Exception):
        schemas.validate(schemas.Config, {"prefix": "x", "repos": {"package": {}, "cache": {}}})


# ---------------------------------------------------------------------- aliases
def test_alias_profiles_say_what_they_change():
    # "primary" is the artifact as built: whatever its install manifest recorded
    # must stay untouched, that is what makes the self-check meaningful.
    assert hashprobe._lua_profiles([{"id": "primary"}]) == '{{ id = "primary" }}'
    assert hashprobe._lua_profiles([{"id": "clang-cl", "toolchains": "clang-cl"}]) == (
        '{{ id = "clang-cl", toolchains = "clang-cl" }}'
    )
    # a profile that means "no toolchain" is as explicit as a named one
    assert hashprobe._lua_profiles([{"id": "msvc", "toolchains": None}]) == (
        '{{ id = "msvc", clear_toolchains = true }}'
    )


def test_alias_verification_uses_the_built_profile():
    hashes = {"primary": {"hash": "abc", "input": "windowsx64{...}"}}
    assert hashprobe.verify(hashes, "abc")
    assert not hashprobe.verify(hashes, "def")
    assert not hashprobe.verify({}, "abc")
    assert hashprobe.input_of(hashes, "primary") == "windowsx64{...}"


def test_recipe_candidates_prefer_the_repository_with_that_version(tmp_path: Path):
    for repo, version in (("levimc-repo", "10.0.0"), ("xmake-repo", "11.2.0")):
        recipe = tmp_path / repo / "packages" / "f" / "fmt"
        recipe.mkdir(parents=True)
        (recipe / "xmake.lua").write_text(
            f'add_versions("{version}", "deadbeef")\n', encoding="utf-8"
        )
    repos = [("levimc-repo", tmp_path / "levimc-repo"), ("xmake-repo", tmp_path / "xmake-repo")]
    found = aliases.recipe_candidates(repos, "fmt", "11.2.0")
    assert [path.parts[-4] for path in found] == ["xmake-repo", "levimc-repo"]


def test_finalize_publishes_aliases_into_the_manifest(tmp_path: Path):
    index_root = tmp_path / "index"
    package_repo = tmp_path / "xmake-repo"
    artifact = {
        "package": "mifcache_fmt",
        "upstream_package": "fmt",
        "version": "11.2.0",
        "buildhash": "ee1902dd",
        "key": "windows-x64-vc-ee1902dd",
        "toolset": "14.44.35207",
        "url": "https://example.invalid/fmt-ee1902dd.7z",
        "sha256": "ab" * 32,
        "aliases": [
            {
                "id": "clang-cl",
                "toolchain": "clang-cl",
                "buildhash": "df51e1a2",
                "key": "windows-x64-vc-df51e1a2",
                "toolset": "14.44.35207",
            }
        ],
    }
    index_store.write_entry(
        index_root, "mifcache_fmt", "11.2.0", "server", {"revision": 1, "artifacts": [artifact]}
    )
    config = util.load_config()
    finalize.regenerate_manifests(
        config,
        plugins.Context(config),
        index_root=index_root,
        package_repo=package_repo,
    )
    entries = manifest.read(package_repo, "mifcache_fmt", "11.2.0")
    assert set(entries) == {"windows-x64-vc-ee1902dd", "windows-x64-vc-df51e1a2"}
    # one archive, two addresses
    assert entries["windows-x64-vc-df51e1a2"] == entries["windows-x64-vc-ee1902dd"]


def test_shipped_plugins_declare_the_profiles_they_can_serve(monkeypatch):
    """Every project says which toolchains its artifacts answer to.

    The profiles are what the build recomputes hashes for; a project that declares
    none only serves the toolchain it was built with. The ll plugin reads
    LeviLamina's own xmake.lua to decide, so the text is stubbed to keep this
    test offline.
    """
    modern = 'local is_server = is_config("target_type", "server")\nset_toolchains("clang-cl")\n'
    found = {}
    for plugin in plugins.load_all(util.load_config()):
        monkeypatch.setattr(
            plugin.module, "upstream_xmake_lua", lambda ctx, commit: modern, raising=False
        )
        profiles = plugin.call("toolchain_aliases", None, "26.51.6", "") or []
        found[plugin.name] = {item["id"] for item in profiles}
    # LeviLamina is a clang-cl build at this version, so both addresses make sense
    assert found["ll"] == {"msvc", "clang-cl"}, found
    # the config-driven projects are msvc builds: no clang-cl address for them
    assert found["moredimensions"] == {"msvc"}, found
    assert found["bsci"] == {"msvc"}, found


def test_ll_entries_follow_the_upstream_build_shape(monkeypatch):
    """Old tags have no client SDK and are not clang-cl builds."""
    config = util.load_config()
    ll = next(item for item in plugins.load_all(config) if item.name == "ll")

    modern = (
        'option("target_type")\n' 'set_values("server", "client")\n'
        'local is_server = is_config("target_type", "server")\n'
        'set_toolchains("clang-cl")\n'
    )
    legacy = (
        '-- local is_client = is_config("target_type", "client")\n'
        "local is_client = false\n"
        'add_requires("bedrockdata 1.21.0.3-client")\n'
        'option("target_type")\n'
    )
    # 0.8/0.13 have no client mechanism at all: asking for it would silently
    # produce the server flavour, so the entry must not be declared either.
    ancient = 'add_defines("LL_PLAT_CLIENT")\n'

    monkeypatch.setattr(ll.module, "upstream_xmake_lua", lambda ctx, commit: modern)
    assert [entry["id"] for entry in ll.module.entries(None, "26.51.6", "")] == [
        "server",
        "client",
    ]
    assert [item["id"] for item in ll.module.toolchain_aliases(None, "26.51.6", "")] == [
        "msvc",
        "clang-cl",
    ]

    monkeypatch.setattr(ll.module, "upstream_xmake_lua", lambda ctx, commit: legacy)
    assert [entry["id"] for entry in ll.module.entries(None, "1.4.4", "")] == ["server"]
    # an msvc build predates clang-cl support, so it gets no clang-cl address
    assert [item["id"] for item in ll.module.toolchain_aliases(None, "1.4.4", "")] == ["msvc"]

    monkeypatch.setattr(ll.module, "upstream_xmake_lua", lambda ctx, commit: ancient)
    assert [entry["id"] for entry in ll.module.entries(None, "0.8.3", "")] == ["server"]


# ------------------------------------------------------------- failed installs
def test_release_table_only_shows_columns_that_carry_information():
    """The core columns come from the framework, the rest from the plugin.

    A column whose value is empty in every row is dropped, so a dependency's table
    has no ``entry`` and no ``compiled with`` -- those are the project's concepts.
    """
    table = reports.Table()
    ll = next(item for item in plugins.load_all(util.load_config()) if item.name == "ll")
    ll.call("register_release_columns", table, None)

    own_row = reports.artifact_row(
        {
            "version": "26.51.6",
            "buildhash": "906b0696836741eda5b5749d54146a8c",
            "toolset": "14.44.35207",
            "asset": "levilamina-906b0696836741eda5b5749d54146a8c.7z",
            "url": "https://example.invalid/levilamina-906b0696836741eda5b5749d54146a8c.7z",
            "aliases": [{"id": "msvc"}, {"id": "clang-cl"}],
        },
        entry_id="server",
        entry={"compiled_with": "clang-cl"},
    )
    dependency_row = reports.artifact_row(
        {
            "version": "11.2.0",
            "buildhash": "ee1902dd7cff4195a287949f80dbce70",
            "asset": "fmt-ee1902dd7cff4195a287949f80dbce70.7z",
            "url": "https://example.invalid/fmt-ee1902dd7cff4195a287949f80dbce70.7z",
        },
        own=False,
    )

    owned = "\n".join(table.render([own_row]))
    assert "| entry |" in owned and "| compiled with |" in owned
    # the asset name is a direct download link
    assert "[levilamina-906b0696836741eda5b5749d54146a8c.7z](https://example.invalid/" in owned

    dependency = "\n".join(table.render([dependency_row]))
    assert "| entry |" not in dependency
    assert "| compiled with |" not in dependency
    assert "| version |" in dependency and "| asset |" in dependency


def test_table_manager_adds_and_removes_columns():
    table = reports.Table()
    assert table.keys() == ["version", "buildhash", "toolset", "asset"]
    table.add("variant", title="variant", render=reports.code)
    assert table.has("variant") and table.keys()[-1] == "variant"
    # registering the same key twice replaces instead of duplicating
    table.add("variant", title="build entry")
    assert table.keys().count("variant") == 1
    assert table.remove("variant").has("variant") is False


def test_release_rows_put_the_newest_version_on_top():
    """A version is not a string, and a reader looks the newest build up first.

    Sorted as text, ``0.13.5`` lands above ``0.8.3`` -- which is exactly what the
    published body used to show. The entries of one version keep a fixed order.
    """
    rows = [
        reports.artifact_row(
            {"buildhash": version, "asset": f"{version}.7z"},
            version=version,
            entry_id=entry,
            own=True,
        )
        for version, entry in [
            ("0.8.3", "server"),
            ("0.13.5", "server"),
            ("1.0.1", "server"),
            ("26.51.6", "server"),
            ("1.4.4", "server"),
            ("26.51.6", "client"),
        ]
    ]
    assert [(row["version"], row["entry"]) for row in reports.newest_first(rows)] == [
        ("26.51.6", "client"),
        ("26.51.6", "server"),
        ("1.4.4", "server"),
        ("1.0.1", "server"),
        ("0.13.5", "server"),
        ("0.8.3", "server"),
    ]


def test_failed_install_logs_are_found(tmp_path: Path):
    """xmake only points at a log inside the sandbox, so we print it ourselves."""
    logs = (
        tmp_path / "root" / ".xmake" / "cache" / "packages"
        / "2610" / "g" / "gsl" / "v4.0.0" / "installdir.failed" / "logs"
    )
    logs.mkdir(parents=True)
    (logs / "install.txt").write_text("vs not found!\n", encoding="utf-8")

    class FakeSandbox:
        root_dir = tmp_path / "root"

    from lib import xmake_env

    found = xmake_env.failed_install_logs(FakeSandbox())
    assert [item["package"] for item in found] == ["gsl"]
    assert [item["version"] for item in found] == ["v4.0.0"]
    assert [path.name for path in found[0]["logs"]] == ["install.txt"]
    assert xmake_env.failed_packages(FakeSandbox()) == [("gsl", "v4.0.0")]


def test_verbose_xmake_noise_is_filtered():
    """A verbose build copies every file; those lines only drown the log.

    Filtering is about printing: the line still counts as activity, otherwise a
    long copy phase would look like the stuck step the heartbeat guards against.
    """
    from lib import xmake_env

    assert xmake_env.is_copy_noise("> copy include/fmt/core.h to ...")
    assert xmake_env.is_copy_noise("copy build/x.lib to lib/x.lib")
    assert xmake_env.is_copy_noise("Copying: lib/fmt.lib -> install/lib/fmt.lib")
    assert xmake_env.is_copy_noise("   > copy a b")
    # real progress and results are kept
    assert not xmake_env.is_copy_noise("=> install fmt 11.2.0 .. ok")
    assert not xmake_env.is_copy_noise("[ 50%]: ccache compile src/a.cpp")
    # a failure that merely mentions copying must still be visible
    assert not xmake_env.is_copy_noise("copying failed: permission denied")


def test_old_era_sources_get_the_include_they_forgot(tmp_path: Path):
    """The repair injects the missing standard include while the package installs.

    libhat 2024.4.16 uses std::string without including <string>, and that fails
    with every toolset the CI image has, so it has to be fixed in the source.
    """
    recipe = tmp_path / "levimc-repo@test" / "packages" / "l" / "libhat"
    recipe.mkdir(parents=True)
    (recipe / "xmake.lua").write_text(
        'package("libhat")\n'
        "    on_install(function (package)\n"
        '        import("package.tools.cmake").install(package)\n'
        "    end)\n",
        encoding="utf-8",
    )
    ll = next(
        item for item in plugins.load_all(util.load_config()) if item.name == "ll"
    )
    snapshot = tmp_path / "levimc-repo@test"
    notes = ll.module._repair_era_sources(snapshot)
    assert notes and "libhat" in notes[0]
    text = (recipe / "xmake.lua").read_text(encoding="utf-8")
    # it belongs to the install body, not to the recipe's top level
    assert text.index("BuildCache: era includes") > text.index("on_install(function")
    assert 'local file = "include/libhat/Process.hpp"' in text
    assert 'io.writefile(file, "#include <string>' in text
    # running it twice is a no-op
    assert ll.module._repair_era_sources(snapshot) == []


def test_old_era_sources_stop_reaching_into_the_stl(tmp_path: Path):
    """optional_ref::transform used std::_Construct_from_invoke_result_tag.

    That is an MSVC STL internal, and the private constructor it fed is no longer
    reachable (C2248), so the era's own source has to be spelled the supported way.
    """
    recipe = tmp_path / "levimc-repo@test" / "packages" / "l" / "levilamina"
    recipe.mkdir(parents=True)
    (recipe / "xmake.lua").write_text(
        'package("levilamina")\n'
        "    on_install(function (package)\n"
        '        import("package.tools.xmake").install(package)\n'
        "    end)\n",
        encoding="utf-8",
    )
    ll = next(
        item for item in plugins.load_all(util.load_config()) if item.name == "ll"
    )
    notes = ll.module._repair_era_sources(tmp_path / "levimc-repo@test")
    assert notes
    text = (recipe / "xmake.lua").read_text(encoding="utf-8")
    assert 'io.replace("src/mc/common/wrapper/optional_ref.h"' in text
    assert "std::_Construct_from_invoke_result_tag{}," in text
    assert "std::invoke(std::forward<Fn>(fn), static_cast<T&>(*mPtr))" in text
    assert "{plain = true})" in text


def test_repaired_versions_are_renamed_where_the_resolver_reads_them(tmp_path: Path):
    """The oldest era names its dependencies in the recipe's own xmake.lua.

    That map is read while resolving, so a four-component version has to be
    rewritten there, not only in the per-version files and not only at install
    time (which is when the upstream xmake.lua gets its own renames).
    """
    ll = next(
        item for item in plugins.load_all(util.load_config()) if item.name == "ll"
    )
    root = tmp_path / "levimc-repo@test" / "packages" / "l" / "levilamina"
    (root / "versions").mkdir(parents=True)
    (root / "xmake.lua").write_text(
        'package("levilamina")\n'
        "    local dependencies = {\n"
        '        ["0.8.3"] = {"bdslibrary 1.20.61.01"}\n'
        "    }\n",
        encoding="utf-8",
    )
    (root / "versions" / "0_13_5.lua").write_text(
        'add_deps("bdslibrary 1.21.0.3")\n', encoding="utf-8"
    )

    notes = ll.module._rename_dependents(
        tmp_path / "levimc-repo@test",
        {"bdslibrary": {"1.20.61.01": "1.20.61-r01", "1.21.0.3": "1.21.0-r3"}},
    )
    assert len(notes) == 2, notes
    assert '"bdslibrary 1.20.61-r01"' in (root / "xmake.lua").read_text(encoding="utf-8")
    assert '"bdslibrary 1.21.0-r3"' in (root / "versions" / "0_13_5.lua").read_text(
        encoding="utf-8"
    )


def test_upstream_pins_are_rewritten_while_the_project_builds(tmp_path: Path):
    """The era's *project* file pins its data package, so both sides must agree.

    LeviLamina 0.8.3's own xmake.lua says ``add_requires("bdslibrary 1.20.61.01")``,
    a four-component version the vendored recipe cannot offer any more (it was
    renamed). The recipe therefore rewrites the project file while it installs --
    and that era spells the anchor ``on_install(function (package)``, with a space.
    """
    ll = next(
        item for item in plugins.load_all(util.load_config()) if item.name == "ll"
    )
    snapshot = tmp_path / "levimc-repo@test"
    root = snapshot / "packages" / "l" / "levilamina"
    root.mkdir(parents=True)
    recipe = root / "xmake.lua"
    recipe.write_text(
        'package("levilamina")\n'
        "    on_install(function (package)\n"
        '        import("package.tools.xmake").install(package)\n'
        "    end)\n",
        encoding="utf-8",
    )
    renames = {"bdslibrary": {"1.20.61.01": "1.20.61-r01"}}

    notes = ll.module._patch_upstream_sources(snapshot, renames)
    assert len(notes) == 1, notes
    injected = recipe.read_text(encoding="utf-8")
    assert (
        'io.replace("xmake.lua", "bdslibrary 1.20.61.01", '
        '"bdslibrary 1.20.61-r01", {plain = true})' in injected
    )
    # the rewrite has to happen before the step that configures the project
    assert injected.index("io.replace") < injected.index("package.tools.xmake")
    # injected once, however often the recipe is prepared
    assert ll.module._patch_upstream_sources(snapshot, renames) == []
    assert injected.count("-- BuildCache: upstream dependency renames") == 1

    # a recipe with no install step to hook has nothing to patch
    bare = snapshot / "packages" / "l" / "levilamina-bare"
    bare.mkdir(parents=True)
    (bare / "xmake.lua").write_text('package("levilamina-bare")\n', encoding="utf-8")
    assert ll.module._patch_upstream_sources(snapshot, renames) == []


def test_dependencies_are_recorded_once_without_an_entry(tmp_path: Path):
    """Only the project's own package has an entry dimension."""
    config = dict(util.load_config())
    config["_index_dir"] = str(tmp_path / "index")
    ctx = plugins.Context(config)

    def artifact(package: str, buildhash: str, entry: str) -> dict:
        return {
            "package": package,
            "upstream_package": package.replace("mifcache_", ""),
            "version": "11.2.0" if package == "mifcache_fmt" else "26.51.6",
            "buildhash": buildhash,
            "key": f"windows-x64-vc-{buildhash}",
            "toolset": "14.44.35207",
            "url": f"https://example.invalid/{package}-{buildhash}.7z",
            "sha256": "ab" * 32,
            "aliases": [],
        }

    shared = artifact("mifcache_fmt", "ee1902dd", "server")
    results = [
        {
            "package": "levilamina",
            "version": "26.51.6",
            "entry": entry,
            "revision": 1,
            "toolchain": "msvc",
            "upstream_toolchain": "clang-cl",
            "alias_profiles": ["msvc", "clang-cl"],
            "artifacts": [artifact("mifcache_levilamina", f"ll{entry}", entry), shared],
        }
        for entry in ("server", "client")
    ]
    finalize.write_ledger(config, ctx, results)

    owned = index_store.entries(
        index_store.version_meta(ctx.index_root, "mifcache_levilamina", "26.51.6")
    )
    assert set(owned) == {"server", "client"}, owned
    assert owned["server"]["compiled_with"] == "clang-cl"
    assert owned["server"]["revision"] == 1
    # the project's record holds its own artifact, not the whole closure
    assert [item["package"] for item in owned["server"]["artifacts"]] == [
        "mifcache_levilamina"
    ]

    dependency = index_store.entries(
        index_store.version_meta(ctx.index_root, "mifcache_fmt", "11.2.0")
    )
    assert set(dependency) == {"default"}, dependency
    assert len(dependency["default"]["artifacts"]) == 1


def test_dependency_addresses_follow_the_windows_abi():
    """A dependency serves both msvc and clang-cl, whatever the project's era.

    LeviLamina before 26.20 is an msvc build and gets no clang-cl address, but the
    packages it pulls in are ordinary Windows static libraries: the same archive
    is perfectly addressable by a clang-cl project, and the address must not be
    dropped just because an older version of the project rebuilt that package.
    """
    assert aliases.closure_profiles("msvc") == [
        {"id": "msvc", "toolchain": "msvc"},
        {"id": "clang-cl", "toolchain": "clang-cl"},
    ]
    # an artifact built for a different ABI has no such pair
    assert aliases.closure_profiles("gcc") == []


def test_old_eras_use_an_xmake_of_their_own_line_that_works():
    """An era's xmake may be bumped inside its line, never across lines.

    xmake 2.9.4 asks CMake for the platform toolset v144 (a Visual Studio 2026
    toolset) while the image only has v143, which makes every CMake package fail
    with MSB8020; 2.9.7 is the same line and gets it right. The 2.8 line must stay
    put: a newer xmake renamed directories that those projects refer to.
    """
    ll = next(
        item for item in plugins.load_all(util.load_config()) if item.name == "ll"
    )
    floor = ll.module.line_floor
    assert floor("2.9.4") == "2.9.7"
    assert floor("2.9.7") == "2.9.7"
    assert floor("2.8.6") == "2.8.6"
    assert floor("3.0.1") == "3.0.1"


def test_eras_use_the_toolset_the_image_has():
    """No era pins an MSVC generation: the images carry one, plus 14.29.

    Pinning an old tag to 14.29 was tried and made things worse -- its STL is too
    old for those sources (preloader dies in unordered_set, LeviLamina's own
    optional_ref.h cannot use std::optional the way it does), while the missing
    include the newest STL complained about is repaired in the sources instead.
    """
    ll = next(
        item for item in plugins.load_all(util.load_config()) if item.name == "ll"
    )
    assert all(toolset is None for _floor, _runner, _imageos, toolset in ll.module.ERA_IMAGES)


def test_pre_2_9_4_xmake_gets_the_toolset_fix_backported(tmp_path: Path):
    """The 2.8 line turns 14.44 into "v144"; 2.9.4 maps it back and 2.8 never did.

    Every CMake package of a 2.8-era build died with MSB8020, because the newest
    toolset the image carries (14.44, still the v143 family) was handed to MSBuild
    under the Visual Studio 2026 name.
    """
    from lib import xmake_env

    tree = tmp_path / "v2.8.6" / "xmake"
    cmake = tree / "modules" / "package" / "tools" / "cmake.lua"
    cmake.parent.mkdir(parents=True)
    cmake.write_text(
        "function _get_vs_toolset(package)\n"
        "        if #verinfo >= 2 then\n"
        '            toolset_ver = "v" .. verinfo[1] .. (verinfo[2]:sub(1, 1) or "0")\n'
        "        end\n"
        "end\n",
        encoding="utf-8",
    )

    assert xmake_env.patch_cmake_toolset_family(tree) is True
    patched = cmake.read_text(encoding="utf-8")
    assert 'toolset_ver == "v144" and _minor >= 40 and _minor < 50' in patched
    # the derivation must survive, it is what a 14.2x/14.3x toolset still goes through
    assert 'toolset_ver = "v" .. verinfo[1]' in patched
    # idempotent: a second pass must not stack a copy on top
    assert xmake_env.patch_cmake_toolset_family(tree) is False
    assert patched.count("local _minor") == 1

    # 2.9.4+ keeps this in modules/private/utils/toolchain.lua and is left alone
    newer = tmp_path / "v2.9.4" / "xmake"
    newer_cmake = newer / "modules" / "package" / "tools" / "cmake.lua"
    newer_cmake.parent.mkdir(parents=True)
    newer_cmake.write_text("-- 2.9.4 already maps v144 back\n", encoding="utf-8")
    assert xmake_env.patch_cmake_toolset_family(newer) is False
    assert newer_cmake.read_text(encoding="utf-8") == "-- 2.9.4 already maps v144 back\n"
