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
  => download https://github.com/zimuya4153/BuildCache/releases/download/...mifcahce_levilamina-26.51.6-....7z .. ok
  => install mifcahce_levilamina 26.51.6 (precompiled) .. ok
```

The `(precompiled)` marker is the important part: without it xmake silently
falls back to building the package from source.
