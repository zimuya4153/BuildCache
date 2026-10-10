"""Fast unit tests for the logic that used to be verified by a 15-minute run.

Run with ``pytest -q`` (pytest is installed automatically with the other
dependencies).
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import finalize  # noqa: E402
from lib import (  # noqa: E402
    aliases,
    hashprobe,
    index_store,
    manifest,
    plugins,
    recipefix,
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


def test_shipped_plugins_declare_their_consumer_profiles():
    """Every configured project has to say which toolchains its artifacts serve.

    The profiles are what the build recomputes hashes for, so a project that
    declares none silently serves only the toolchain it was built with.
    """
    for plugin in plugins.load_all(util.load_config()):
        profiles = plugin.call("toolchain_aliases", None, "26.51.6", "") or []
        found = {item["id"]: item["toolchain"] for item in profiles}
        assert found, f"{plugin.name} declares no consumer profile"
        # the shapes a consumer can declare: nothing (the built profile), msvc,
        # clang-cl -- see docs/tutorial.md, section 5
        assert set(found) >= {"msvc", "clang-cl"}, plugin.name
        assert all(found.values())
