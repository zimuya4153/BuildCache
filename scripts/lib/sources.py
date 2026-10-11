"""One place that turns a recipe source into a ready-to-use snapshot.

The sequence — mirror the repository, check it out at the era's cutoff, overlay
our fixups, let the plugin repair project-specific problems, refresh the
generic repairs — used to be written out in more than one place (the build and the
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
    overlay: tuple[str, ...] = (),
) -> dict[str, tuple[Path, str]]:
    """Return ``{source name: (directory, commit)}`` for every configured source.

    ``cutoff`` rolls the source back to the ecosystem of the version being built
    (``label`` names the snapshot directory so per-version snapshots coexist);
    ``only`` limits the work to a single source.

    ``overlay`` is the other way round: take every source at its **newest** state
    -- a dependency has to be the recipe a consumer resolves today, or the
    artifact we publish sits at an address nobody asks for -- and then take the
    named package directories from ``cutoff`` instead. That is what the project's
    own package needs: its recipe is the one of its era, which is what pins the
    dependency versions and carries the repairs. The returned commit is the one
    the sources were taken at.
    """
    env = env or util.subprocess_env(config, proxy=getattr(ctx, "proxy", None))
    root = util.absolute(config.get("_snapshot_root", ".snapshots"))
    mode = str(config.get("recipes", {}).get("snapshotMode", "version"))
    prepared: dict[str, tuple[Path, str]] = {}

    for source in config.get("recipes", {}).get("sources", []):
        name = source["name"]
        if only and name != only:
            continue
        mirror = ctx.mirror(name, source["url"])
        wanted = None if mode == "head" or overlay else cutoff
        commit = repos.commit_at(mirror, wanted, env=env)
        tag = label or commit[:12]
        dest = root / f"{name}@{tag}"
        util.log(f"snapshot {name} @ {commit[:12]}" + (f" (before {wanted})" if wanted else ""))
        repos.snapshot(mirror, dest, commit=commit, env=env)
        for path in overlay:
            age = _overlay_from_cutoff(dest, cutoff, path, env=env)
            if age:
                util.log(f"  {path} kept at {age[:12]} (the project's own recipe)")
        for note in recipefix.apply_fixups(config, name, dest):
            util.log(note)
        for note in recipefix.plugin_repair(config, plugin, name, dest):
            util.log(note)
        for note in recipefix.normalize_tree(dest):
            util.log(note)
        prepared[name] = (dest, commit)

    return prepared


def sources_changed(config: dict, ctx, cutoff: str | None) -> bool:
    """Did anything move in the recipe sources since ``cutoff``?

    A version released so recently that nothing did is already built against the
    recipes a consumer resolves, so one pass covers both sides. An older one is
    not, and there the dependencies have to be taken from today's state to reach
    the addresses a consumer asks for.
    """
    if not cutoff:
        return False
    env = util.subprocess_env(config, proxy=getattr(ctx, "proxy", None))
    for source in config.get("recipes", {}).get("sources", []):
        mirror = ctx.mirror(source["name"], source["url"])
        if repos.commit_at(mirror, cutoff, env=env) != repos.commit_at(mirror, None, env=env):
            return True
    return False


def _overlay_from_cutoff(dest: Path, cutoff: str | None, path: str, env: dict | None = None) -> str:
    """Take ``path`` in the snapshot from the version's date; "" when absent.

    The snapshot is a clone of the mirror, so the older state is already there:
    one checkout of that directory is enough, and a source that does not carry
    the package (only one of them holds the project's recipe) is simply skipped.
    """
    if not cutoff:
        return ""
    commit = repos.commit_at(Path(dest), cutoff, env=env)
    if not commit:
        return ""
    listed = util.run_out(
        ["git", "-C", str(dest), "ls-tree", "-r", "--name-only", commit, "--", path],
        env=env,
        check=False,
    )
    if not listed:
        return ""
    util.run(
        ["git", "-C", str(dest), "checkout", commit, "--", path],
        env=env,
        quiet=True,
    )
    return commit
