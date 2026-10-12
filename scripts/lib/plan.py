"""What a round builds, and in which order.

LeviLamina is a *dependency* of each of its plugins, so a round that builds a
plugin has to build the whole SDK around it -- ten minutes of compiler time in
that plugin's job, once per plugin, every round. The SDK's own artifact may even
have been published a moment earlier, in the same round, but a job cannot see
another job's files: the archive is already on its release, while the manifest
that points at it is written by ``finalize`` at the very end.

So the dependency is planned before the work starts. The version a dependent will
resolve is read from the recipes -- the declarations xmake itself reads, including
the era table (``["0.13.*"] = {"levilamina >=1.9.2 <1.10.0"}``) that LeviLamina's
plugins use -- the SDK's own build is queued ahead of the dependent, and the
workflow hands that stage's build results to the dependents, which turn them into
manifests (see ``manifest.write_results``).

Everything that cannot be resolved this way is not planned: the dependent then
builds as it always has, compiling whatever nobody published yet. That is the
safe direction -- a wrong guess would mean waiting for an artifact that never
matches, and nothing here waits.
"""

from __future__ import annotations

import re
from pathlib import Path

from . import repos, semver, util

_ADD_DEPS = re.compile(r'add_(?:deps|requires)\(\s*"([^"]+)"')
#: A key/value row of the era dependency table, e.g.
#: ``["0.13.*"] = {"levilamina >=1.9.2 <1.10.0"}``.
_ERA_ROW = re.compile(r'\[\s*"([^"]+)"\s*\]\s*=\s*\{([^}]*)\}')
_QUOTED = re.compile(r'"([^"]+)"')
_COMPARISONS = (">=", "<=", "==", ">", "<", "=")


def spec_name(spec: str) -> str:
    """``"levilamina >=1.9.2"`` -> ``"levilamina"``; ``""`` for nothing usable."""
    text = str(spec or "").split(",", 1)[0].strip()
    if not text:
        return ""
    return text.split()[0].split("[", 1)[0].strip().lower()


def spec_version(spec: str) -> str:
    """The version part of a spec -- which may be a range, a wildcard or empty."""
    text = str(spec or "").split(",", 1)[0].strip()
    parts = text.split(None, 1)
    return parts[1].strip() if len(parts) > 1 else ""


def satisfies(version: str, constraint: str) -> bool:
    """Does ``version`` satisfy ``constraint``? Raises ``ValueError`` if not readable.

    Only the constraint shapes this ecosystem writes are understood: a version, a
    comparison (``>=1.9.2 <1.10.0``), a wildcard (``0.13.*``, ``1.9.x``) or
    nothing at all. ``~``/``^`` are *not* guessed -- a wrong answer would plan the
    wrong version, and the caller has a safe alternative (build it as usual).
    """
    for clause in str(constraint or "").split():
        if clause.startswith(("~", "^")):
            raise ValueError(f"unsupported constraint {constraint!r}")
        operator = next((item for item in _COMPARISONS if clause.startswith(item)), "")
        operand = clause[len(operator):]
        if not operand:
            raise ValueError(f"unsupported constraint {constraint!r}")
        if operator:
            if not _compare(version, operator, operand):
                return False
            continue
        wildcard = _wildcard(operand)
        if wildcard is not None:
            parsed = semver.parse(version)
            digits = [parsed.major, parsed.minor, parsed.patch][: len(wildcard)]
            if digits != wildcard:
                return False
            continue
        if semver.compare(version, operand) != 0:
            return False
    return True


def _wildcard(text: str) -> list[int] | None:
    """``0.13.*``/``1.9.x`` -> ``[0, 13]``; ``None`` when this is not a wildcard."""
    parts = text.split(".")
    taken: list[int] = []
    for part in parts:
        if part in ("*", "x", "X"):
            return taken
        if not part.isdigit():
            return None
        taken.append(int(part))
    return None


def _compare(version: str, operator: str, operand: str) -> bool:
    result = semver.compare(version, operand)
    if operator == ">=":
        return result >= 0
    if operator == "<=":
        return result <= 0
    if operator == ">":
        return result > 0
    if operator == "<":
        return result < 0
    return result == 0


def pick(versions, constraint: str) -> str | None:
    """The newest version satisfying the constraint, the way xmake picks one.

    ``None`` when nothing satisfies it *or* when the constraint cannot be read:
    both mean "do not plan anything for this dependency".
    """
    try:
        matching = [version for version in versions if satisfies(version, constraint)]
    except ValueError:
        return None
    return semver.sort_desc(matching)[0] if matching else None


def era_version(config: dict, ctx, package: str, cutoff: str | None) -> str | None:
    """The newest version of ``package`` that existed at ``cutoff``.

    A dependency named *without* a version resolves to the newest one there is,
    which is right for a project of today and wrong for one of two years ago:
    `bsci` 0.4.0 says `add_requires("levilamina")`, and the newest SDK is one whose
    headers its sources were never compiled against -- which is how it failed
    ("definition of implicitly declared copy constructor": a definition that the
    SDK of its own era declared cannot be written for a class that no longer
    declares it).

    Asking the recipe repository what it looked like at the version's own date is
    the same rollback the recipe snapshot does, applied to that one dependency:
    the version list of a package *is* the set of its versions at that moment.
    """
    if not cutoff:
        return None
    for source in config.get("recipes", {}).get("sources", []):
        mirror = ctx.mirror(source["name"], source["url"])
        commit = repos.commit_at(mirror, cutoff)
        if not commit:
            continue
        directory = f"packages/{package[0].lower()}/{package}"
        listing = repos.file_at(mirror, commit, f"{directory}/versions/versions.txt")
        versions = [line.split()[0] for line in listing.splitlines() if line.split()]
        if not versions:
            recipe = repos.file_at(mirror, commit, f"{directory}/xmake.lua")
            versions = [
                _strip_url_alias(match.group(1))
                for match in re.finditer(r'add_versions\(\s*"([^"]+)"', recipe)
            ]
        if versions:
            return semver.sort_desc(versions)[0]
    return None


def _strip_url_alias(version: str) -> str:
    """``release:v1.15.5`` / ``git:v1.6.3`` pick a URL alias, not a version."""
    _prefix, sep, rest = version.partition(":")
    if sep and rest and "//" not in version:
        return rest
    return version


def recipe_specs(recipe_dir, version: str) -> list[str]:
    """The dependencies a recipe declares for ``version``.

    Three places carry them in this ecosystem: ``add_deps``/``add_requires`` in the
    recipe, the same in its per-version file, and the era table a recipe uses when
    the dependency follows from the version being built.
    """
    if not recipe_dir:
        return []
    recipe_dir = Path(recipe_dir)
    if not recipe_dir.is_dir():
        return []
    texts = [util.read_text(recipe_dir / "xmake.lua")]
    listing = recipe_dir / "versions" / f"{_version_file(version)}.lua"
    texts.append(util.read_text(listing))
    specs = [spec.strip() for text in texts for spec in _ADD_DEPS.findall(text)]
    for key, body in _ERA_ROW.findall(texts[0]):
        try:
            if not satisfies(version, key.strip()):
                continue
        except ValueError:
            continue
        specs += [item.strip() for item in _QUOTED.findall(body)]
    return [spec for spec in specs if spec]


def _version_file(version: str) -> str:
    return str(version).replace(".", "_").replace("-", "_")


def dependency_specs(plugin, ctx, version: str, commit: str, recipe_dir) -> list[str]:
    """Every spec that could pull an owned package into this project's build."""
    specs = list(recipe_specs(recipe_dir, version))
    for item in extra_deps(plugin, ctx, version, commit, recipe_dir):
        name = item.get("name") if isinstance(item, dict) else item
        wanted = item.get("version", "") if isinstance(item, dict) else ""
        specs.append(f"{name} {wanted}".strip())
    for spec in plugin.call("dependency_specs", ctx, version, commit) or []:
        specs.append(str(spec))
    return [spec for spec in specs if spec_name(spec)]


def own_pins(plugin, ctx, version: str, commit: str) -> dict[str, str]:
    """The versions the project's own file pins, by package name (lower case).

    A dependency is usually declared twice -- a range in the recipe
    (``levilamina >=1.7.6 <1.8.0``) and the version the project itself was built
    against in its own ``xmake.lua`` (``levilamina 1.7.6``) -- and xmake settles on
    the newest that satisfies, which is the range's answer, prereleases included.
    The build forces the project's own version on the whole resolve tree for that
    very reason (``build.py::dependency_overrides``), so planning has to make the
    same call: otherwise a round queues an SDK nobody installs (and makes the
    project wait for it) while the SDK it really needs stays unbuilt.

    A range the project itself names is not a pin and is left to xmake.
    """
    found: dict[str, str] = {}
    for item in plugin.call("extra_deps", ctx, version, commit) or []:
        name = str(item.get("name") or "")
        pinned = str(item.get("version") or "")
        if not name or not pinned:
            continue
        if any(char in pinned for char in "~^><*|"):
            continue
        found[name.lower()] = pinned
    return found


def extra_deps(plugin, ctx, version: str, commit: str, recipe_dir) -> list[dict]:
    """The project's own requires, minus the ones its recipe already declares.

    A project's ``xmake.lua`` names what *it* builds against, and the recipe names
    what the *package* depends on; when the two disagree, asking for both installs
    two SDKs. ``more-dimensions`` 0.13.0 pins ``levilamina 1.9.4`` in its own file
    while its recipe resolves ``>=1.9.2 <1.10.0`` to 1.9.9: the build then
    downloads 1.9.9 from the cache and compiles a whole 1.9.4 nobody asked for
    (xmake keeps it as ``levilamina_1``, a second version of the same package).

    The recipe's declaration is the one a consumer resolves, so it is the one that
    decides; an extra require only survives when the recipe says nothing about it
    (``bsci`` depends on LeviLamina that way).
    """
    declared = {spec_name(spec) for spec in recipe_specs(recipe_dir, version)}
    found: list[dict] = []
    for item in plugin.call("extra_deps", ctx, version, commit) or []:
        if isinstance(item, dict):
            name = str(item.get("name") or "")
        else:
            name, item = str(item), {"name": item}
        if not name or name.lower() in declared:
            continue
        found.append(item)
    return found


def job(config: dict, ctx, plugin, version: str, commit: str, entry: dict) -> dict:
    """One matrix entry: what a build job needs to know."""
    metadata = plugin.call("metadata") or {}
    toolchain_plan = plugin.call("toolchain_plan", ctx, version, commit) or {}
    return {
        "plugin": plugin.name,
        "package": metadata.get("package") or "",
        "recipe_package": metadata.get("package") or "",
        "version": version,
        "entry": entry["id"],
        "image": toolchain_plan.get("image") or "windows-latest",
        "vs_toolset": toolchain_plan.get("vs_toolset") or "",
        # A plugin that knows better says so (the era's own compiler, and whether
        # the image needs LLVM 22 installed on top of it); the safe answer for
        # anything else is to install it, which is what the rounds always did.
        "llvm": bool(toolchain_plan.get("llvm", True)),
        "commit": commit,
    }


def split(config: dict, ctx, plugins_list, jobs: list[dict], built):
    """``(free to run, what a dependent waits for, the dependents, notes)``.

    A job that resolves an owned package -- one a plugin of ours publishes, which
    is how LeviLamina reaches its plugins -- whose artifact is *not* in the ledger
    yet has to wait for that package's own build, which is queued here
    (deduplicated: one build per plugin version, whoever asked for it).

    The two groups are kept apart because only the dependents need the second one:
    the rest of the round -- the ordinary backlog -- runs at the same time as it,
    so a plugin is not delayed by whichever versions happen to be queued next.
    A job the round would have run anyway lands in the second group as soon as a
    dependent needs that very version, since the dependents read the results of
    the builds they wait for.

    An added build is itself planned for nothing: a dependency of a dependency
    would need a stage per hop, and the dependent compiles what nobody published
    instead -- which is what it always did.
    """
    by_package = {}
    for plugin in plugins_list:
        metadata = plugin.call("metadata") or {}
        package = metadata.get("package")
        if package:
            by_package[package.lower()] = plugin

    by_name = {plugin.name: plugin for plugin in plugins_list}
    queued = {(item["plugin"], item["version"]) for item in jobs}
    added: list[dict] = []
    needed: set[tuple[str, str]] = set()
    waits: dict[tuple[str, str], set[str]] = {}
    notes: list[str] = []
    for item in jobs:
        plugin = by_name.get(item["plugin"])
        waits_for: set[str] = set()
        if plugin is not None:
            specs = dependency_specs(
                plugin, ctx, item["version"], item["commit"],
                plugin.call("recipe_dir", ctx, item["version"], item["commit"]),
            )
            cutoff = plugin.call("recipe_cutoff", ctx, item["version"], item["commit"])
            # A dependency can be declared twice with different versions (the
            # recipe's own table, and the project file's `add_requires`), and xmake
            # settles on the newest of them -- unless the project's own file pins
            # one, which is the version its build is forced to use (see own_pins).
            pins = own_pins(plugin, ctx, item["version"], item["commit"])
            wanted: dict[str, set[str]] = {}
            for spec in specs:
                name = spec_name(spec)
                owner = by_package.get(name)
                if owner is None or owner.name == plugin.name:
                    continue
                version = pins.get(name.lower()) or _owned_version(
                    config, ctx, owner, spec, cutoff
                )
                if not version:
                    notes.append(f"{name}: cannot resolve the version of {spec!r}")
                    continue
                wanted.setdefault(name, set()).add(version)
            for name in sorted(wanted):
                versions = wanted[name]
                owner = by_package[name]
                version = semver.sort_desc(versions)[0]
                if len(versions) > 1:
                    notes.append(
                        f"{name}: declared as {', '.join(sorted(versions))}, planning {version}"
                    )
                if built(name, version):
                    continue
                if (owner.name, version) not in queued:
                    found = _owner_jobs(config, ctx, owner, version)
                    if not found:
                        notes.append(f"{owner.name} {version}: nothing to build for {spec!r}")
                        continue
                    queued.add((owner.name, version))
                    added += found
                needed.add((owner.name, version))
                waits_for.add(f"{name}@{version}")
        if waits_for:
            waits[(item["plugin"], item["version"])] = waits_for
    for key in sorted(waits):
        if key in needed:
            # Something else waits for *this* build, and a job cannot both wait
            # for its own dependency and be one.
            notes.append(f"{key[0]} {key[1]}: it is a dependency of another job, so it builds")
    dependencies = [
        item
        for item in list(jobs) + added
        if (item["plugin"], item["version"]) in needed
    ]
    waiting = [
        dict(item, waits_for=sorted(waits[(item["plugin"], item["version"])]))
        for item in jobs
        if (item["plugin"], item["version"]) in waits
        and (item["plugin"], item["version"]) not in needed
    ]
    first = [
        item for item in jobs if (item["plugin"], item["version"]) not in needed | set(waits)
    ]
    return first, dependencies, waiting, notes


def _owned_version(config: dict, ctx, owner, spec: str, cutoff: str | None) -> str | None:
    """The version of ``spec`` the owner recipe would resolve to.

    A spec that names no version is the interesting one: today's newest is not
    what the project was built against, so the era decides (see era_version).
    """
    constraint = spec_version(spec)
    if constraint:
        available = [item["version"] for item in owner.call("list_versions", ctx) or []]
        found = pick(available, constraint)
        if found:
            return found
        # Nothing today satisfies it: the package's own era may still hold the
        # version the project was built with, which is what it means.
        package = (owner.call("metadata") or {}).get("package") or ""
        era = era_version(config, ctx, package, cutoff) if package else None
        return era if era and satisfies(era, constraint) else None
    package = (owner.call("metadata") or {}).get("package") or ""
    era = era_version(config, ctx, package, cutoff) if package else None
    if era:
        return era
    available = [item["version"] for item in owner.call("list_versions", ctx) or []]
    return semver.sort_desc(available)[0] if available else None


def _owner_jobs(config: dict, ctx, owner, version: str) -> list[dict]:
    """The owner's own jobs for one version (its entries, its image).

    Deliberately not filtered by ``should_skip``: that hook answers "can *this*
    machine build it", and the job runs on the image the matrix names (an era's
    xmake needs an image of its own generation). A job that ends up skipping
    leaves the dependent to build as it always has.
    """
    commit = ""
    for item in owner.call("list_versions", ctx) or []:
        if item["version"] == version:
            commit = item["commit"]
            break
    return [
        job(config, ctx, owner, version, commit, entry)
        for entry in owner.entries(ctx, version, commit)
    ]
