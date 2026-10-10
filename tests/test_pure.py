"""Fast unit tests for the logic that used to be verified by a 15-minute run.

Run with ``pytest -q`` (pytest is installed automatically with the other
dependencies).
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from lib import index_store, manifest, recipefix, schemas, semver, util  # noqa: E402


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
