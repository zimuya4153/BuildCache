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
import cache  # noqa: E402
import discover  # noqa: E402
import finalize  # noqa: E402
from lib import (  # noqa: E402
    aliases,
    gh,
    hashprobe,
    index_store,
    manifest,
    plan,
    plugins,
    recipefix,
    reports,
    schemas,
    semver,
    sources,
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
    manifest.write_all(tmp_path, "fmt", "11.2.0", entries)
    assert manifest.read(tmp_path, "fmt", "11.2.0") == entries


def test_manifests_are_generated_from_the_ledger(tmp_path: Path):
    """The repo a build hands xmake and the one a consumer reads are one generator."""
    index_root = tmp_path / "index"
    repo = tmp_path / "binary-repo"
    artifact = {
        "package": "levilamina",
        "version": "26.51.6",
        "buildhash": "ee1902dd",
        "key": "windows-x64-vc-ee1902dd",
        "toolset": "14.44.35207",
        "url": "https://example.invalid/levilamina.7z",
        "sha256": "ab" * 32,
        "aliases": [
            {
                "id": "clang-cl",
                "buildhash": "df51e1a2",
                "key": "windows-x64-vc-df51e1a2",
                "toolset": "14.44.35207",
            }
        ],
    }
    index_store.write_entry(
        index_root, "levilamina", "26.51.6", "server", {"revision": 1, "artifacts": [artifact]}
    )

    written = manifest.write_repo(index_root, repo)

    assert written == {"levilamina": {"26.51.6"}}
    entries = manifest.read(repo, "levilamina", "26.51.6")
    assert set(entries) == {"windows-x64-vc-ee1902dd", "windows-x64-vc-df51e1a2"}
    # one archive, two addresses: the alias points at the same bytes
    assert entries["windows-x64-vc-df51e1a2"] == entries["windows-x64-vc-ee1902dd"]


def test_the_build_offers_the_published_manifests_to_xmake(tmp_path: Path, monkeypatch):
    """A dependency installs from the cache instead of being compiled again.

    xmake looks a manifest up in every repository it loaded, so the sandbox gets a
    repository generated from the ledger; without it LeviLamina is rebuilt once
    per plugin, every round.
    """
    config = dict(util.load_config())
    sandbox_root = tmp_path / "sandbox"
    index_root = tmp_path / "index"
    config["paths"] = {"sandboxRoot": str(sandbox_root)}
    config["_index_dir"] = str(index_root)
    revision = int(config["revision"])
    index_store.write_entry(
        index_root,
        "levilamina",
        "26.51.6",
        "server",
        {
            "revision": revision,
            "artifacts": [
                {
                    "package": "levilamina",
                    "version": "26.51.6",
                    "buildhash": "ee1902dd",
                    "key": "windows-x64-vc-ee1902dd",
                    "toolset": "14.44.35207",
                    "asset": "levilamina-ee1902dd.7z",
                    "url": "https://example.invalid/levilamina.7z",
                    "sha256": "ab" * 32,
                }
            ],
        },
    )
    context = plugins.Context(config)
    monkeypatch.setattr(plugins.Context, "ensure_index", lambda self, create=False: self.index_root)
    # what the release serves for that asset (the digests are what decides the entry)
    monkeypatch.setattr(
        "lib.verify.published_digests",
        lambda config, owner, repo: {"levilamina-ee1902dd.7z": {"sha256": "ab" * 32, "size": 7}},
    )

    repo = build.published_manifests(config, context)

    assert repo == sandbox_root / "binary-repo"
    assert manifest.read(repo, "levilamina", "26.51.6")
    # the directory has to exist even when nothing was written: xmake reads an
    # existing directory as a repository and anything else as a URL to clone
    assert repo.is_dir()
    empty = dict(config)
    empty["_index_dir"] = str(tmp_path / "no-index")
    assert build.published_manifests(empty, context).is_dir()
    # a bumped revision says the earlier build is stale: the build is not offered
    # the artifact it would otherwise silently keep reusing
    stale = dict(config)
    stale["revision"] = revision + 1
    assert manifest.read(build.published_manifests(stale, context), "levilamina", "26.51.6") == {}
    # a ledger that cannot be read leaves an empty repository behind, i.e. the
    # behaviour this had before: everything is compiled
    monkeypatch.setattr(
        plugins.Context, "ensure_index", lambda self, create=False: (_ for _ in ()).throw(OSError())
    )
    assert build.published_manifests(config, context) == repo
    assert manifest.read(repo, "levilamina", "26.51.6") == {}


def test_build_id_matches_xmake_contract():
    # xmake matches on "<plat>-<arch>-vc..." prefix and a "-<buildhash>" suffix.
    key = manifest.build_id("windows", "x64", "14.44.35207", "deadbeef")
    assert key.startswith("windows-x64-vc")
    assert key.endswith("-deadbeef")


# ------------------------------------------------------------------ index/store
def test_revision_gates_rebuilds(tmp_path: Path):
    index_store.write_entry(
        tmp_path, "fmt", "11.2.0", "server", {"revision": 1, "artifacts": []}
    )
    assert index_store.built_entries_at_revision(tmp_path, "fmt", "11.2.0", 1) == {"server"}
    # a fix bumps the config revision -> the entry counts as stale again
    assert index_store.built_entries_at_revision(tmp_path, "fmt", "11.2.0", 2) == set()


def test_a_document_written_before_entries_were_named_still_reads(tmp_path: Path):
    """Documents used `variants` before the entry dimension got its name."""
    util.write_json(
        tmp_path / "fmt" / "versions" / "11.2.0.json",
        {"package": "fmt", "version": "11.2.0", "variants": {"default": {"artifacts": []}}},
    )
    meta = index_store.version_meta(tmp_path, "fmt", "11.2.0")
    assert "default" in index_store.entries(meta)


def test_a_document_without_any_entry_is_empty_not_invented(tmp_path: Path):
    """Nothing generic may guess what a package's entries were called.

    A record that only carries top-level artifacts predates the entry dimension,
    and inventing one there would have to name a *project's* concept (LeviLamina's
    ``target_type``) for every package in the ledger.
    """
    util.write_json(
        tmp_path / "fmt" / "versions" / "11.2.0.json",
        {"package": "fmt", "version": "11.2.0", "artifacts": [{"key": "k"}]},
    )
    meta = index_store.version_meta(tmp_path, "fmt", "11.2.0")
    assert index_store.entries(meta) == {}
    assert index_store.all_artifacts(meta) == []


def test_entry_keeps_the_profiles_it_was_built_for(tmp_path: Path):
    """`discover` compares the declared profiles against what an entry records."""
    index_store.write_entry(
        tmp_path,
        "levilamina",
        "26.51.6",
        "server",
        {"revision": 1, "alias_profiles": ["msvc", "clang-cl"], "artifacts": []},
    )
    assert index_store.built_entries_with_profiles(
        tmp_path, "levilamina", "26.51.6", 1, ["msvc", "clang-cl"]
    ) == {"server"}
    # a plugin that starts declaring another profile queues the entry again
    assert (
        index_store.built_entries_with_profiles(
            tmp_path, "levilamina", "26.51.6", 1, ["msvc", "clang-cl", "gcc"]
        )
        == set()
    )


class _FakePlugin:
    """A plugin with the hooks a plan reads; the real ones talk to mirrors."""

    def __init__(self, name, package, *, versions=(), entries=(), specs=(), extra=(),
                 skip=(False, "")):
        self.name = name
        self._package = package
        self._versions = list(versions)
        self._entries = list(entries)
        self._specs = list(specs)
        self._extra = list(extra)
        self._skip = skip

    def call(self, function, *args, **kwargs):
        if function == "metadata":
            return {"package": self._package}
        if function == "list_versions":
            return [
                {"version": item["version"], "commit": item.get("commit", "")}
                for item in self._versions
            ]
        if function == "extra_deps":
            return [{"name": name, "version": version} for name, version in self._extra]
        if function == "dependency_specs":
            return list(self._specs)
        if function == "should_skip":
            return self._skip
        if function == "recipe_dir":
            return None
        if function == "toolchain_plan":
            return {}
        return None

    def entries(self, ctx, version, commit):
        return list(self._entries)


def _levilamina_plugin():
    return _FakePlugin(
        "ll",
        "levilamina",
        versions=[{"version": "1.9.9", "commit": "aaa"}],
        entries=[{"id": "server", "configs": {}}, {"id": "client", "configs": {}}],
    )


def _dependent(name, version, commit):
    return _FakePlugin(
        name,
        name,
        versions=[{"version": version, "commit": commit}],
        entries=[{"id": "default", "configs": {}}],
        specs=["levilamina >=1.9.2 <1.10.0"],
    )


def test_a_constraint_is_read_the_way_the_recipes_write_it():
    assert plan.satisfies("1.9.9", ">=1.9.2 <1.10.0")
    assert not plan.satisfies("1.8.0-rc.2", ">=1.9.2 <1.10.0")
    assert plan.satisfies("0.13.0", "0.13.*")
    assert not plan.satisfies("0.14.1", "0.13.*")
    assert plan.satisfies("26.40.3", ">=26.40 <27")
    assert plan.satisfies("1.4.4", "")
    assert plan.satisfies("1.4.4", "*")
    # `~`/`^` are not guessed: a wrong answer would plan the wrong SDK version
    import pytest

    with pytest.raises(ValueError):
        plan.satisfies("1.9.9", "~1.9")
    assert plan.pick(["1.9.2", "1.9.9", "1.8.0"], ">=1.9.2 <1.10.0") == "1.9.9"
    assert plan.pick(["1.9.2"], "~1.9") is None


def test_the_era_dependency_table_is_read(tmp_path: Path):
    """LeviLamina's plugins declare their SDK dependency per version range.

    `more-dimensions` has no `add_deps` at all: the dependency that makes it need
    LeviLamina 1.9.9 lives in the table keyed by `0.13.*`.
    """
    recipe = tmp_path / "packages" / "m" / "more-dimensions"
    recipe.mkdir(parents=True)
    (recipe / "xmake.lua").write_text(
        'package("more-dimensions")\n'
        "    local dependencies = {\n"
        '        ["0.13.*"] = {"levilamina >=1.9.2 <1.10.0"},\n'
        '        ["0.14.*"] = {"levilamina 26.20.*"},\n'
        "    }\n",
        encoding="utf-8",
    )
    assert plan.recipe_specs(recipe, "0.13.0") == ["levilamina >=1.9.2 <1.10.0"]
    assert plan.recipe_specs(recipe, "0.14.1") == ["levilamina 26.20.*"]
    assert plan.recipe_specs(recipe, "0.12.2") == []


def test_an_extra_require_the_recipe_already_declares_is_dropped(tmp_path: Path):
    """A project's own file and its recipe can pin different versions of one SDK.

    `more-dimensions` 0.13.0 names `levilamina 1.9.4` in its own xmake.lua while
    its recipe resolves to 1.9.9. Asking for both installs both -- xmake keeps the
    second as `levilamina_1` -- so the build downloads 1.9.9 from the cache and
    compiles a whole 1.9.4 nobody asked for. The recipe is what a consumer
    resolves, so it decides; only a dependency the recipe says nothing about (how
    `bsci` gets LeviLamina) survives.
    """
    recipe = tmp_path / "packages" / "m" / "more-dimensions"
    recipe.mkdir(parents=True)
    (recipe / "xmake.lua").write_text(
        'package("more-dimensions")\n'
        '        ["0.13.*"] = {"levilamina >=1.9.2 <1.10.0"},\n',
        encoding="utf-8",
    )
    pinned = _FakePlugin(
        "moredimensions", "more-dimensions", extra=[("levilamina", "1.9.4")]
    )
    assert plan.extra_deps(pinned, None, "0.13.0", "bbb", recipe) == []
    assert plan.dependency_specs(pinned, None, "0.13.0", "bbb", recipe) == [
        "levilamina >=1.9.2 <1.10.0"
    ]
    silent = _FakePlugin("bsci", "bsci", extra=[("levilamina", "26.51.0")])
    assert plan.extra_deps(silent, None, "26.40.0", "ccc", None) == [
        {"name": "levilamina", "version": "26.51.0"}
    ]


def test_an_sdk_the_ledger_lacks_is_built_ahead_of_its_plugins():
    """A plugin would otherwise compile the whole SDK itself, every round."""
    ll = _levilamina_plugin()
    md = _dependent("moredimensions", "0.13.0", "bbb")
    jobs = [plan.job({}, None, md, "0.13.0", "bbb", {"id": "default"})]

    first, dependencies, waiting, notes = plan.split(
        {}, None, [ll, md], jobs, lambda package, version: False
    )

    assert notes == []
    assert first == []
    assert [(item["plugin"], item["version"], item["entry"]) for item in dependencies] == [
        ("ll", "1.9.9", "server"),
        ("ll", "1.9.9", "client"),
    ]
    assert [item["plugin"] for item in waiting] == ["moredimensions"]
    assert waiting[0]["waits_for"] == ["levilamina@1.9.9"]


def test_a_published_sdk_is_reused_and_nothing_waits():
    ll = _levilamina_plugin()
    md = _dependent("moredimensions", "0.13.0", "bbb")
    jobs = [plan.job({}, None, md, "0.13.0", "bbb", {"id": "default"})]

    first, dependencies, waiting, notes = plan.split(
        {}, None, [ll, md], jobs, lambda package, version: package == "levilamina"
    )

    assert notes == []
    assert dependencies == []
    assert waiting == []
    assert [(item["plugin"], item["version"]) for item in first] == [("moredimensions", "0.13.0")]


def test_two_dependents_share_one_sdk_build():
    ll = _levilamina_plugin()
    one = _dependent("bsci", "26.40.0", "ccc")
    two = _dependent("moredimensions", "0.13.0", "ddd")
    jobs = [
        plan.job({}, None, one, "26.40.0", "ccc", {"id": "default"}),
        plan.job({}, None, two, "0.13.0", "ddd", {"id": "default"}),
    ]

    first, dependencies, waiting, _notes = plan.split(
        {}, None, [ll, one, two], jobs, lambda p, v: False
    )

    assert first == []
    assert [(item["plugin"], item["version"]) for item in dependencies] == [
        ("ll", "1.9.9"),
        ("ll", "1.9.9"),
    ]
    assert len(waiting) == 2


def test_a_build_a_dependent_waits_for_is_not_run_beside_it():
    """The plugin's own queue can pick the very version a dependent needs.

    That build then has to move out of the free-running group: the dependents read
    the results of the builds they wait for, so a job running *beside* them would
    leave them without the artifact they asked for -- and they would compile it
    themselves, which is the whole thing being avoided.
    """
    ll = _levilamina_plugin()
    md = _dependent("moredimensions", "0.13.0", "bbb")
    jobs = [
        plan.job({}, None, ll, "1.9.9", "aaa", {"id": "server"}),
        plan.job({}, None, md, "0.13.0", "bbb", {"id": "default"}),
    ]

    first, dependencies, waiting, _notes = plan.split(
        {}, None, [ll, md], jobs, lambda package, version: False
    )

    assert first == []
    assert [(item["plugin"], item["version"], item["entry"]) for item in dependencies] == [
        ("ll", "1.9.9", "server")
    ]
    assert len(waiting) == 1


def test_a_dependency_that_cannot_be_resolved_does_not_block_anything():
    """Nothing waits for something nobody can name: it builds as it always did."""
    ll = _levilamina_plugin()
    md = _FakePlugin(
        "moredimensions",
        "more-dimensions",
        versions=[{"version": "0.13.0", "commit": "bbb"}],
        entries=[{"id": "default", "configs": {}}],
        specs=["levilamina ~1.9"],
    )
    jobs = [plan.job({}, None, md, "0.13.0", "bbb", {"id": "default"})]

    first, dependencies, waiting, notes = plan.split(
        {}, None, [ll, md], jobs, lambda package, version: False
    )

    assert waiting == []
    assert dependencies == []
    assert [item["plugin"] for item in first] == ["moredimensions"]
    assert notes and "cannot resolve" in notes[0]


def test_only_the_eras_that_build_with_llvm_22_download_it():
    """LeviLamina moved to LLVM 22 in August 2026, first released with 26.32.0.

    The compiler of an era is part of what the era *is*: 26.32.0 and later need
    LLVM 22 installed on top of the image, everything before them is built by the
    compiler the image already carries. A plugin that does not say gets the safe
    answer (install it), which is what every round did before this existed.
    """
    ll = next(item for item in plugins.load_all(util.load_config()) if item.name == "ll")
    assert ll.module.needs_llvm22("26.51.6")
    assert ll.module.needs_llvm22("26.32.0")
    assert not ll.module.needs_llvm22("26.20.7")
    assert not ll.module.needs_llvm22("1.9.9")
    # a branch is not a version: the newest rules apply
    assert ll.module.needs_llvm22("main")

    silent = _FakePlugin(
        "x", "x", versions=[{"version": "1.0.0", "commit": "a"}],
        entries=[{"id": "default", "configs": {}}],
    )
    assert plan.job({}, None, silent, "1.0.0", "a", {"id": "default"})["llvm"] is True


def test_every_plugin_repairs_the_snapshot_not_only_the_one_being_built(monkeypatch, tmp_path: Path):
    """A dependency is built from this snapshot too, and its repairs belong to its owner.

    `bsci` builds LeviLamina, and the era data packages LL is built from (four
    component versions xmake cannot parse) are repaired by the LeviLamina plugin --
    not by the plugin being built. Repairing them only when LL itself is the
    project left `bsci 0.1.6` unable to create `bedrock_runtime_api.lib`.
    """
    calls = []

    class Other:
        name = "ll"

        def repair_recipes(self, config, source_name, snapshot):
            calls.append((self.name, source_name))
            return [f"{self.name} repaired something"]

    monkeypatch.setattr("lib.plugins.load_all", lambda config: [Other()])
    building = type("B", (), {"name": "bsci", "module": object()})()

    notes = recipefix.plugin_repairs({}, building, "levimc-repo", tmp_path)

    assert calls == [("ll", "levimc-repo")], calls
    assert notes == ["ll repaired something"]
    # the plugin being built is not asked twice
    monkeypatch.setattr("lib.plugins.load_all", lambda config: [])
    assert recipefix.plugin_repairs({}, building, "levimc-repo", tmp_path) == []


def test_a_version_without_a_commit_is_given_the_one_its_tag_has(tmp_path: Path, monkeypatch):
    """`add_versions("0.12.1")` leaves xmake cloning *by tag*, which fails upstream.

    The repository tags its releases `v0.12.1`, so the clone dies with "Remote
    branch 0.12.1 not found in upstream origin" -- before a single file is
    compiled, and saying nothing about the recipe. The commit is looked up from
    the repository's own tags; a version whose tag is not there is left alone.
    """
    root = tmp_path / "levimc-repo@test"
    recipe = root / "packages" / "m" / "more-dimensions" / "xmake.lua"
    recipe.parent.mkdir(parents=True)
    recipe.write_text(
        'package("more-dimensions")\n'
        '    add_urls("https://github.com/LiteLDev/MoreDimensions.git")\n'
        '    add_versions("0.12.1")\n'
        '    add_versions("0.13.0", "3d89965e0b5208aff1462a2bb09f97bb6034cfb0")\n'
        '    add_versions("0.9.0")\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        recipefix,
        "_tags_of",
        lambda url: {"v0.12.1": "25db4b9319db98c37323c952a036d8b7a8d73a8f"},
    )

    notes = recipefix.backfill_commits(root)

    assert len(notes) == 1, notes
    text = util.read_text(recipe)
    assert 'add_versions("0.12.1", "25db4b9319db98c37323c952a036d8b7a8d73a8f")' in text
    # the one that already had a commit is untouched, and the one with no tag is
    # left exactly as it was (a clone failure is better than a wrong commit)
    assert 'add_versions("0.13.0", "3d89965e0b5208aff1462a2bb09f97bb6034cfb0")' in text
    assert 'add_versions("0.9.0")' in text
    # idempotent: nothing left to backfill
    assert recipefix.backfill_commits(root) == []


def test_the_git_shim_retries_a_ref_upstream_spells_another_way():
    """A recipe may name `0.4.0` where upstream tags it `v0.4.0`.

    xmake then asks git for a ref that does not exist and the clone dies before
    anything is built -- the era plan meets this, because the recipe as it was on
    that date had not pinned the commit yet, and nothing in the log says so. The
    shim tries the "v", and then clones without a ref at all, which its own pin()
    rewinds to the cutoff.
    """
    from lib import git_shim_impl as shim

    argv = ["clone", "https://x/y.git", "-b", "0.4.0", "--depth", "1", "dir"]
    assert shim.with_v(argv) == ["clone", "https://x/y.git", "-b", "v0.4.0", "--depth", "1", "dir"]
    assert shim.without_ref(argv) == ["clone", "https://x/y.git", "--depth", "1", "dir"]
    # a long form, and a clone that names no ref at all
    assert shim.with_v(["clone", "--branch=1.2.3", "u"]) == ["clone", "--branch=v1.2.3", "u"]
    plain = ["clone", "https://x/y.git", "dir"]
    assert shim.with_v(plain) == plain
    assert shim.without_ref(plain) == plain
    assert shim.clone_target(["u.git", "dir"]) == "dir"


def test_a_dependency_without_a_version_takes_the_one_of_its_era(monkeypatch, tmp_path: Path):
    """`bsci` 0.4.0 says `add_requires("levilamina")` -- no version at all.

    Resolved against today's recipes that is the newest SDK there is (26.51.6 at
    the time), whose headers its sources were never compiled against; resolved
    against the recipes *as they were on the day it was released*, it is 26.20.0,
    which is what it was built with. The rollback the recipe snapshot does for the
    project is applied to that one dependency.
    """
    mirror = tmp_path / "levimc-repo"
    recipe = mirror / "packages" / "l" / "levilamina"
    (recipe / "versions").mkdir(parents=True)
    (recipe / "versions" / "versions.txt").write_text(
        "1.9.9 aaa\n26.20.0 bbb\n26.51.6 ccc\n", encoding="utf-8"
    )
    monkeypatch.setattr(plan.repos, "commit_at", lambda directory, cutoff=None, **kw: "era")
    monkeypatch.setattr(
        plan.repos,
        "file_at",
        lambda directory, commit, path, **kw: util.read_text(Path(directory) / path),
    )
    config = {"recipes": {"sources": [{"name": "levimc-repo", "url": "x"}]}}
    ctx = type("Ctx", (), {"mirror": staticmethod(lambda name, url: mirror)})()

    # the file *is* the set of versions at that moment, so the newest in it is the
    # newest of that era
    assert plan.era_version(config, ctx, "levilamina", "2026-07-10T00:00:00Z") == "26.51.6"
    # a build with no cutoff has no era to look up
    assert plan.era_version(config, ctx, "levilamina", "") is None


def test_a_version_only_one_plugin_tracks_does_not_stop_the_round(monkeypatch, tmp_path: Path):
    """`-f version=X` is routinely meant for one plugin out of several.

    The input pins the round to that version; plugins that have no such version are
    simply not part of it. Failing the whole discovery instead meant a version was
    only usable together with `-f plugin=`, so "this SDK version, whichever plugin
    owns it" could not be said at all.
    """
    import pytest

    ll = _FakePlugin(
        "ll",
        "levilamina",
        versions=[{"version": "26.20.7", "commit": "aaa"}],
        entries=[{"id": "server", "configs": {}}],
    )
    md = _FakePlugin(
        "moredimensions",
        "more-dimensions",
        versions=[{"version": "0.14.1", "commit": "bbb"}],
        entries=[{"id": "default", "configs": {}}],
    )
    monkeypatch.setattr(discover.plugins, "load_all", lambda config: [ll, md])
    monkeypatch.setattr(
        discover.plugins.Context, "ensure_index", lambda self, create=False: self.index_root
    )
    monkeypatch.setattr(
        discover.util,
        "load_config",
        lambda: {"repos": {}, "revision": 1, "batch": {}, "_index_dir": str(tmp_path / "index")},
    )
    out = tmp_path / "matrix.json"
    monkeypatch.setattr(
        discover.sys, "argv", ["discover.py", "--version", "26.20.7", "--out", str(out)]
    )

    assert discover.main() == 0
    assert [(item["plugin"], item["version"]) for item in util.read_json(out, [])] == [
        ("ll", "26.20.7")
    ]

    # a version nobody tracks is still an error worth reporting
    monkeypatch.setattr(
        discover.sys, "argv", ["discover.py", "--version", "9.9.9", "--out", str(out)]
    )
    with pytest.raises(SystemExit):
        discover.main()


def test_the_revision_gate_decides_whether_a_dependency_can_be_reused(tmp_path: Path):
    index_root = tmp_path / "index"
    index_store.write_entry(
        index_root, "levilamina", "1.9.9", "server", {"revision": 4, "artifacts": [{"key": "k"}]}
    )
    assert index_store.has_artifacts(index_root, "levilamina", "1.9.9", 4)
    # built by an older revision: a build is not offered it, so it cannot reuse it
    assert not index_store.has_artifacts(index_root, "levilamina", "1.9.9", 5)
    assert not index_store.has_artifacts(index_root, "levilamina", "1.9.8", 4)
    # the same question for a whole package, which is what tells one release line
    # from another
    assert index_store.versions_with_artifacts(index_root, "levilamina", 4) == {"1.9.9"}
    assert index_store.versions_with_artifacts(index_root, "levilamina", 5) == set()


def test_a_job_reuses_what_the_round_just_published(tmp_path: Path):
    """The SDK stage's results become manifests for the stage that depends on them."""
    def artifact(package, key, asset):
        return {
            "package": package,
            "version": "11.2.0",
            "buildhash": key,
            "key": key,
            "asset": asset,
            "url": f"https://x/{asset}",
            "sha256": "ab" * 32,
        }

    repo = tmp_path / "binary-repo"
    manifest.write_all(
        repo,
        "levilamina",
        "1.9.9",
        {"windows-x64-vc-old": {"urls": "https://x/old.7z", "sha256": "ff" * 32}},
    )
    fresh = {
        "package": "levilamina",
        "version": "1.9.9",
        "entry": "server",
        "artifacts": [
            {
                "package": "levilamina",
                "version": "1.9.9",
                "buildhash": "new",
                "key": "windows-x64-vc-new",
                "url": "https://x/new.7z",
                "sha256": "ab" * 32,
                "toolset": "14.51.36231",
                "aliases": [{"key": "windows-x64-vc-new-alias", "toolchain": "clang-cl"}],
            }
        ],
    }

    written = manifest.write_results([fresh], repo)

    assert written == {"levilamina": {"1.9.9"}}
    entries = manifest.read(repo, "levilamina", "1.9.9")
    # merged: the address the ledger had is still there
    assert set(entries) == {"windows-x64-vc-old", "windows-x64-vc-new", "windows-x64-vc-new-alias"}
    assert entries["windows-x64-vc-new-alias"] == entries["windows-x64-vc-new"]

    # and the digest is the one the release serves, not the one the result recorded
    lost = [dict(fresh, artifacts=[artifact("fmt", "windows-x64-vc-h", "fmt-h.7z")])]
    written = manifest.write_results(
        lost, repo, digests={"fmt-h.7z": {"sha256": "cd" * 32, "size": 7}}
    )
    assert written == {"fmt": {"11.2.0"}}
    assert manifest.read(repo, "fmt", "11.2.0")["windows-x64-vc-h"]["sha256"] == "cd" * 32
    # an artifact the release does not serve is left out: no entry means "build it",
    # while an entry pointing at nothing stops xmake with "unmatched checksum"
    missing = [dict(fresh, artifacts=[artifact("gone", "windows-x64-vc-x", "gone.7z")])]
    assert manifest.write_results(missing, repo, digests={}) == {}
    assert manifest.read(repo, "gone", "11.2.0") == {}


# ------------------------------------------------------------------------ config
def test_shipped_config_is_valid():
    config = schemas.validate(schemas.Config, util.load_config())
    assert config.recipes.snapshotMode == "version"
    assert config.xmake.installRetries >= 1
    assert config.bot.email.endswith("users.noreply.github.com")


def test_a_config_that_misses_a_repository_is_rejected():
    import pydantic
    import pytest

    with pytest.raises(pydantic.ValidationError):
        schemas.validate(
            schemas.Config, {"repos": {"cache": {"owner": "o", "repo": "r"}}}
        )


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
        "package": "fmt",
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
        index_root, "fmt", "11.2.0", "server", {"revision": 1, "artifacts": [artifact]}
    )
    config = util.load_config()
    finalize.regenerate_manifests(
        config,
        plugins.Context(config),
        index_root=index_root,
        package_repo=package_repo,
    )
    entries = manifest.read(package_repo, "fmt", "11.2.0")
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
    # the columns a reader looks up come first, the build details last
    assert table.render([own_row])[1] == (
        "| version | entry | asset | compiled with | also addressable as "
        "| build hash | toolset |"
    )

    dependency = "\n".join(table.render([dependency_row]))
    assert "| entry |" not in dependency
    assert "| compiled with |" not in dependency
    assert "| version |" in dependency and "| asset |" in dependency
    assert table.render([dependency_row])[1] == "| version | asset | build hash |"


def test_table_manager_adds_and_removes_columns():
    table = reports.Table()
    # What a reader looks up comes first, how it was built trails behind.
    assert table.keys() == ["version", "asset", "buildhash", "toolset"]
    table.add("variant", title="variant", render=reports.code)
    assert table.has("variant")
    assert table.keys() == ["version", "asset", "variant", "buildhash", "toolset"]
    # a column names its own slot: an entry belongs beside the version
    table.add("variant", title="variant", render=reports.code, order=reports.ORDER_ENTRY)
    assert table.keys() == ["version", "variant", "asset", "buildhash", "toolset"]
    # registering the same key twice replaces instead of duplicating
    table.add("variant", title="build entry", order=reports.ORDER_ENTRY)
    assert table.keys().count("variant") == 1
    assert table.remove("variant").has("variant") is False
    assert table.keys() == ["version", "asset", "buildhash", "toolset"]


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


def test_manifests_of_removed_packages_are_dropped(tmp_path: Path):
    """A manifest points at a released asset, so it cannot outlive the entry.

    The recipe stays behind (a package without a manifest simply builds from
    source); only the directory that holds nothing but a manifest goes. Files a
    recipe owns -- its ``xmake.lua``, its ``versions/`` listing -- are untouched.
    """
    config = dict(util.load_config())
    config["_index_dir"] = str(tmp_path / "index")
    ctx = plugins.Context(config)
    repo = tmp_path / "repo"

    meta = {
        "package": "fmt",
        "version": "11.2.0",
        "artifacts": [
            {
                "package": "fmt",
                "version": "11.2.0",
                "buildhash": "ee1902dd7cff4195a287949f80dbce70",
                "key": "windows-x64-vc-ee1902dd7cff4195a287949f80dbce70",
                "toolset": "14.44.35207",
                "url": "https://example.invalid/fmt.7z",
                "sha256": "ab" * 32,
                "aliases": [],
            }
        ],
    }
    index_store.write_entry(ctx.index_root, "fmt", "11.2.0", "default", meta)
    # a package the cache no longer holds, and the recipe that must survive it
    stale = repo / "packages" / "e" / "entt"
    (stale / "v3.13.1").mkdir(parents=True)
    (stale / "v3.13.1" / "manifest.txt").write_text("{}", encoding="utf-8")
    (stale / "xmake.lua").write_text('package("entt")\n', encoding="utf-8")
    (stale / "versions").mkdir()
    (stale / "versions" / "versions.txt").write_text("v3.13.1 abc\n", encoding="utf-8")
    # a package the cache no longer holds: nothing in the repository says that
    # name is ours, but the directory holds nothing but a manifest, which is what
    # makes it ours to remove.
    upstream = repo / "packages" / "l" / "levilamina"
    (upstream / "0.8.3").mkdir(parents=True)
    (upstream / "0.8.3" / "manifest.txt").write_text("{}", encoding="utf-8")

    finalize.regenerate_manifests(config, ctx, ctx.index_root, repo)

    assert (repo / "packages" / "f" / "fmt" / "11.2.0" / "manifest.txt").is_file()
    assert (stale / "v3.13.1").exists() is False
    assert (stale / "xmake.lua").is_file() and (stale / "versions" / "versions.txt").is_file()
    assert (upstream / "0.8.3").exists() is False
    assert upstream.exists() is False


def test_removing_one_entry_leaves_the_other_in_the_document(tmp_path: Path):
    """`cache.py remove` rewrites the entry map under the key readers use.

    A generated document that keeps both the current and the legacy key would be
    read from whichever copy a reader happens to prefer, so the removal
    normalises it: the entry it dropped is gone, the other one is untouched.
    """
    index_root = tmp_path / "index"
    for entry_id in ("server", "client"):
        index_store.write_entry(
            index_root, "levilamina", "26.51.6", entry_id, {"revision": 1, "artifacts": []}
        )
    context = plugins.Context({**util.load_config(), "_index_dir": str(index_root)})

    cache._drop_from_index(context, [("levilamina", "26.51.6", "client", [])])

    document = util.read_json(index_root / "levilamina" / "versions" / "26.51.6.json", None)
    assert set(index_store.entries(document)) == {"server"}
    assert "variants" not in document


def test_a_failed_upload_is_retried(tmp_path: Path, monkeypatch):
    """A build must not fail because a shard was being created at that moment.

    Rounds share their dependencies, so two jobs can meet the same release while
    one of them is still creating it ("HTTP 404" on the upload endpoint). The
    archive is re-attached after re-resolving the release.
    """
    class Result:
        returncode = 1
        stdout = ""
        stderr = "HTTP 404: Not Found"

    calls = {"n": 0}

    def fake_run(args, **kwargs):
        calls["n"] += 1
        result = Result()
        result.returncode = 0 if calls["n"] >= 3 else 1
        return result

    monkeypatch.setattr(gh.util, "run", fake_run)
    monkeypatch.setattr(gh.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(gh, "ensure_release", lambda *args, **kwargs: None)
    asset = tmp_path / "fmt.7z"
    asset.write_bytes(b"archive")

    gh.upload_asset("owner", "repo", "fmt_0", asset, config=util.load_config())
    assert calls["n"] == 3


def test_an_artifact_already_published_is_not_packed_again(tmp_path: Path):
    """The build hash is the identity of a build, so a published one is reused.

    Re-packing it would spend minutes and, when two rounds share a dependency,
    upload a second set of bytes under an asset name the manifest already
    describes -- the mismatch a consumer sees as "unmatched checksum". The record
    has to come from the current revision, which is how a framework change says
    that everything built before it is stale.
    """
    config = dict(util.load_config())
    config["_index_dir"] = str(tmp_path / "index")
    config["revision"] = 7
    ctx = plugins.Context(config)
    record = {
        "package": "fmt",
        "version": "11.2.0",
        "revision": 7,
        "artifacts": [
            {
                "package": "fmt",
                "version": "11.2.0",
                "buildhash": "ee1902dd7cff4195a287949f80dbce70",
                "key": "windows-x64-vc-ee1902dd7cff4195a287949f80dbce70",
                "url": "https://example.invalid/fmt.7z",
                "sha256": "ab" * 32,
            }
        ],
    }
    index_store.write_entry(ctx.index_root, "fmt", "11.2.0", "default", record)

    found = build.cached_artifact(
        ctx, "fmt", "11.2.0", "ee1902dd7cff4195a287949f80dbce70", 7
    )
    assert found and found["sha256"] == "ab" * 32
    # a different build, or one from an older revision, is packed instead
    assert build.cached_artifact(ctx, "fmt", "11.2.0", "deadbeef", 7) is None
    assert build.cached_artifact(
        ctx, "fmt", "11.2.0", "ee1902dd7cff4195a287949f80dbce70", 8
    ) is None
    assert build.cached_artifact(ctx, "fmt", "10.2.1", "ee1902dd", 7) is None


def test_packing_is_reproducible(tmp_path: Path, monkeypatch):
    """The same package built again must pack to the same bytes.

    An artifact is re-packed by every round that shares it and uploaded under the
    same name, so two archives that differ only by the build's file timestamps
    would leave the published checksum pointing at whichever upload was last --
    a consumer then reports "unmatched checksum" and falls back to a source build.
    """
    from lib import packer

    source = tmp_path / "install"
    (source / "include").mkdir(parents=True)
    (source / "include" / "fmt.h").write_text("//\n", encoding="utf-8")
    calls: list[list[str]] = []

    def fake_run(args, **kwargs):
        calls.append(list(args))
        Path(args[-2]).write_bytes(b"archive")
        return None

    monkeypatch.setattr(packer.util, "run", fake_run)
    packer._pack_7z(source, tmp_path / "fmt.7z", "f/fmt/11.2.0/deadbeef")
    assert calls and calls[0][:4] == ["7z", "a", "-t7z", "-mx=9"]
    # modification, creation and access times are all left out of the archive
    assert {"-mtm=off", "-mtc=off", "-mta=off"} <= set(calls[0])



def test_a_recent_version_needs_one_pass_only(tmp_path: Path, monkeypatch):
    """Plan selection: today's recipes, unless the era already is today.

    A version whose release date is so recent that nothing moved in the recipe
    repositories is built against today's recipes by definition, so the fallback
    plan (the era's own recipes) is not even offered.
    """
    repo = tmp_path / "recipe-source"
    repo.mkdir()
    config = dict(util.load_config())
    config["recipes"] = {"sources": [{"name": "test-repo", "url": str(repo)}]}
    ctx = plugins.Context(config)
    monkeypatch.setattr(ctx, "mirror", lambda name, url: repo)

    monkeypatch.setattr(
        sources.repos, "commit_at", lambda mirror, cutoff=None, **kwargs: cutoff or "head"
    )
    assert sources.sources_changed(config, ctx, "2025-01-01") is True

    monkeypatch.setattr(
        sources.repos, "commit_at", lambda mirror, cutoff=None, **kwargs: "same"
    )
    assert sources.sources_changed(config, ctx, "2025-01-01") is False
    # no cutoff means "no era to speak of"
    assert sources.sources_changed(config, ctx, "") is False


def test_the_consumer_dependency_list_is_read_from_the_upstream_recipe(monkeypatch):
    """The dependency pass has to ask for what a consumer asks for.

    A consumer resolves the version out of the upstream (today's) recipe, so that
    version file is the list; the dependencies only the project's own xmake.lua
    names are added on top, because our artifacts record them and a consumer ends
    up installing them too.
    """
    from lib import repos

    ll = next(item for item in plugins.load_all(util.load_config()) if item.name == "ll")
    file_text = (
        "local deps_common = {\n"
        '    "entt v3.14.0",\n'
        '    "fmt 11.2.0"\n'
        "}\n"
        "function load(package)\n"
        '    if package:config("target_type") == "server" then\n'
        '        package:add("deps", "bedrockdata v1.21.93-server.6")\n'
        "    end\n"
        "end\n"
    )
    monkeypatch.setattr(repos, "file_at", lambda *args, **kwargs: file_text)
    monkeypatch.setattr(
        ll.module,
        "upstream_xmake_lua",
        lambda *args, **kwargs: 'add_requires("mimalloc")\nadd_requires("cpr[ssl=y] 1.11.1")\n',
    )

    ctx = plugins.Context(dict(util.load_config()))
    specs = ll.module.dependency_specs(ctx, "1.4.4", "deadbeef")
    # only the dependency specs survive the loader around them
    assert specs == [
        "entt v3.14.0",
        "fmt 11.2.0",
        "bedrockdata v1.21.93-server.6",
        "cpr 1.11.1",
        "mimalloc",
    ], specs


def test_dependencies_come_from_today_and_the_project_from_its_era(tmp_path: Path):
    """The artifact must land on an address a consumer asks for.

    A consumer resolves the dependencies from the recipes of today, so those are
    what this build has to use; the project's own recipe stays the one of its era,
    because that is what pins the dependency versions (and what our repairs are
    written for). The snapshot is a clone of the mirror with the whole history, so
    the older state of one directory is a checkout away.
    """
    import os
    import subprocess

    def git(*args, date=None):
        env = dict(os.environ)
        if date:
            env.update(GIT_AUTHOR_DATE=date, GIT_COMMITTER_DATE=date)
        subprocess.run(
            ["git", "-C", str(repo), *args], check=True, capture_output=True, env=env
        )

    repo = tmp_path / "source"
    (repo / "packages" / "l" / "levilamina").mkdir(parents=True)
    (repo / "packages" / "f" / "fmt").mkdir(parents=True)
    git("init", "-q")
    (repo / "packages" / "l" / "levilamina" / "xmake.lua").write_text("era\n", encoding="utf-8")
    (repo / "packages" / "f" / "fmt" / "xmake.lua").write_text("old\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-q", "-m", "era", date="2024-01-01T00:00:00")
    (repo / "packages" / "l" / "levilamina" / "xmake.lua").write_text("head\n", encoding="utf-8")
    (repo / "packages" / "f" / "fmt" / "xmake.lua").write_text("new\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-q", "-m", "head", date="2026-01-01T00:00:00")

    when = sources._overlay_from_cutoff(repo, "2025-01-01", "packages/l/levilamina")
    assert when
    # the project's recipe went back to the era, its dependencies did not
    assert (repo / "packages" / "l" / "levilamina" / "xmake.lua").read_text(
        encoding="utf-8"
    ) == "era\n"
    assert (repo / "packages" / "f" / "fmt" / "xmake.lua").read_text(
        encoding="utf-8"
    ) == "new\n"
    # a source that does not carry that package is simply skipped
    assert sources._overlay_from_cutoff(repo, "2025-01-01", "packages/x/nothing") == ""


def test_the_generated_checkouts_carry_the_push_credential(tmp_path: Path, monkeypatch):
    """Every checkout we make ends in a push, so its remote has to carry a token.

    A remote without one makes git fall back to the environment's credential --
    in CI the workflow's own token, which cannot write the *second* repository,
    and the push then comes back as "denied to github-actions[bot]".
    """
    from lib import gitops

    monkeypatch.setenv("BUILDCACHE_PUSH_TOKEN", "pat")
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    cloned: list[list[str]] = []

    def fake_run(args, **kwargs):
        cloned.append([str(item) for item in args])
        return None

    monkeypatch.setattr(gitops.util, "run", fake_run)
    gitops.ensure_checkout(
        {"_index_dir": str(tmp_path / "index")},
        "https://github.com/o/r.git",
        tmp_path / "checkout",
    )
    assert cloned and cloned[0][:2] == ["git", "clone"]
    assert cloned[0][2] == "https://x-access-token:pat@github.com/o/r.git"


def test_push_url_uses_the_workflow_token_when_there_is_no_push_token(monkeypatch):
    """A job that only touches its own repository pushes with the workflow token.

    The push token exists for the *second* repository; the branch that release tags
    are created on lives in our own, so the workflow's own token is the right
    credential there. Without any embedded token git asks for one, and a runner
    answers ``failed to execute prompt script``.
    """
    from lib import gitops

    url = "https://github.com/o/r.git"
    for name in ("BUILDCACHE_PUSH_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    assert gitops.push_url(url) == url

    monkeypatch.setenv("GH_TOKEN", "workflow")
    assert gitops.push_url(url) == "https://x-access-token:workflow@github.com/o/r.git"

    # where both are available the push token wins for a *foreign* repository
    monkeypatch.setenv("BUILDCACHE_PUSH_TOKEN", "pat")
    assert gitops.push_url(url) == "https://x-access-token:pat@github.com/o/r.git"

    # ... and the workflow token wins for the repository the workflow runs in,
    # because a token of another account has no write access there
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    assert gitops.push_url(url) == "https://x-access-token:workflow@github.com/o/r.git"

    # and a foreign host is never rewritten
    assert gitops.push_url("https://example.invalid/o/r.git") == "https://example.invalid/o/r.git"


def test_every_release_line_gets_a_build_before_any_gets_two():
    """A backlog is covered line by line first, patches after.

    Patch levels of one line differ in which upstream commits they carry; lines
    differ in what the SDK is built from. Covering lines first turns a backlog
    into broad coverage quickly, and the remaining patches then fill in what is
    missing -- the middle patch of each line being the representative, and the
    order stable so that "the next few" means the same thing on every run.
    """
    pending = [
        "26.51.6", "26.51.5", "26.51.4", "26.51.2", "26.51.1", "26.51.0",
        "26.40.6", "26.40.5",
        "1.4.4", "1.4.3",
        "0.8.3",
    ]
    ordered = discover.cover_lines_first(pending)
    # one per line first: 26.51 has six members (middle-biased -> 3rd newest),
    # 26.40 two (the newer), 1.4 two (the newer), 0.8 one
    assert ordered[:4] == ["26.51.4", "26.40.6", "1.4.4", "0.8.3"], ordered
    # then the leftovers, still newest first, so patches come last
    assert ordered[4:] == ["26.51.6", "26.51.5", "26.51.2", "26.51.1", "26.51.0", "26.40.5", "1.4.3"]
    # stable: the same input always yields the same order
    assert discover.cover_lines_first(pending) == ordered


def test_a_line_the_ledger_already_covers_comes_last():
    """`discover` takes the first two, so the order has to move down the lines.

    With 26.51 (26.51.4, 26.51.6) and 26.40 (26.40.3) already built, a round that
    re-represented those lines would pick 26.51.x and 26.40.x again -- and the
    round after that, and every round after, so 26.32, 26.20, ... and 1.9 would
    never be reached. A line the ledger has nothing for comes first instead.
    """
    pending = [
        "26.51.6", "26.51.4", "26.40.6", "26.40.3", "26.32.2", "26.32.0", "26.20.7",
    ]
    assert discover.cover_lines_first(pending)[:2] == ["26.51.6", "26.40.6"]
    covered = {"26.51.6", "26.51.4", "26.40.3"}
    assert discover.cover_lines_first(pending, covered)[:2] == ["26.32.2", "26.20.7"]
    # and once those are built, the next lines follow
    assert discover.cover_lines_first(pending, covered | {"26.32.2"})[:1] == ["26.20.7"]


def test_a_version_the_recipe_dropped_is_not_swapped_for_a_newer_sdk(tmp_path: Path):
    """A project pinning an SDK that old is written against its own era.

    `bsci` 0.4.0 names a LeviLamina its recipe no longer declares; asking for "the
    newest" instead is how it ended up compiled against 26.51.6, whose headers its
    sources never saw. Nothing extra is asked for then, so the failure is the
    project's own and happens where it means something.
    """
    recipe = tmp_path / "packages" / "l" / "levilamina"
    recipe.mkdir(parents=True)
    (recipe / "xmake.lua").write_text(
        'package("levilamina")\n    add_versions("1.9.9", "abc")\n', encoding="utf-8"
    )
    sources = {"levimc-repo": tmp_path}

    assert build._declared_version(sources, "levilamina", "1.9.9") == "1.9.9"
    # a range is left to xmake
    assert build._declared_version(sources, "levilamina", ">=1.9.2 <1.10.0") == ">=1.9.2 <1.10.0"
    # nothing to ask for, and deliberately not the newest either
    assert build._declared_version(sources, "levilamina", "1.9.4") is None
    assert build._declared_version(sources, "unknown-package", "1.0.0") is None


def test_versions_a_project_gives_up_on_are_not_queued():
    """`bsci` 0.1.x-0.4.0 do not compile with a current compiler at all.

    Their own sources fail (clang rejects a copy constructor defined after the
    implicit one is used), whatever SDK they link. Queued, they would take a job
    every round and fail it, so the project declares them out of its version list.
    """
    def config_plugin(**settings):
        config = {
            "plugins": [
                {
                    "name": "bsci",
                    "module": "plugins/upstream.py",
                    "settings": dict(recipe_package="bsci", **settings),
                }
            ]
        }
        return plugins.load_all(config)[0].module

    plugin = config_plugin(skipBelow="1.0.0")
    found = plugin._kept(
        [
            {"version": "26.32.2", "commit": "a"},
            {"version": "0.4.0", "commit": "b"},
            {"version": "1.0.0", "commit": "c"},
        ]
    )
    assert [item["version"] for item in found] == ["26.32.2", "1.0.0"]
    named = config_plugin(skipVersions=["26.32.2"])
    assert [item["version"] for item in named._kept([{"version": "26.32.2"}])] == []
    # nothing declared, nothing filtered
    plain = config_plugin()
    assert plain._kept([{"version": "0.4.0"}]) == [{"version": "0.4.0"}]


def test_the_extra_closure_is_built_not_only_declared(tmp_path: Path):
    """Deps that only the project's own xmake.lua names are part of the closure.

    A consumer resolves them (the recipe we publish declares them) and compiles
    them from source today, because the export of a build only covered the recipe's
    own dependency list. They are asked for at the top level here, which is what
    makes ``require --export`` include them.
    """
    config = dict(util.load_config())
    ctx = plugins.Context(config)
    repo = tmp_path / "levimc-repo@test"
    for name, versions in {"cpr": ["1.11.1"], "libhat": ["v0.4.0"], "trampoline": ["2024.11.7"]}.items():
        recipe = repo / "packages" / name[0] / name
        recipe.mkdir(parents=True)
        declared = "".join(f'    add_versions("{item}", "{"ab" * 32}")\n' for item in versions)
        (recipe / "xmake.lua").write_text(
            f'package("{name}")\n' + declared, encoding="utf-8"
        )

    lines = build.extra_requires(
        config,
        ctx,
        [("levimc-repo", repo)],
        [
            {"name": "cpr", "version": "1.11.1"},
            # the recipe spells it with the "v", the project file does not
            {"name": "libhat", "version": "0.4.0"},
            # a dependency with no version in the project file stays unpinned
            {"name": "trampoline", "version": ""},
        ],
    )
    assert lines == [
        'add_requires("cpr 1.11.1")',
        'add_requires("libhat v0.4.0")',
        'add_requires("trampoline")',
    ]
    # a project with no such dependencies adds nothing
    assert build.extra_requires(config, ctx, [("levimc-repo", repo)], []) == []


class _StubPlugin:
    """The smallest plugin the planning and version logic ever asks anything."""

    def __init__(self, name, package, deps=(), versions=(), entries=(), specs=()):
        self.name = name
        self._package = package
        self._deps = list(deps)
        self._versions = list(versions)
        self._entries = list(entries) or [{"id": "default", "configs": {}}]
        self._specs = list(specs)

    def call(self, function, *args, **kwargs):
        if function == "metadata":
            return {"id": self.name, "package": self._package}
        if function == "extra_deps":
            return list(self._deps)
        if function == "dependency_specs":
            return list(self._specs)
        if function == "list_versions":
            return [{"version": item, "commit": "ab" * 20} for item in self._versions]
        if function == "recipe_cutoff":
            return None
        if function == "recipe_dir":
            return None
        return None

    def entries(self, ctx, version, commit, settings=None):
        return list(self._entries)


def test_an_old_header_only_recipe_is_told_it_is_one(tmp_path: Path):
    """Without the declaration xmake treats it as a library and wants a link.

    The recipe of the 0.12 era has no ``set_kind("library", {headeronly = true})``,
    so after installing a header-only package xmake looks for a link to write into
    the ``.pc`` file and refuses it ("package(nlohmann_json): links not found!"),
    which is how levilamina 0.12.2/0.12.3 died. Upstream added the declaration
    later, and the repair adds what it added -- where it is missing.
    """
    recipe = tmp_path / "xmake-repo@test" / "packages" / "n" / "nlohmann_json"
    recipe.mkdir(parents=True)
    (recipe / "xmake.lua").write_text(
        'package("nlohmann_json")\n'
        '    set_homepage("https://nlohmann.github.io/json/")\n'
        "    on_install(function (package) end)\n",
        encoding="utf-8",
    )
    ll = next(item for item in plugins.load_all(util.load_config()) if item.name == "ll")
    notes = ll.module._declare_headeronly(tmp_path / "xmake-repo@test")
    text = (recipe / "xmake.lua").read_text(encoding="utf-8")
    assert notes and "declared header-only" in notes[0], notes
    assert text.startswith('package("nlohmann_json")\n    set_kind("library", { headeronly = true })\n')
    # idempotent, and a package the snapshot does not carry is skipped
    assert ll.module._declare_headeronly(tmp_path / "xmake-repo@test") == []
    assert ll.module._declare_headeronly(tmp_path / "xmake-repo@other") == []


def test_an_old_libcurl_does_not_build_its_documentation(tmp_path: Path):
    """The man pages are not what we publish, and these images cannot make them.

    The libcurl of the 1.0 era fails inside ``docs/libcurl/curl-man.vcxproj`` with
    MSB8066 ("custom build ... exited with code 1") and takes the whole era build of
    ``levilamina 1.0.0-rc.3`` with it, while the library itself compiles fine. Its
    recipe passes neither flag, so the repair adds the one CMake offers for docs.
    """
    recipe = tmp_path / "xmake-repo@test" / "packages" / "l" / "libcurl"
    recipe.mkdir(parents=True)
    (recipe / "xmake.lua").write_text(
        'package("libcurl")\n'
        "    on_install(function (package)\n"
        '        local configs = {"-DBUILD_TESTING=OFF", "-DENABLE_MANUAL=OFF", "-DENABLE_CURL_MANUAL=OFF"}\n'
        "    end)\n",
        encoding="utf-8",
    )
    ll = next(item for item in plugins.load_all(util.load_config()) if item.name == "ll")
    notes = ll.module._disable_libcurl_docs(tmp_path / "xmake-repo@test")
    text = (recipe / "xmake.lua").read_text(encoding="utf-8")
    assert notes and "docs disabled" in notes[0], notes
    assert '"-DENABLE_CURL_MANUAL=OFF", "-DBUILD_LIBCURL_DOCS=OFF"}' in text
    # idempotent, and a recipe that does not look like this one is left alone
    assert ll.module._disable_libcurl_docs(tmp_path / "xmake-repo@test") == []
    other = tmp_path / "xmake-repo@other" / "packages" / "l" / "libcurl"
    other.mkdir(parents=True)
    (other / "xmake.lua").write_text('package("libcurl")\n', encoding="utf-8")
    assert ll.module._disable_libcurl_docs(tmp_path / "xmake-repo@other") == []
    assert (other / "xmake.lua").read_text(encoding="utf-8") == 'package("libcurl")\n'


def test_the_plugin_being_built_repairs_its_own_snapshot(tmp_path: Path, monkeypatch):
    """The one plugin that must run is the one whose project this is.

    Its ``repair_recipes`` is what prepares the project's own recipe -- for the
    upstream plugin that means injecting the block that pins the SDK versions the
    project leaves open. A "already seen" set seeded with its name made the loop
    skip it, so such a project was built against today's newest SDK (compiled a
    second time inside the dependent's job, then packed and uploaded again), while
    every *other* plugin's repairs still ran and hid the bug.
    """
    calls: list[str] = []

    class Fake:
        def __init__(self, name):
            self.name = name

        def repair_recipes(self, config, source_name, snapshot):
            if source_name == "some-repo":
                calls.append(self.name)
                return [f"{self.name}: repaired"]
            return []

    building = Fake("the-one-being-built")
    monkeypatch.setattr(plugins, "load_all", lambda config: [building, Fake("another")])
    notes = recipefix.plugin_repairs({}, building, "some-repo", tmp_path)
    assert calls == ["the-one-being-built", "another"], calls
    assert notes == ["the-one-being-built: repaired", "another: repaired"], notes
    # a plugin the config does not list (a probe) still gets its turn, once
    monkeypatch.setattr(plugins, "load_all", lambda config: [Fake("another")])
    calls.clear()
    notes = recipefix.plugin_repairs({}, Fake("probe-only"), "some-repo", tmp_path)
    assert calls == ["probe-only", "another"], calls


def test_a_snapshot_that_predates_the_release_declares_the_version(tmp_path: Path):
    """A recipe newer than the version cannot fetch it, and says something else.

    The snapshot is taken just before the release, so the version it is built for
    may not be in the recipe at all; xmake then fetches a git package by *tag*,
    and these projects tag with a "v" ("v0.12.1" against the recipe's "0.12.1"),
    which fails with "fatal: Remote branch 0.12.1 not found in upstream origin".
    """
    snapshot = tmp_path / "levimc-repo@0.12.1"
    recipe = snapshot / "packages" / "m" / "more-dimensions"
    recipe.mkdir(parents=True)
    (recipe / "xmake.lua").write_text(
        'package("more-dimensions")\n'
        '    add_urls("https://github.com/LiteLDev/MoreDimensions.git")\n'
        '    add_versions("0.12.0", "213818c9653fd9db63a279e9afbf893aed68efcc")\n'
        "    on_install(function (package) end)\n",
        encoding="utf-8",
    )
    prepared = {"levimc-repo": (snapshot, "b3d8952f8120")}
    notes = recipefix.declare_version(prepared, "more-dimensions", "0.12.1", "25db4b93" * 5)
    assert notes and "0.12.1" in notes[0], notes
    text = (recipe / "xmake.lua").read_text(encoding="utf-8")
    assert 'add_versions("0.12.1", "' + "25db4b93" * 5 + '")' in text
    # the version that was already there is untouched, and the call is a no-op
    assert 'add_versions("0.12.0", "213818c9653fd9db63a279e9afbf893aed68efcc")' in text
    assert recipefix.declare_version(prepared, "more-dimensions", "0.12.1", "x") == []
    assert text == (recipe / "xmake.lua").read_text(encoding="utf-8")

    # a recipe that lists its versions in a file gets the line there
    listing_dir = tmp_path / "levimc-repo@1.9.4" / "packages" / "l" / "levilamina"
    (listing_dir / "versions").mkdir(parents=True)
    (listing_dir / "xmake.lua").write_text(
        'package("levilamina")\n    add_versionfiles("versions/versions.txt")\n',
        encoding="utf-8",
    )
    (listing_dir / "versions" / "versions.txt").write_text(
        "1.9.0 abcdef\n", encoding="utf-8"
    )
    files = {"levimc-repo": (tmp_path / "levimc-repo@1.9.4", "deadbeef")}
    assert recipefix.declare_version(files, "levilamina", "1.9.4", "0123456789")
    assert (listing_dir / "versions" / "versions.txt").read_text(
        encoding="utf-8"
    ) == "1.9.0 abcdef\n1.9.4 0123456789\n"
    assert recipefix.declare_version(files, "levilamina", "1.9.4", "0123456789") == []

    # a package the snapshot does not carry is skipped
    assert recipefix.declare_version(files, "not-here", "1.0.0", "abc") == []


def test_the_projects_own_pin_forces_the_sdk_version():
    """A recipe's range is not what a project of its own age was built against.

    ``more-dimensions`` 0.12.1 pins ``levilamina 1.7.6`` in its own file while its
    recipe allows ``>=1.7.6 <1.8.0``, and xmake reads the prerelease `1.8.0-rc.2`
    as inside that -- an SDK whose generated headers do not even compile. The
    project's own version is forced on the resolve tree instead (``add_requireconfs``
    in ``build_once``), and a dependency the project leaves open gets the era's
    answer, the same one the project's own build is pinned to.
    """
    ctx = plugins.Context(dict(util.load_config()))
    pinned = _StubPlugin("moredimensions", "more-dimensions",
                         deps=[{"name": "levilamina", "version": "1.7.6"}])
    # the recipe declares that name too, so it is not asked for directly
    forced = build.dependency_overrides(
        ctx, pinned, "0.12.1", "25db4b93", [], {}
    )
    assert forced == {"levilamina": "1.7.6"}

    # a dependency the project names without a version takes the era's answer
    open_dep = _StubPlugin("bsci", "bsci", deps=[{"name": "levilamina", "version": ""}])
    assert build.dependency_overrides(
        ctx, open_dep, "0.4.0", "4c88ff64", [], {"levilamina": "26.20.0"}
    ) == {"levilamina": "26.20.0"}

    # a range of its own is a deliberate statement, and is left to the resolvers
    ranged = _StubPlugin("bsci", "bsci", deps=[{"name": "levilamina", "version": "26.20.*"}])
    assert build.dependency_overrides(ctx, ranged, "0.4.0", "4c88ff64", [], {}) == {}
    assert plan.own_pins(ranged, ctx, "0.4.0", "4c88ff64") == {}


def test_an_entry_can_say_which_versions_have_that_dimension(tmp_path: Path):
    """Not every version of a project has every build dimension.

    ``bsci`` only grew its ``target_type`` option at 0.3.0: asking 0.1.6 for a
    client artifact resolves a client LeviLamina its era never published, and the
    project's own file does not even mention the option. The bounds are declared
    with the project (``config.json``), not invented by the framework.
    """
    import importlib.util

    path = ROOT / "plugins" / "upstream.py"
    spec = importlib.util.spec_from_file_location("upstream_plugin_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    plugin = module.create(
        {
            "id": "bsci",
            "recipe_package": "bsci",
            "entries": [
                {"id": "server", "configs": {"target_type": "server"}},
                {"id": "client", "from": "0.3.0", "configs": {"target_type": "client"}},
                {"id": "legacy", "until": "0.1.6", "configs": {}},
            ],
        }
    )
    ctx = plugins.Context(dict(util.load_config()))
    ids = [item["id"] for item in plugin.entries(ctx, "0.1.6", "abc")]
    assert ids == ["server", "legacy"], ids
    ids = [item["id"] for item in plugin.entries(ctx, "0.3.0", "abc")]
    assert ids == ["server", "client"], ids
    # the bounds are not part of the entry a job is built from
    assert all("from" not in item and "until" not in item
               for item in plugin.entries(ctx, "0.3.0", "abc"))
    # a version nothing can compare (a branch) keeps every entry rather than
    # silently publishing nothing
    assert len(plugin.entries(ctx, "main", "abc")) == 3


def test_the_planned_sdk_is_the_version_the_project_pins():
    """Planning has to make the same call as the build, or it queues a stranger.

    The recipe's range resolves to the newest SDK there is; the project's own file
    pins the one its sources were written against. A round that planned the range
    would build and publish an SDK the dependent never installs, and leave the one
    it does need unbuilt -- and make the dependent wait for the wrong artifact.
    """
    config = dict(util.load_config())
    ctx = plugins.Context(config)
    project = _StubPlugin(
        "moredimensions",
        "more-dimensions",
        deps=[{"name": "levilamina", "version": "1.7.6"}],
        specs=["levilamina >=1.7.6 <1.8.0", "levilamina 1.7.6"],
    )
    owner = _StubPlugin("ll", "levilamina", versions=["1.8.0-rc.2", "1.7.7", "1.7.6"])
    jobs = [plan.job(config, ctx, project, "0.12.1", "25db4b93", {"id": "default", "configs": {}})]
    first, dependencies, waiting, notes = plan.split(
        config, ctx, [project, owner], jobs, lambda package, version: False
    )
    assert [item["version"] for item in dependencies] == ["1.7.6"], (dependencies, notes)
    assert waiting and waiting[0]["waits_for"] == ["levilamina@1.7.6"], waiting
    assert first == []


def test_copy_only_dependencies_are_not_cached(tmp_path: Path):
    """Nothing compiled means nothing to precompile.

    A header-only library or a data package is installed by downloading and
    copying files: an archive would save a consumer nothing, and that is also why
    those packages used to show up with no address for the other toolchain (their
    hash does not carry one). The project's own package is always published --
    that is what a consumer installs.
    """
    dependency = tmp_path / "entt" / "v3.13.1" / "3c37e4785cbf4e5da3ed26348f465135"
    (dependency / "include" / "entt").mkdir(parents=True)
    (dependency / "include" / "entt" / "entt.hpp").write_text("//\n", encoding="utf-8")
    (dependency / "manifest.txt").write_text("", encoding="utf-8")
    assert build.has_build_output(dependency) is False
    assert build.should_publish("entt", "levilamina", dependency) is False

    # a dependency that compiled something keeps its artifact
    (dependency / "lib").mkdir()
    (dependency / "lib" / "entt.lib").write_text("", encoding="utf-8")
    assert build.should_publish("entt", "levilamina", dependency) is True

    # the project's own package is published whatever it installs: it is the SDK
    own = tmp_path / "levilamina" / "0.8.3" / "1744349df0da4502a23929f25a067945"
    (own / "include").mkdir(parents=True)
    (own / "include" / "LeviLamina.h").write_text("", encoding="utf-8")
    assert build.should_publish("levilamina", "levilamina", own) is True


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
    # the edit is guarded by existence: 1.4.4 has no such file, and inside a
    # package script a failing io.readfile *raises*, which would fail its install
    assert 'local file = "src/mc/common/wrapper/optional_ref.h"' in text
    assert "if os.isfile(file) then" in text
    assert "local data = io.readfile(file)" in text
    assert "if data and data:find([==[" in text
    assert "io.replace(file, [==[" in text
    assert "std::_Construct_from_invoke_result_tag{}," in text
    assert "std::invoke(std::forward<Fn>(fn), static_cast<T&>(*mPtr))" in text
    assert "{plain = true})" in text
    # 0.13.x reached into the STL for the other one too: its tuple-like concept
    # named ``std::_Is_std_array_v``/``std::_Is_subrange_v``, which newer STLs do
    # not expose -- and because it is a *header*, every consumer that compiled
    # against the SDK failed with C3878/C2039 inside it:
    assert 'local file = "src/ll/api/base/Concepts.h"' in text
    assert "::std::_Is_std_array_v<T>" in text
    # ... and the repair spells both traits out locally, keeping the expression
    # that used them (so tuple, pair, array and subrange all stay tuple-like)
    assert "struct is_std_array<::std::array<T, N>> : ::std::true_type {};" in text
    assert "struct is_subrange<::std::ranges::subrange<I, S, K>> : ::std::true_type {};" in text
    assert "|| is_specialization_of_v<T, ::std::pair> || is_std_array_v<T> || is_subrange_v<T>;" in text
    assert "concept TupleLike = tuple_like_impl<std::remove_cvref_t<T>>" in text
    # the names it now mentions bring their own headers
    assert '"src/ll/api/base/Concepts.h", "<array>"' in text or "#include <array>" in text
    assert "#include <ranges>" in text
    assert "std::tuple_size<std::remove_cvref_t<T>>::value;" in text


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


def test_a_declared_recipe_edit_repairs_what_xmake_cannot_import(tmp_path: Path):
    """`more-dimensions` asks to import `package.tools.*make`, which never resolves.

    A module name is a *path* in xmake's importer (the dots become separators and
    that file is looked up), so a wildcard is not a pattern there in any version:
    the install dies with "cannot import module: ... not found!" before a single
    file is compiled. The repair is project knowledge and therefore declared in
    the project's own settings; it only ever touches our snapshot.
    """
    config = util.load_config()
    plugin = next(
        item for item in plugins.load_all(config) if item.name == "moredimensions"
    )
    snapshot = tmp_path / "levimc-repo@test"
    recipe = snapshot / "packages" / "m" / "more-dimensions" / "xmake.lua"
    recipe.parent.mkdir(parents=True)
    recipe.write_text(
        'package("more-dimensions")\n'
        "    on_install(function (package)\n"
        '        import("package.tools.*make").install(package)\n'
        "    end)\n",
        encoding="utf-8",
    )

    notes = plugin.module.repair_recipes(config, "levimc-repo", snapshot)
    # the recipe gets the edit, and the block that pins what the project leaves open
    assert len(notes) == 2, notes
    assert "pins the SDK versions it leaves open" in notes[0], notes
    assert "package.tools.xmake" in util.read_text(recipe)
    assert "BUILDCACHE_PINS" in util.read_text(recipe)
    # idempotent: the pin block is not injected twice
    assert plugin.module.repair_recipes(config, "levimc-repo", snapshot) == []
    assert util.read_text(recipe).count("BUILDCACHE_PINS") == 1
    assert 'import("package.tools.xmake")' in util.read_text(recipe)
    # a source that does not carry this project's recipe is left alone, and the
    # repair is idempotent: a second pass has nothing left to replace
    assert plugin.module.repair_recipes(config, "xmake-repo", snapshot) == []
    assert plugin.module.repair_recipes(config, "levimc-repo", snapshot) == []


def test_upstream_pins_are_rewritten_while_the_project_builds(tmp_path: Path):
    """The era's *project* file pins its data package, so both sides must agree.

    LeviLamina 0.8.3's own xmake.lua says ``add_requires("bdslibrary 1.20.61.01")``,
    a four-component version the repaired recipe cannot offer any more (it was
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
        'io.replace(file, "bdslibrary 1.20.61.01", '
        '"bdslibrary 1.20.61-r01", {plain = true})' in injected
    )
    # the same guard as the era repairs: the project file has to be there
    assert injected.count("if os.isfile(file) then") == 2
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


def test_an_owned_packages_dependency_artifact_takes_the_entry_it_matches(monkeypatch, tmp_path: Path):
    """A plugin's own package has entries; a dependency record must not add "default".

    LeviLamina is both a project of its own plugin and a dependency of every LL
    plugin, so a plugin's round can publish an artifact of it first. Those carry
    the configs they were built with (`target_type=client`), which is exactly one
    of the owner's entries -- and recording it as `default` would put an entry the
    project does not have next to its own, in the ledger and in every release body
    that shows the column. Configs no entry matches are left to the owner's build.
    """
    ll = next(item for item in plugins.load_all(util.load_config()) if item.name == "ll")
    monkeypatch.setattr(finalize.plugins, "load_all", lambda config: [ll])
    monkeypatch.setattr(
        ll.module, "list_versions", lambda ctx: [{"version": "26.51.6", "commit": "aaa"}]
    )
    monkeypatch.setattr(
        ll.module,
        "upstream_xmake_lua",
        lambda ctx, commit: 'option("target_type")\nset_values("server", "client")\n',
    )
    ctx = plugins.Context(dict(util.load_config(), _index_dir=str(tmp_path / "index")))

    assert finalize._owning_entry(ctx, "levilamina", "26.51.6", {"target_type": "client"}) == "client"
    assert finalize._owning_entry(ctx, "levilamina", "26.51.6", {"target_type": "server"}) == "server"
    # configs no entry of the owner matches are left to the owner's own build
    assert finalize._owning_entry(ctx, "levilamina", "26.51.6", {"target_type": "arm"}) == ""
    # and a package the owner does not publish says nothing at all
    assert finalize._owning_entry(ctx, "fmt", "11.2.0", {}) == ""


def test_default_is_not_an_entry_a_reader_ever_sees():
    """"default" is the framework's name for "no build dimension".

    MoreDimensions is the same API for a client and a server, so it is built once
    and has nothing to say in an `entry` column -- which then drops itself, as a
    column empty in every row does.
    """
    assert reports.artifact_row({}, version="0.14.1", entry_id="default", own=True)["entry"] == ""
    # a project that does have dimensions keeps them
    assert reports.artifact_row({}, entry_id="server", own=True)["entry"] == "server"
    # a dependency is never given one
    assert reports.artifact_row({}, entry_id="server", own=False)["entry"] == ""


def test_dependencies_are_recorded_once_without_an_entry(tmp_path: Path):
    """Only the project's own package has an entry dimension."""
    config = dict(util.load_config())
    config["_index_dir"] = str(tmp_path / "index")
    ctx = plugins.Context(config)

    def artifact(package: str, buildhash: str, entry: str) -> dict:
        return {
            "package": package,
            "version": "11.2.0" if package == "fmt" else "26.51.6",
            "buildhash": buildhash,
            "key": f"windows-x64-vc-{buildhash}",
            "toolset": "14.44.35207",
            "url": f"https://example.invalid/{package}-{buildhash}.7z",
            "sha256": "ab" * 32,
            "aliases": [],
        }

    shared = artifact("fmt", "ee1902dd", "server")
    results = [
        {
            "package": "levilamina",
            "version": "26.51.6",
            "entry": entry,
            "revision": 1,
            "toolchain": "msvc",
            "upstream_toolchain": "clang-cl",
            "alias_profiles": ["msvc", "clang-cl"],
            "artifacts": [artifact("levilamina", f"ll{entry}", entry), shared],
        }
        for entry in ("server", "client")
    ]
    finalize.write_ledger(config, ctx, results)

    owned = index_store.entries(
        index_store.version_meta(ctx.index_root, "levilamina", "26.51.6")
    )
    assert set(owned) == {"server", "client"}, owned
    assert owned["server"]["compiled_with"] == "clang-cl"
    assert owned["server"]["revision"] == 1
    # the project's record holds its own artifact, not the whole closure
    assert [item["package"] for item in owned["server"]["artifacts"]] == ["levilamina"]

    dependency = index_store.entries(
        index_store.version_meta(ctx.index_root, "fmt", "11.2.0")
    )
    assert set(dependency) == {"default"}, dependency
    assert len(dependency["default"]["artifacts"]) == 1
    # and it says which project pulled it in -- with *that* project's version,
    # not the dependency's own (``levilamina@41aab920`` read as if LeviLamina
    # were the one at 41aab920, the version of the entt it needs)
    assert dependency["default"]["version"] == "11.2.0"
    assert dependency["default"]["produced_for"] == "levilamina@26.51.6"
    # the dependency keeps its own name, which is what a consumer resolves -- our
    # repository carries the manifest only, so nothing is renamed on the way out
    assert dependency["default"]["artifacts"][0]["key"] == shared["key"]
    assert "default" not in index_store.entries(
        index_store.version_meta(ctx.index_root, "levilamina", "26.51.6")
    )


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
    The era is expressed by the *image* alone, for every plugin that builds with
    the xmake of its own time.
    """
    from lib import semver, xmake_env

    config = dict(util.load_config())
    ctx = plugins.Context(config)
    ll = next(item for item in plugins.load_all(config) if item.name == "ll")
    for version, commit in (("26.51.6", "a" * 40), ("1.4.4", "b" * 40)):
        plan = ll.module.toolchain_plan(ctx, version, commit)
        assert plan["vs_toolset"] is None, plan
    # an era xmake that predates a Visual Studio generation gets the image that
    # has one it knows; a current era keeps the current image
    assert xmake_env.era_image("3.1.1") == ("windows-latest", "win25")
    assert xmake_env.era_image("3.0.2") == ("windows-2022", "win22")
    assert xmake_env.era_image("2.8.6") == ("windows-2022", "win22")
    # the table is ordered newest floor first, so the first match is the answer
    floors = [semver.key(floor) for floor, _runner, _imageos in xmake_env.ERA_IMAGES]
    assert floors == sorted(floors, reverse=True)


def test_an_upstream_project_gets_the_image_of_its_era(tmp_path: Path):
    """A project built with an old xmake cannot be driven on today's image.

    ``bsci`` 0.1.7 died with ``vs not found!`` on every attempt: its era xmake is
    3.0.2, which does not know Visual Studio 18, so the CMake-based dependency
    (entt) could not configure at all -- a failure that names no version.
    """
    import importlib.util

    path = ROOT / "plugins" / "upstream.py"
    spec = importlib.util.spec_from_file_location("upstream_plugin_test2", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    plugin = module.create(
        {
            "id": "bsci",
            "recipe_package": "bsci",
            "upstream_repo": "OEOTYAN/BedrockServerClientInterface",
        }
    )
    ctx = plugins.Context(dict(util.load_config()))
    plugin.commit_date = lambda ctx, commit: "2025-09-14T00:00:00Z"
    plan = plugin.toolchain_plan(ctx, "0.1.7", "c" * 40)
    assert plan["image"] == "windows-2022", plan
    assert plan["imageos"] == "win22"
    assert plan["era_xmake"] and plan["era_xmake"].startswith("3."), plan


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
