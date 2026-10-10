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
under the configured prefix (`mifcahce_`), so official recipes keep working and
our additions stay purely additive.

## Using the cache

```lua
add_repositories("levimc-repo https://github.com/LiteLDev/xmake-repo.git")
add_repositories("llsdk https://github.com/zimuya4153/xmake-repo.git")

add_requires("mifcahce_levilamina 26.51.6")

target("my-plugin")
    set_kind("shared")
    add_packages("mifcahce_levilamina")
```

`mifcahce_levilamina` resolves to the precompiled SDK and its dependencies
resolve to our prefixed precompiled packages as well. To make our artifacts take
precedence over the official mirror entirely, set
`XMAKE_BINARY_REPO=https://github.com/zimuya4153/xmake-repo.git`.

## Local usage

```bash
python scripts/vendor.py                     # (re)generate prefixed recipes
python scripts/discover.py --first-only      # what still needs building
python scripts/build.py --version 26.51.6    # build + publish one SDK version
python scripts/finalize.py --push            # index + manifests, push both repos
python scripts/selftest.py --component mifcahce_levilamina
python scripts/status.py --versions
```

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
