# Consumer smoke test

Checks that a project which only adds the repositories can actually *use* the
cached SDK — i.e. that xmake picks our prebuilt artifact instead of compiling
LeviLamina.

```bash
cd tests/consumer
xmake f -y -p windows -a x64 -m release      # resolves + downloads the artifact
xmake -y -v                                  # builds the tiny shared library
```

What a successful run looks like:

```
  => download https://github.com/MiracleForest/iBuildCache/releases/download/levilamina_0/levilamina-3ea5781bf5dc46ad9fe1983c1735bfd5.7z .. ok
  => install levilamina 26.51.6 (precompiled) .. ok
```

The `(precompiled)` marker is the important part: without it xmake silently
falls back to building the package from source.

The artifact above is served by the *consumer* repository
(`repos.package` in `config.json`, `MiracleForest/xmake-repo` here) -- that is
the only repository that has to be added for the cache to be used; the shard it
points at lives in the releases of this framework repository.
