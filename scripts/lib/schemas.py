"""Typed boundaries.

Every field-name drift we hit during development (``variant`` renamed to
``entry``, the workflow input called ``plugin`` but dispatched as ``dep``,
``installRetries`` missing from the config) failed *late* and quietly — usually
as "the artifact was never selected, so xmake compiled from source". The models
here validate the three boundaries that matter (config, build result, index
document) at load/save time instead, so a mistake surfaces immediately.

They are a validation layer, not a rewrite: existing code keeps working with
plain dicts and the helpers below convert/validate at the edges.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field
import pydantic

PYDANTIC_V2 = pydantic.VERSION.startswith("2")


class _Base(BaseModel):
    model_config = ConfigDict(extra="allow")


class CacheRepo(_Base):
    owner: str
    repo: str


class PackageRepo(_Base):
    owner: str
    repo: str
    branch: str = "main"


class Repos(_Base):
    package: PackageRepo
    cache: CacheRepo
    index: dict[str, Any] = Field(default_factory=dict)


class Platform(_Base):
    plat: str = "windows"
    arch: str = "x64"
    mode: str = "release"
    vsToolset: str | None = None


class RecipeSource(_Base):
    name: str
    url: str


class RecipeSettings(_Base):
    sources: list[RecipeSource]
    snapshotMode: str = "version"
    cutoffOffsetDays: int = 3
    skipPackages: list[str] = Field(default_factory=list)
    fixupDir: str = "recipes/fixups"


class XmakeSettings(_Base):
    minArtifactsVersion: str = "3.0.0"
    installRetries: int = 3
    heartbeatSeconds: int = 30
    silentTimeoutSeconds: int = 1800
    timeoutSeconds: int = 5400
    cacheDir: str = ".cache/xmake"


class BotSettings(_Base):
    name: str = "github-actions[bot]"
    email: str = "41898282+github-actions[bot]@users.noreply.github.com"
    tokenEnv: str = "BUILDCACHE_BOT_TOKEN"


class ReleaseSettings(_Base):
    maxAssetsPerRelease: int = 800
    targetBranch: str | None = "assets"


class PluginSpec(_Base):
    name: str
    module: str
    enabled: bool = True
    settings: dict[str, Any] = Field(default_factory=dict)


class Config(_Base):
    revision: int = 1
    platform: Platform = Field(default_factory=Platform)
    repos: Repos
    release: ReleaseSettings = Field(default_factory=ReleaseSettings)
    artifacts: dict[str, Any] = Field(default_factory=dict)
    bot: BotSettings = Field(default_factory=BotSettings)
    recipes: RecipeSettings
    xmake: XmakeSettings = Field(default_factory=XmakeSettings)
    batch: dict[str, Any] = Field(default_factory=dict)
    plugins: list[PluginSpec]


class Artifact(_Base):
    package: str
    version: str
    buildhash: str
    key: str
    release: str
    asset: str
    url: str
    sha256: str
    size: int = 0
    toolset: str | None = None
    upstream_package: str | None = None
    container: str | None = None


class BuildResult(_Base):
    plugin: str = ""
    package: str
    version: str
    entry: str = "default"
    revision: int = 0
    configs: dict[str, Any] = Field(default_factory=dict)
    upstream_commit: str = ""
    upstream_date: str = ""
    xmake_version: str = ""
    toolchain: str = ""
    upstream_toolchain: str = ""
    vs_toolset: str | None = None
    recipe_commits: dict[str, str] = Field(default_factory=dict)
    platform: dict[str, Any] = Field(default_factory=dict)
    built_at: str = ""
    artifacts: list[Artifact] = Field(default_factory=list)


def validate(model, data: Any):
    """Validate ``data`` against ``model``, reporting the offending field."""
    return model.model_validate(data) if PYDANTIC_V2 else model.parse_obj(data)


def dump(model) -> dict:
    return model.model_dump(exclude_none=True) if PYDANTIC_V2 else model.dict(exclude_none=True)
