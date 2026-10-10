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

BuildCache fills that channel by letting xmake itself do the work:

```
discover ──► build (per SDK version)                  ──► finalize
          sandboxed xmake installs the whole closure,     index + manifests,
          exports it, packs it, uploads the archives      commit and push
```

## Layout

| Location | Contents |
|---|---|
| `main` branch | framework: scripts, plugins, workflow, config |
| `index` branch | generated index (`<package>/index.json`, `<package>/versions/*.json`) |
| `assets` branch | empty branch that only exists as the target of release tags |
| GitHub Releases | the `*.tar.zst` artifacts, sharded as `<package>_<n>` |
| `zimuya4153/xmake-repo` | consumer-facing repository: prefixed recipes + manifests |

Nothing we publish reuses an upstream package name: every package is vendored
under the configured prefix (`mifcache_`), so official recipes keep working and
our additions stay purely additive.

## Using the cache

```lua
add_repositories("levimc-repo https://github.com/LiteLDev/xmake-repo.git")
add_repositories("llsdk https://github.com/zimuya4153/xmake-repo.git")

add_requires("mifcache_levilamina 26.51.6")

target("my-plugin")
    set_kind("shared")
    add_packages("mifcache_levilamina")
```

`mifcache_levilamina` resolves to the precompiled SDK and its dependencies
resolve to our prefixed precompiled packages as well. To make our artifacts take
precedence over the official mirror entirely, set
`XMAKE_BINARY_REPO=https://github.com/zimuya4153/xmake-repo.git`.

## Local usage

```bash
python scripts/vendor.py                     # (re)generate prefixed recipes
python scripts/discover.py --first-only      # what still needs building
python scripts/build.py --version 26.51.6    # build + publish one SDK version
python scripts/finalize.py --push            # index + manifests, push both repos
python scripts/selftest.py --component mifcache_levilamina
python scripts/status.py --versions
```

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

`finalize.py` verifies every indexed artifact against the release asset it
points at and refuses to finish on a mismatch, so a stale manifest can no longer
make consumers silently fall back to compiling. `cache.py verify` runs the same
check on demand.

Notes:

* Python 3.14+ and an authenticated `gh` CLI are required; there are no
  third-party Python dependencies.
* `BUILDCACHE_PROXY=http://127.0.0.1:2080` enables a proxy for one invocation.
  It is never persisted and never a default.
* `BUILDCACHE_PUSH_TOKEN` is only needed in CI to push the second repository.
* Sandboxes live in a deliberately short path (`<tempdrive>/.buildcache`) because
  CMake rejects object paths longer than 250 characters.

## What a build does

1. Snapshot `xmake-repo` and `levimc-repo` at the commit recorded for the run,
   overlay `recipes/fixups/` and repair legacy recipes (malformed patch files and
   4-component versions that xmake cannot parse).
2. Install the pinned xmake for that era, point every xmake directory inside the
   sandbox via `XMAKE_GLOBALDIR`, and rewind unpinned `git clone` calls to the
   release date of the version being built (a `git` shim placed first on `PATH`).
3. `xmake require --export` the closure, pack every exported install dir as
   `<package>/<version>/<buildhash>/`, upload it to a release shard and record it
   in the build result.
4. `finalize` writes the index branch and regenerates every manifest from the
   index, then pushes both repositories.

Re-running is idempotent: assets are addressed by name, manifests are rewritten
from the index, and the index only ever grows.

## Status and known issues

Verified end to end on Windows with xmake 3.1.1:

* the whole closure of a LeviLamina version is installed by xmake, exported,
  packed and published (24 artifacts for `mifcache_levilamina 26.51.6`);
* a consumer project resolves `mifcache_fmt 11.2.0` to our release asset and
  installs it as `(precompiled) .. ok` — no compilation happens
  (`python scripts/selftest.py --component mifcache_fmt --version 11.2.0`).

Open items:

* **Parallel artifact downloads.** Resolving a package that pulls many of our
  artifacts at once fails with
  `unmatched checksum, current hash(...) != original hash(...)` while the same
  artifact installs fine on its own. Every package downloads, packs and
  installs correctly individually, so this looks like xmake's download cache
  under concurrency rather than a publishing problem; it needs a follow-up with
  a single-artifact download versus a batch download.
* **Client variants.** The server variant is built and published; the client
  variant is wired up (`plugins/ll/plugin.py::variants`) but still has to be run
  (`python scripts/build.py --version <v> --variant client`).
* **Consumers need `levimc-repo`** in `add_repositories` because some LL-side
  dependencies (for example `prelink`) are only published there.
