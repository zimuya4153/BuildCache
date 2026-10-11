# BuildCache

Prebuilt xmake packages for [LeviLamina](https://github.com/LiteLDev/LeviLamina)
and its whole dependency closure, so a plugin project downloads its SDK instead
of compiling it.

## How it works

xmake already knows how to install a *precompiled* package: while resolving a
dependency it looks for `packages/<letter>/<package>/<version>/manifest.txt` in
the configured repositories, picks the entry whose build id
(`<plat>-<arch>-vc<toolset>-<buildhash>`) matches the consumer's configuration,
and then downloads, verifies and unpacks that archive instead of building
anything.

That build id contains the toolchain the *consumer* resolves the package with,
so the same archive is addressed differently by an msvc project and a clang-cl
one. Instead of building everything twice, a plugin declares the extra consumer
toolchains (`toolchain_aliases`) and the build recomputes the build hash offline
with the very xmake that built the package: the alias is published only when the
recomputation of the built profile reproduces the artifact's own hash, and the
alias is just one more manifest entry pointing at the same archive.

**We publish manifests, nothing else.** There is no recipe fork and no package
prefix: a consumer adds our repository *before* the official ones and keeps
writing the upstream names. xmake looks a manifest up by the name it is
resolving, in *every* repository, so the recipe comes from the official
repository and the archive from ours -- nothing upstream is replaced, shadowed or
renamed, and a package we have no manifest for simply builds from source there as
before.

```
discover ──► build (per SDK version)                   ──► finalize
          sandboxed xmake installs the closure,            index + manifests,
          exports it, packs what is worth an archive,      release bodies,
          uploads it                                       commit and push
```

## Layout

| Location | Contents |
|---|---|
| `main` branch | framework: scripts, plugins, workflow, config |
| `index` branch | generated index (`<package>/index.json`, `<package>/versions/*.json`) |
| `assets` branch | empty branch that only exists as the target of release tags |
| GitHub Releases | the `*.7z` artifacts, sharded as `<package>_<n>` |
| the consumer repository (`repos.package` in `config.json`) | generated manifests, and nothing else |

## What a build does

1. Take the recipe sources. The project's **own** recipe comes from the version's
   era -- it is what pins the dependency versions and what our repairs are written
   for -- while the **dependency** recipes come from their current state, because
   that is what a consumer resolves. Building a dependency against an older
   sibling (cpr against libcurl 8.11 instead of 8.21) makes xmake's compatibility
   check reject the artifact, and every consumer then compiles it from source.
   An era whose xmake cannot read today's recipes (0.8.3's 2.8.6 dies on
   `on_source`, an API newer recipes use) falls back to its own recipes: that
   failure happens while the recipes are parsed, before anything is compiled, so
   trying costs seconds. In that case a second pass with a current xmake builds
   the dependency list a consumer resolves, and only that list.
2. Install the xmake of that era, point every xmake directory inside the sandbox
   via `XMAKE_GLOBALDIR`, and rewind unpinned `git clone` calls to the release
   date of the version (a `git` shim placed first on `PATH`).
3. `xmake require --export` the closure and pack every exported install dir as
   `<package>/<version>/<buildhash>/`, then upload it to a release shard.
   * A dependency that compiled nothing -- header-only, a data package -- is
     skipped: an archive would save a consumer nothing, and its hash does not
     even depend on the toolchain.
   * An artifact whose exact build is already published is reused instead of
     packed again: the build hash *is* the identity of a build. Bump `revision`
     in `config.json` to say "everything built before me is stale".
   * The sandbox is handed the manifests of everything already published, as its
     *binary* repository, so a dependency installs from the cache instead of being
     compiled all over again. xmake looks a manifest up in every repository it has
     loaded, and without one it compiles: LeviLamina is the dependency each plugin
     shares, so each plugin's round would otherwise rebuild the whole SDK.
   * A dependency that one of *our* plugins publishes -- LeviLamina, for each of
     its plugins -- is planned as its own job, *ahead* of the dependent, when the
     ledger has no artifact for the version that dependent resolves (the version
     is read from the recipes, era table included). A round therefore runs in two
     stages: the SDKs first, then the plugins, which turn the first stage's build
     results into manifests of their own. A job cannot see another job's files,
     and this is what makes the SDK build happen once per round instead of once
     per plugin.
   * Packing writes no timestamps, so packing the *same* install directory twice
     gives the same bytes. Two jobs that each compiled the same dependency can
     still end up with different archives (the objects carry their own build
     metadata); the upload clobbers and the index adopts whatever the release
     ended up serving, so no manifest ever points at bytes that are gone.
4. Derive the aliases of every artifact (the same archive, build hash recomputed
   for the other consumer toolchains) and record them next to it.
5. `finalize` writes the index branch, regenerates every manifest from the index
   (aliases included), refreshes the release bodies and pushes.

Re-running is idempotent: assets are addressed by name, manifests are rewritten
from the index, and `finalize` verifies every indexed artifact against the
release asset it points at before it finishes.

## Using the cache

```lua
add_repositories("levimc-repo https://github.com/LiteLDev/xmake-repo.git")
add_repositories("llsdk https://github.com/<owner>/xmake-repo.git")   -- the consumer repository

set_toolchains("msvc")     -- in the project file: it is part of the build hash
set_runtimes("MD")
add_requires("levilamina 26.51.6", {configs = {target_type = "server"}})

target("my-plugin")
    set_kind("shared")
    add_packages("levilamina")
```

Which toolchain the project *declares* decides which manifest entry it needs,
because the declaration is part of the build hash. Every published artifact is
therefore addressable in all three ways a project can write it down -- nothing,
`set_toolchains("msvc")`, and `set_toolchains("clang-cl")` -- from one archive.
It has to be `set_toolchains` in the project file: `xmake f --toolchain=...`
hashes the same but never selects artifacts (see `docs/tutorial.md`, section 5).

To make our artifacts take precedence over the official mirror entirely, set
`XMAKE_BINARY_REPO=https://github.com/<owner>/xmake-repo.git`.

## Running it

```bash
python scripts/cache.py trigger --plugin ll --first-only --local   # build one version here
python scripts/cache.py verify                                     # index vs published assets
python scripts/discover.py --first-only                            # what still needs building
python scripts/status.py --versions
```

The workflow runs a round **every four hours**, and on every change to
`config.json` (a `revision` bump is exactly the signal "everything published so
far is stale"). A round is incremental: `discover` queues only what is missing or
was built by an older revision, so a run with nothing to do costs a couple of
minutes of discovery and no build jobs at all -- which is what makes a frequent
schedule affordable, and what drains a backlog steadily. Two versions per plugin
per run (the `batch` input, default 2; running `scripts/discover.py` by hand
falls back to `batch.defaultSize` in `config.json`), one round at a time
(`concurrency`).

## Managing the cache

```bash
python scripts/cache.py list [--package PKG] [--versions]   # what is cached
python scripts/cache.py graph [--package PKG]               # dependency edges
python scripts/cache.py verify                              # index vs published assets

# deletion always prints its plan first; --yes applies it
python scripts/cache.py remove PKG@VERSION --dry-run
python scripts/cache.py remove PKG --include-deps --yes
python scripts/cache.py remove PKG --include-deps --include-dependents --recursive --yes
python scripts/cache.py remove '*' --all --yes --prune-releases

# one build round, locally or through the workflow
python scripts/cache.py trigger --plugin ll --first-only --local
python scripts/cache.py trigger --plugin ll --workflow --batch 5
```

Notes:

* Python 3.14+ and an authenticated `gh` CLI are required. Third-party Python
  packages (`semver`, `pydantic`, `pytest`) are installed automatically on first
  run; set `BUILDCACHE_NO_AUTO_INSTALL=1` to opt out.
* `BUILDCACHE_PROXY=http://127.0.0.1:2080` enables a proxy for one invocation.
  It is never persisted and never a default.
* The consumer repository is pushed with `PACKAGE_REPO_TOKEN` in CI (a PAT); the
  repository you are reading is pushed with the workflow's `GITHUB_TOKEN`.
* Sandboxes live in a deliberately short path (`<tempdrive>/.buildcache`) because
  CMake rejects object paths longer than 250 characters.

## Moving this to another owner or repository

Everything owner-specific is configuration:

1. `config.json`: `repos.cache.owner`/`repo` (this repository),
   `repos.package.owner`/`repo` (the consumer-facing one), and
   `repos.index.branch` / `release.targetBranch` if you want different names.
2. Secrets on the new repository: `PACKAGE_REPO_TOKEN`, a token allowed to push
   the consumer repository. `GITHUB_TOKEN` comes from Actions.
3. The consumer repository has to exist (empty is fine): its branches, release
   shards and contents are created by the code.
4. Places that spell the repositories out in prose or fixtures: `README.md`,
   `docs/tutorial.md`, `tests/consumer/xmake.lua`, `tests/consumer/README.md`.
5. Nothing else is owner-specific. Artifact URLs come from the index and the
   manifests, which are generated from `config.json`, and the releases are
   addressed by `release`/`asset` names that carry no owner.

A new repository starts with an empty cache, so the first rounds rebuild
everything; with the schedule above, five versions take two runs.

## Status and known issues

Verified end to end on Windows with xmake 3.1.1 -- a LeviLamina closure is
installed by xmake, exported, packed and published, and a consumer project
resolves it as `install levilamina <version> (precompiled) .. ok` with nothing
compiled.

Open items:

* A version whose era cannot read today's recipes needs the second pass, and that
  pass builds the dependency list the *current* recipe declares. Pinned
  dependencies of such an old version are served by the era build, which a
  consumer resolves to the same address as long as the recipe still declares that
  version the same way.
* Consumers need `levimc-repo` in `add_repositories` as well, because some
  LL-side dependencies (for example `prelink`) live only there.
