"""One place that turns a recipe source into a ready-to-use snapshot.

The sequence — mirror the repository, check it out at the era's cutoff, overlay
our fixups, let the plugin repair project-specific problems, refresh the
generic repairs — used to be written out three times (build, vendoring, the
consumer self-test). Any change to the order had to be made in all of them,
which is exactly the kind of drift that produces "works in one path, not the
other" bugs. It lives here now.
"""

from __future__ import annotations

from pathlib import Path

from . import recipefix, repos, util


def prepare(
    config: dict,
    ctx,
    plugin=None,
    *,
    cutoff: str | None = None,
    label: str = "",
    refresh: bool = False,
    only: str | None = None,
    env: dict | None = None,
) -> dict[str, tuple[Path, str]]:
    """Return ``{source name: (directory, commit)}`` for every configured source.

    ``cutoff`` rolls the source back to the ecosystem of the version being built
    (``label`` names the snapshot directory so per-version snapshots coexist);
    ``only`` limits the work to a single source.
    """
    env = env or util.subprocess_env(config, proxy=getattr(ctx, "proxy", None))
    root = util.absolute(config.get("_vendor_root", ".vendor"))
    mode = str(config.get("recipes", {}).get("snapshotMode", "version"))
    prepared: dict[str, tuple[Path, str]] = {}

    for source in config.get("recipes", {}).get("sources", []):
        name = source["name"]
        if only and name != only:
            continue
        mirror = ctx.mirror(name, source["url"])
        wanted = None if mode == "head" else cutoff
        commit = repos.commit_at(mirror, wanted, env=env)
        tag = label or commit[:12]
        dest = root / f"{name}@{tag}"
        util.log(f"snapshot {name} @ {commit[:12]}" + (f" (before {wanted})" if wanted else ""))
        repos.snapshot(mirror, dest, commit=commit, env=env)
        for note in recipefix.apply_fixups(config, name, dest):
            util.log(note)
        for note in recipefix.plugin_repair(config, plugin, name, dest):
            util.log(note)
        for note in recipefix.normalize_tree(dest):
            util.log(note)
        prepared[name] = (dest, commit)

    return prepared
