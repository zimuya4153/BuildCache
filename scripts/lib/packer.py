"""Deterministic packing/unpacking of package install dirs.

Two containers are supported and both are consumed identically by xmake's
precompiled-artifact mechanism (download, extract, find ``manifest.txt``):

* ``.tar.zst`` via the Python 3.14 stdlib :mod:`compression.zstd`
* ``.7z`` via the 7-Zip CLI (the container the official xmake mirror uses)
"""

from __future__ import annotations

import os
import tarfile
from pathlib import Path

from . import util


def stdlib_zstd_available() -> bool:
    try:
        import compression.zstd  # noqa: F401
    except ImportError:
        return False
    return True


def default_container() -> str:
    """``7z`` is what xmake unpacks natively; it ships its own 7-Zip on Windows.

    ``tar.zst`` is offered as an option because the stdlib can produce it, but
    xmake 3.1 has no ``.tar.zst`` extractor on Windows.
    """
    return "7z" if util.find_tool("7z") or util.find_tool("7za") else "tar.zst"


def pack(src: Path, dest: Path, store_as: str, container: str | None = None) -> None:
    """Pack ``src`` so that the archive root contains ``store_as`` (3 levels deep).

    ``store_as`` must be a multi-level relative path such as
    ``zlib/v1.3.1/95786712bac941e795afa187a68143a4`` so the extracted tree keeps
    the ``<something>/<something>/<hash>/manifest.txt`` shape xmake expects.
    """
    src = Path(src)
    dest = Path(dest)
    container = container or default_container()
    dest.unlink(missing_ok=True)
    if not src.is_dir():
        raise RuntimeError(f"cannot pack missing directory: {src}")
    if container == "tar.zst":
        _pack_tar_zstd(src, dest, store_as)
    elif container == "7z":
        _pack_7z(src, dest, store_as)
    else:
        raise RuntimeError(f"unsupported container: {container}")


def unpack(archive: Path, dest: Path) -> None:
    archive = Path(archive)
    dest = Path(dest)
    util.ensure_dir(dest)
    if archive.suffix == ".zst":
        from compression import zstd

        with zstd.ZstdFile(archive, "rb") as handle:
            with tarfile.open(fileobj=handle, mode="r:") as tar:
                _safe_extract(tar, dest)
    elif archive.suffix == ".7z":
        util.run(["7z", "x", "-y", f"-o{dest}", str(archive)], quiet=True)
    else:
        raise RuntimeError(f"unsupported archive: {archive}")


def _pack_tar_zstd(src: Path, dest: Path, store_as: str) -> None:
    from compression import zstd

    store_as = store_as.replace("\\", "/").strip("/")
    with zstd.ZstdFile(dest, "wb", level=19) as handle:
        with tarfile.open(fileobj=handle, mode="w", format=tarfile.PAX_FORMAT) as tar:
            _add_tree(tar, src, store_as)


def _pack_7z(src: Path, dest: Path, store_as: str) -> None:
    store_as = store_as.replace("\\", "/").strip("/")
    # 7-Zip cannot rename while archiving, so stage the tree under a temp root.
    with util_tempdir() as tmp:
        staged = Path(tmp) / store_as
        util.ensure_dir(staged.parent)
        _copy_tree(src, staged)
        # No timestamps: the same package built again (another round, another
        # machine) has to produce the same bytes. It is published under the same
        # name, so an archive that differs byte for byte would leave the manifest's
        # checksum pointing at the upload that happened to be last -- which is how
        # a consumer ends up with "unmatched checksum".
        util.run(
            [
                "7z", "a", "-t7z", "-mx=9", "-mtm=off", "-mtc=off", "-mta=off",
                "-bso0", "-bsp0", str(dest), store_as,
            ],
            cwd=Path(tmp),
            quiet=True,
        )


def _add_tree(tar: tarfile.TarFile, src: Path, store_as: str) -> None:
    root = tarfile.TarInfo(store_as)
    root.type = tarfile.DIRTYPE
    root.mode = 0o755
    root.mtime = 0
    tar.addfile(root)
    for path in sorted(src.rglob("*")):
        rel = path.relative_to(src).as_posix()
        info = tar.gettarinfo(str(path), arcname=f"{store_as}/{rel}")
        info.uid = 0
        info.gid = 0
        info.uname = ""
        info.gname = ""
        info.mtime = 0
        if info.isreg():
            with open(path, "rb") as handle:
                tar.addfile(info, handle)
        else:
            tar.addfile(info)


def _copy_tree(src: Path, dest: Path) -> None:
    import shutil

    dest.mkdir(parents=True, exist_ok=True)
    for item in src.iterdir():
        target = dest / item.name
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            _copy_tree(item, target)
        else:
            shutil.copy2(item, target)


def _safe_extract(tar: tarfile.TarFile, dest: Path) -> None:
    dest = dest.resolve()
    for member in tar.getmembers():
        target = (dest / member.name).resolve()
        if os.path.commonpath([str(dest), str(target)]) != str(dest):
            raise RuntimeError(f"unsafe archive member: {member.name}")
    tar.extractall(dest)


class util_tempdir:
    """Small context manager wrapper so ``tempfile`` stays a local import."""

    def __init__(self):
        import tempfile

        self._tmp = tempfile.TemporaryDirectory(prefix="buildcache-pack-")

    def __enter__(self):
        return self._tmp.__enter__()

    def __exit__(self, *exc):
        return self._tmp.__exit__(*exc)
