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
    "variants": [{"name": "default", "configs": {}}]
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

from lib import repos, semver, util

_ADD_VERSIONS = re.compile(r'add_versions\(\s*"([^"]+)"\s*,\s*"([0-9a-fA-F]{7,40})"')
_ADD_REQUIRES = re.compile(r'add_requires\(\s*"([^"]+)"')


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
        return versions

    def should_skip(self, ctx, version: str, commit: str) -> tuple[bool, str]:
        return False, ""

    # ------------------------------------------------------------------ behaviour
    def toolchain(self, ctx, version: str, commit: str) -> str:
        return self.settings.get("toolchain", "msvc")

    def native_toolchain(self, ctx, version: str, commit: str) -> str:
        return self.settings.get("native_toolchain", self.toolchain(ctx, version, commit))

    def xmake_version(self, ctx, version: str, commit: str) -> str:
        from lib import xmake_env

        return xmake_env.xmake_version_for_date(ctx.config, self.commit_date(ctx, commit))

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
        variants = self.variants(ctx, version, commit)
        return variants[0]["configs"] if variants else {}

    def variants(self, ctx, version: str, commit: str) -> list[dict]:
        return self.settings.get("variants") or [{"name": "default", "configs": {}}]

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

    def report_skipped(self, ctx, known: list[dict]) -> list[str]:
        return []
