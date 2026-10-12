"""Generic plugin for upstream projects that ship an xmake recipe.

The plugin is configured entirely from ``config.json`` so adding a project never
means writing framework code:

```json
{
  "name": "moredimensions",
  "module": "plugins/upstream.py",
  "settings": {
    "upstream": "https://github.com/LiteLDev/MoreDimensions.git",
    "upstream_repo": "LiteLDev/MoreDimensions",
    "recipe_source": "levimc-repo",
    "recipe_package": "more-dimensions",
    "entries": [{"id": "default", "configs": {}}]
  }
}
```

Versions come from the recipe's ``add_versions`` list (so the cache can never
drift from what the recipe can actually resolve), and the cutoff used to pin
unpinned clones is the release date of the upstream commit for that version.
"""

from __future__ import annotations

import re
from pathlib import Path

from lib import recipefix, repos, semver, util

_ADD_VERSIONS = re.compile(r'add_versions\(\s*"([^"]+)"\s*,\s*"([0-9a-fA-F]{7,40})"')
_ADD_REQUIRES = re.compile(r'add_requires\(\s*"([^"]+)"')
_ON_INSTALL = re.compile(r"^\s*on_install\(function", re.M)

def create(settings: dict):
    """Build the plugin instance for the settings from ``config.json``."""
    return _UpstreamPlugin(settings)


class _UpstreamPlugin:
    def __init__(self, settings: dict):
        self.settings = settings or {}

    # ------------------------------------------------------------------ metadata
    def metadata(self) -> dict:
        return {
            "id": self.settings.get("id", self.settings["recipe_package"]),
            "display": self.settings.get("display", self.settings["recipe_package"]),
            "package": self.settings["recipe_package"],
            "upstream": self.settings.get("upstream_repo", ""),
            "extra_packages": self.settings.get("extra_packages", []),
        }

    def _recipe_dir(self, ctx) -> Path:
        source = ctx.recipe_source(self.settings["recipe_source"])
        if source is None:
            raise RuntimeError(f"recipe source {self.settings['recipe_source']} is not configured")
        return source / "packages" / self.settings["recipe_package"][0] / self.settings["recipe_package"]

    def _upstream_dir(self, ctx):
        return ctx.mirror(self.metadata()["id"], self.settings["upstream"])

    # ------------------------------------------------------------------- versions
    def list_versions(self, ctx) -> list[dict]:
        text = util.read_text(self._recipe_dir(ctx) / "xmake.lua")
        versions: list[dict] = []
        seen = set()
        for match in _ADD_VERSIONS.finditer(text):
            version = match.group(1).split(":", 1)[-1]
            if version in seen:
                continue
            seen.add(version)
            versions.append({"version": version, "commit": match.group(2)})
        listing = self._recipe_dir(ctx) / "versions.txt"
        if listing.is_file():
            for line in util.read_text(listing).splitlines():
                parts = line.split()
                if len(parts) >= 2 and parts[0] not in seen:
                    seen.add(parts[0])
                    versions.append({"version": parts[0], "commit": parts[1]})
        versions.sort(key=lambda item: semver.key(item["version"]), reverse=True)
        return self._kept(versions)

    def _kept(self, versions: list[dict]) -> list[dict]:
        """Drop the versions this project does not want built (see ``skipBelow``).

        The oldest releases of a project are sometimes impossible to build at all --
        their own sources do not compile with a current compiler, whatever SDK they
        are linked against. Such a version is not a backlog item: queued, it takes a
        job every round and fails that job, which is how a round ends up red for
        something that cannot be fixed. Which of its own versions a project gives up
        on is that project's decision, so it is declared with the rest of it.
        """
        below = self.settings.get("skipBelow")
        named = {str(item) for item in (self.settings.get("skipVersions") or [])}
        if not below and not named:
            return versions
        kept, skipped = [], []
        for item in versions:
            version = item["version"]
            if version in named or (below and semver.lt(version, below)):
                skipped.append(version)
                continue
            kept.append(item)
        if skipped:
            shown = ", ".join(skipped[:3]) + (", ..." if len(skipped) > 3 else "")
            util.log(
                f"{self.metadata()['package']}: {len(skipped)} version(s) this project "
                f"cannot build are not queued ({shown})"
            )
        return kept

    def should_skip(self, ctx, version: str, commit: str) -> tuple[bool, str]:
        return False, ""

    # ------------------------------------------------------------------ behaviour
    def toolchain(self, ctx, version: str, commit: str) -> str:
        return self.settings.get("toolchain", "msvc")

    def native_toolchain(self, ctx, version: str, commit: str) -> str:
        return self.settings.get("native_toolchain", self.toolchain(ctx, version, commit))

    def toolchain_aliases(self, ctx, version: str, commit: str) -> list[dict]:
        """Extra consumer toolchains to publish the same artifact for.

        Opt-in per project (``"toolchainAliases": [{"id": "clang-cl",
        "toolchain": "clang-cl"}]``): whether the same archive can serve another
        toolchain is a property of the project's artifacts, not of the framework.

        Only addresses that make sense for how this version is built survive: the
        toolchain that compiled its sources, and the one the package is resolved
        with. A project still compiled by msvc predates clang-cl support, and no
        clang-cl consumer exists for it, so that address is dropped.
        """
        found = []
        for item in self.settings.get("toolchainAliases") or []:
            if isinstance(item, str):
                found.append({"id": item, "toolchain": item})
            elif item.get("toolchain"):
                found.append({"id": item.get("id") or item["toolchain"], "toolchain": item["toolchain"]})
        allowed = {self.native_toolchain(ctx, version, commit), self.toolchain(ctx, version, commit)}
        return [item for item in found if item["toolchain"] in allowed]

    def xmake_version(self, ctx, version: str, commit: str) -> str:
        """The xmake the project was released with -- its own, never a newer one.

        Deliberately not clamped to ``xmake.minArtifactsVersion``, exactly like
        LeviLamina's own plugin (see its ``xmake_version``): a project's build is
        driven by the tooling of its time. The clamp used to raise an era that
        predates the floor to the *newest* xmake there is, so `bsci` 0.1.6 -- whose
        era is 2.9.8 -- was built by 3.1.1 after all: a new xmake driving the era's
        own link rule and its era's `prelink`, which report a format mismatch deep
        inside the tool ("Cannot create bedrock_runtime_api.lib, no such file or
        directory") instead of anything a recipe could be blamed for.

        The image the job runs on follows from the same value (see toolchain_plan),
        so both have to come from one place.
        """
        return self._era_xmake(ctx, commit)

    def _era_xmake(self, ctx, commit: str) -> str:
        from lib import xmake_env

        era = xmake_env.xmake_version_for_date(
            ctx.config, self.commit_date(ctx, commit), floor="0.0.0"
        )
        # The floor of the era's own release line: 2.9.0-2.9.3 ask CMake for a
        # Visual Studio 2026 toolset on an image that has none, which kills every
        # CMake dependency of such an era with MSB8020 -- that is how `bsci` 0.1.4
        # failed while its neighbours built (see lib/xmake_env.LINE_FLOORS).
        return xmake_env.line_floor(era)

    def toolchain_plan(self, ctx, version: str, commit: str) -> dict:
        """The image the era's xmake needs to run on.

        A project built with the xmake of its own time needs the image of that
        generation too, and for the same reason LeviLamina's own plugin does: an
        xmake that predates a Visual Studio cannot drive it, and a CMake-based
        *dependency* then dies with ``vs not found!`` -- which names no version,
        no package and no recipe. That is how ``bsci`` 0.1.7 failed on every
        attempt: xmake 3.0.2 asking the windows-latest image of 2026 for MSVC
        14.51, a Visual Studio generation it has never heard of.

        Nothing else is pinned here: the toolset stays whatever the image has (the
        era's own generation, which is the point), and an era recent enough for the
        current image keeps it.
        """
        from lib import xmake_env

        era = self._era_xmake(ctx, commit)
        runner, imageos = xmake_env.era_image(era)
        return {"era_xmake": era, "image": runner, "imageos": imageos}

    def recipe_cutoff(self, ctx, version: str, commit: str) -> str:
        return self.commit_date(ctx, commit)

    def git_cutoff(self, ctx, version: str, commit: str) -> str:
        return self.commit_date(ctx, commit)

    def commit_date(self, ctx, commit: str) -> str:
        directory = self._upstream_dir(ctx)
        text = util.run_out(
            ["git", "-C", str(directory), "show", "-s", "--format=%cI", commit], check=False
        )
        if not text:
            # some recipes track a repository that no longer contains the commit
            return util.run_out(
                ["git", "-C", str(directory), "log", "-1", "--format=%cI"], check=False
            ) or "1970-01-01T00:00:00Z"
        return text

    def configs(self, ctx, version: str, commit: str) -> dict:
        entries = self.entries(ctx, version, commit)
        return entries[0]["configs"] if entries else {}

    def entries(self, ctx, version: str, commit: str) -> list[dict]:
        """Entries declared by the settings (``id`` + package ``configs``).

        An entry may carry ``from`` / ``until`` bounds (inclusive, semver) for the
        versions it exists in. A build dimension is not automatically a property of
        every version of a project: ``bsci`` only grew its ``target_type`` option at
        0.3.0, so asking 0.1.6 for a client artifact resolves a client LeviLamina
        its era never published -- a failure about a dependency the project's own
        file does not even name (it has no such option to name it with), while the
        server artifact of that version is fine. Which versions of a project have
        which dimension is that project's knowledge, so it is declared with it.
        """
        declared = self.settings.get("entries")
        if not declared:
            return [{"id": "default", "configs": self.settings.get("configs") or {}}]
        found = []
        for item in declared:
            entry = dict(item) if isinstance(item, dict) else {"id": item}
            low = str(entry.pop("from", "") or "")
            high = str(entry.pop("until", "") or "")
            if not _in_version_range(version, low, high):
                continue
            entry.setdefault("configs", {})
            found.append(entry)
        return found

    def extra_deps(self, ctx, version: str, commit: str) -> list[dict]:
        """Dependencies only named by the upstream project's own xmake.lua."""
        text = repos.file_at(self._upstream_dir(ctx), commit, "xmake.lua")
        if not text:
            return []
        wanted = set(self.metadata()["extra_packages"])
        found: dict[str, str] = {}
        for match in _ADD_REQUIRES.finditer(text):
            spec = match.group(1).split(",", 1)[0].strip()
            parts = spec.split()
            if not parts:
                continue
            name = parts[0].split("[", 1)[0].strip()
            if name in wanted:
                found[name] = parts[1] if len(parts) > 1 else ""
        return [{"name": name, "version": version} for name, version in sorted(found.items())]

    def recipe_dir(self, ctx, version: str, commit: str):
        """Where this project's recipe lives, so a round can read its dependencies."""
        try:
            return self._recipe_dir(ctx)
        except RuntimeError:
            return None

    def repair_recipes(self, config, source_name: str, snapshot) -> list[str]:
        """Apply the ``recipeEdits`` this project declares.

        An upstream recipe is written against the project's own build, and one of
        them can simply not work: ``more-dimensions`` asks xmake to
        ``import("package.tools.*make")``, and a module name *is* a path in
        xmake's importer -- it maps the dots to separators and looks for that file,
        so no version resolves a wildcard there. The install dies with "cannot
        import module: ... not found!" before a single file is compiled, which is
        why the package never had an artifact.

        The edit is project knowledge, so it is declared in ``config.json`` under
        this plugin and applied to *our* snapshot only. None of it reaches a
        consumer: the published repository carries manifests and no recipes, and a
        package's build hash comes from the source commit, not from recipe text.
        """
        if source_name != self.settings.get("recipe_source"):
            return []
        package = self.settings["recipe_package"]
        recipe = (
            Path(snapshot) / "packages" / package[0].lower() / package / "xmake.lua"
        )
        if not recipe.is_file():
            return []
        notes = recipefix.pin_project_requires(recipe)
        for edit in self.settings.get("recipeEdits") or []:
            find = str(edit.get("find") or "")
            replace = str(edit.get("replace") or "")
            if not find:
                continue
            text = util.read_text(recipe)
            if find not in text:
                # Either the recipe was fixed upstream, or the declaration no
                # longer matches: both are worth hearing about.
                util.warn(
                    f"{package}: declared recipe edit does not apply ({find!r})"
                )
                continue
            util.write_text(recipe, text.replace(find, replace))
            notes.append(f"{package}: repaired recipe ({find!r} -> {replace!r})")
        return notes


def _in_version_range(version: str, low: str, high: str) -> bool:
    """Is ``version`` within ``[low, high]`` (both optional, both inclusive)?

    A version the comparison cannot read (a branch name, a date-like tag) keeps
    every entry: dropping one because of a parsing quirk would silently stop
    publishing something that worked.
    """
    if not low and not high:
        return True
    try:
        if low and semver.lt(version, low):
            return False
        if high and semver.gt(version, high):
            return False
    except ValueError:
        return True
    return True


    def asset_name(self, ctx, package: str, version: str, toolset, build_hash: str, container: str):
        """Optional ``assetName`` template from the settings, e.g. ``"{version}-{hash}.7z"``."""
        template = self.settings.get("assetName")
        if not template:
            return None
        suffix = "tar.zst" if container == "tar.zst" else "7z"
        return str(template).format(
            package=package,
            version=version,
            toolset=toolset or "generic",
            hash=build_hash,
            buildhash=build_hash,
            ext=suffix,
        )

    def report_skipped(self, ctx, known: list[dict]) -> list[str]:
        return []
