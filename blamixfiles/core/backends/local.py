"""The local filesystem, behind the same interface as the remotes."""
from __future__ import annotations

import os
import shutil
import stat as statmod
import string
import sys
from pathlib import Path
from typing import BinaryIO

from ..vfs import Backend, Capabilities, Entry, ProgressFn

CHUNK = 256 * 1024
IS_WIN = sys.platform == "win32"


def _drives() -> list[Entry]:
    out = []
    for letter in string.ascii_uppercase:
        root = f"{letter}:\\"
        if os.path.exists(root):
            out.append(Entry(name=root, path=root, is_dir=True))
    return out


class LocalBackend(Backend):
    name = "local"
    is_local = True
    caps = Capabilities(resume=True, rename=True, chmod=not IS_WIN, set_mtime=True,
                        symlinks=True, atomic_replace=True)

    def home(self) -> str:
        return str(Path.home())

    # "" is the drive list on Windows ("This PC")
    def join(self, *parts: str) -> str:
        return os.path.join(*parts)

    def parent(self, path: str) -> str:
        if not path:
            return ""
        p = Path(path)
        if p.parent == p:                       # a root: C:\ or /
            return "" if IS_WIN else str(p)
        return str(p.parent)

    def basename(self, path: str) -> str:
        return Path(path).name or path

    def normalize(self, path: str) -> str:
        if not path:
            return "" if IS_WIN else "/"
        path = os.path.expanduser(path)
        return os.path.normpath(os.path.abspath(path))

    def _entry(self, path: str, name: str | None = None) -> Entry:
        st = os.lstat(path)
        is_link = statmod.S_ISLNK(st.st_mode)
        target = ""
        if is_link:
            try:
                target = os.readlink(path)
                st = os.stat(path)
            except OSError:
                pass
        return Entry(name=name or os.path.basename(path), path=path,
                     is_dir=statmod.S_ISDIR(st.st_mode), size=st.st_size, mtime=st.st_mtime,
                     mode=st.st_mode, is_link=is_link, link_target=target)

    def list(self, path: str) -> list[Entry]:
        if IS_WIN and not path:
            return _drives()
        out = []
        with os.scandir(path) as it:
            for d in it:
                try:
                    out.append(self._entry(d.path, d.name))
                except OSError:
                    continue
        return out

    def stat(self, path: str) -> Entry | None:
        try:
            return self._entry(path)
        except FileNotFoundError:
            return None

    def mkdir(self, path: str) -> None:
        os.mkdir(path)

    def makedirs(self, path: str) -> None:
        os.makedirs(path, exist_ok=True)

    def remove(self, path: str) -> None:
        os.remove(path)

    def rmdir(self, path: str) -> None:
        os.rmdir(path)

    def remove_tree(self, path: str) -> None:
        shutil.rmtree(path)

    def rename(self, src: str, dst: str) -> None:
        os.replace(src, dst)

    def chmod(self, path: str, mode: int) -> None:
        os.chmod(path, mode)

    def set_mtime(self, path: str, mtime: float) -> None:
        os.utime(path, (mtime, mtime))

    def download(self, path: str, fp: BinaryIO, offset: int = 0,
                 progress: ProgressFn | None = None) -> None:
        with open(path, "rb") as f:
            f.seek(offset)
            while chunk := f.read(CHUNK):
                fp.write(chunk)
                if progress:
                    progress(len(chunk))

    def upload(self, fp: BinaryIO, path: str, offset: int = 0,
               progress: ProgressFn | None = None) -> None:
        with open(path, "r+b" if offset else "wb") as f:
            if offset:
                f.seek(offset)
                f.truncate()
            while chunk := fp.read(CHUNK):
                f.write(chunk)
                if progress:
                    progress(len(chunk))
