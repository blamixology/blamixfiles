"""SCP: for SSH servers without the SFTP subsystem (routers, NAS boxes, locked-down hosts).

Transfers speak the scp protocol over an exec channel. SCP itself can't list, delete or
rename, so those run as shell commands (ls, mkdir, rm, mv) on the server: this needs a
Unix-like shell on the other side. Prefer SFTP whenever the server offers it.
"""
from __future__ import annotations

import os
import re
import shlex
import stat as statmod
from typing import BinaryIO

import paramiko

from .. import ssh
from ..vfs import Backend, BackendError, Capabilities, Entry, ProgressFn
from .ftp import parse_list_line

BLOCK = 64 * 1024

# GNU ls with --time-style=+%s: "-rw-r--r-- 1 user group 1234 1700000000 name"
_GNU = re.compile(r"^(?P<mode>[-dlbcpsDw][-rwxsStTl+.@]{9})\S*\s+\d+\s+(?P<owner>\S+)\s+(?P<group>\S+)\s+"
                  r"(?P<size>\d+)\s+(?P<ts>\d+)\s(?P<name>.+)$")


def _q(path: str) -> str:
    return shlex.quote(path)


class SCPBackend(Backend):
    name = "scp"
    caps = Capabilities(resume=False, rename=True, chmod=True, set_mtime=True,
                        symlinks=True, atomic_replace=True)

    def __init__(self, site, interactive=None):
        self.site = site
        self.interactive = interactive
        self.client: paramiko.SSHClient | None = None
        self._owns_client = True
        self._gnu_ls = True

    @classmethod
    def on_client(cls, site, client: paramiko.SSHClient) -> "SCPBackend":
        b = cls(site)
        b.client = client
        b._owns_client = False
        return b

    def connect(self) -> None:
        self.client = ssh.open_client(self.site, self.interactive)
        self._owns_client = True
        self.run("true")                 # fails early if the server refuses exec (e.g. sftp-only)

    def close(self) -> None:
        if self.client is not None and self._owns_client:
            try:
                self.client.close()
            except Exception:
                pass
        self.client = None

    @property
    def connected(self) -> bool:
        t = self.client.get_transport() if self.client else None
        return bool(t and t.is_active())

    # ------------------------------------------------------------ shell commands
    def _channel(self, command: str) -> paramiko.Channel:
        chan = self.client.get_transport().open_session(timeout=15)
        chan.exec_command(command)
        return chan

    def run(self, command: str, check: bool = True) -> tuple[int, str, str]:
        chan = self._channel(command)
        out, err = bytearray(), bytearray()
        while True:
            if chan.recv_ready():
                out += chan.recv(65536)
            if chan.recv_stderr_ready():
                err += chan.recv_stderr(65536)
            if chan.exit_status_ready() and not chan.recv_ready() and not chan.recv_stderr_ready():
                break
            if not (chan.recv_ready() or chan.recv_stderr_ready()):
                chan.status_event.wait(0.05)
        code = chan.recv_exit_status()
        chan.close()
        o, e = out.decode("utf-8", "replace"), err.decode("utf-8", "replace").strip()
        if check and code != 0:
            raise BackendError(e.splitlines()[-1] if e else f"'{command.split()[0]}' failed ({code})")
        return code, o, e

    def home(self) -> str:
        return self.run("pwd")[1].strip() or "/"

    def _parse(self, parent: str, line: str) -> Entry | None:
        m = _GNU.match(line) if self._gnu_ls else None
        if m:
            name, kind = m["name"], m["mode"][0]
            target = ""
            if kind == "l" and " -> " in name:
                name, target = name.split(" -> ", 1)
            from .ftp import _mode_from_string
            return Entry(name=name, path=self.join(parent, name), is_dir=kind == "d", size=int(m["size"]),
                         mtime=float(m["ts"]), mode=_mode_from_string(m["mode"]),
                         owner=f"{m['owner']}:{m['group']}", is_link=kind == "l", link_target=target)
        d = parse_list_line(line)
        if not d:
            return None
        return Entry(name=d["name"], path=self.join(parent, d["name"]), is_dir=d["is_dir"], size=d["size"],
                     mtime=d["mtime"], mode=d["mode"], owner=d["owner"], is_link=d["is_link"],
                     link_target=d["target"])

    def _ls(self, args: str, path: str) -> list[str]:
        if self._gnu_ls:
            code, out, err = self.run(f"LC_ALL=C ls {args} --time-style=+%s -- {_q(path)}", check=False)
            if code == 0:
                return out.splitlines()
            if "time-style" in err or "unrecognized" in err or "illegal option" in err or "invalid" in err:
                self._gnu_ls = False            # BusyBox / BSD ls
            else:
                raise BackendError(err.splitlines()[-1] if err else f"Can't list {path}")
        return self.run(f"LC_ALL=C ls {args} -- {_q(path)}")[1].splitlines()

    def list(self, path: str) -> list[Entry]:
        out = []
        for line in self._ls("-la", path):
            if line.startswith("total "):
                continue
            e = self._parse(path, line)
            if e and e.name not in (".", ".."):
                if e.is_link:        # follow links to folders so they open like folders
                    code, _, _ = self.run(f"test -d {_q(e.path)}", check=False)
                    e.is_dir = code == 0
                out.append(e)
        return out

    def stat(self, path: str) -> Entry | None:
        code, _, _ = self.run(f"test -e {_q(path)} -o -L {_q(path)}", check=False)
        if code != 0:
            return None
        lines = [ln for ln in self._ls("-lad", path) if ln.strip()]
        e = self._parse(self.parent(path), lines[0]) if lines else None
        if e is not None:
            e.name, e.path = self.basename(path), path
        return e

    def mkdir(self, path: str) -> None:
        self.run(f"mkdir -- {_q(path)}")

    def makedirs(self, path: str) -> None:
        self.run(f"mkdir -p -- {_q(path)}")

    def remove(self, path: str) -> None:
        self.run(f"rm -f -- {_q(path)}")

    def rmdir(self, path: str) -> None:
        self.run(f"rmdir -- {_q(path)}")

    def remove_tree(self, path: str) -> None:
        if path.rstrip("/") in ("", "/"):
            raise BackendError("Refusing to delete /")
        self.run(f"rm -rf -- {_q(path)}")

    def rename(self, src: str, dst: str) -> None:
        self.run(f"mv -f -- {_q(src)} {_q(dst)}")

    def chmod(self, path: str, mode: int) -> None:
        self.run(f"chmod {statmod.S_IMODE(mode):o} -- {_q(path)}")

    def set_mtime(self, path: str, mtime: float) -> None:
        # GNU touch; BusyBox/BSD fall back to a formatted date
        import time as _t
        if self.run(f"touch -m -d @{int(mtime)} -- {_q(path)}", check=False)[0] != 0:
            stamp = _t.strftime("%Y%m%d%H%M.%S", _t.localtime(mtime))
            self.run(f"touch -m -t {stamp} -- {_q(path)}", check=False)

    def checksum(self, path, algos=("sha256", "md5")):
        from ..checksum import remote_hash_via_shell
        for algo in algos:
            h = remote_hash_via_shell(lambda c: self.run(c, check=False), path, algo)
            if h:
                return algo, h
        return None

    # ------------------------------------------------------------ scp protocol
    @staticmethod
    def _ack(chan: paramiko.Channel) -> None:
        b = chan.recv(1)
        if b == b"\x00":
            return
        if not b:
            raise BackendError("The server closed the SCP channel (is scp installed there?)")
        msg = bytearray()
        while True:
            c = chan.recv(1)
            if not c or c == b"\n":
                break
            msg += c
        raise BackendError(msg.decode("utf-8", "replace").strip() or "SCP error")

    def download(self, path: str, fp: BinaryIO, offset: int = 0,
                 progress: ProgressFn | None = None) -> None:
        chan = self._channel(f"scp -f -- {_q(path)}")
        try:
            chan.sendall(b"\x00")
            header = bytearray()
            while True:
                c = chan.recv(1)
                if not c:
                    raise BackendError(f"Can't read {path}")
                if c == b"\n":
                    break
                header += c
            line = header.decode("utf-8", "replace")
            if line[:1] in ("\x01", "\x02"):
                raise BackendError(line[1:].strip())
            if line.startswith("T"):           # timestamps (only with -p): skip, ask again
                chan.sendall(b"\x00")
                return self._read_file(chan, path, fp, offset, progress, first=None)
            return self._read_file(chan, path, fp, offset, progress, first=line)
        finally:
            chan.close()

    def _read_file(self, chan, path, fp, offset, progress, first) -> None:
        if first is None:
            header = bytearray()
            while (c := chan.recv(1)) not in (b"\n", b""):
                header += c
            first = header.decode("utf-8", "replace")
        if not first.startswith("C"):
            raise BackendError(f"{path} is not a regular file")
        size = int(first.split(" ", 2)[1])
        chan.sendall(b"\x00")
        left, skip = size, offset             # SCP always sends the whole file: skip what we have
        while left > 0:
            chunk = chan.recv(min(BLOCK, left))
            if not chunk:
                raise BackendError("Connection lost during the transfer")
            left -= len(chunk)
            if skip:
                cut = min(skip, len(chunk))
                chunk, skip = chunk[cut:], skip - cut
                if not chunk:
                    continue
            fp.write(chunk)
            if progress:
                progress(len(chunk))
        self._ack(chan)
        chan.sendall(b"\x00")

    def upload(self, fp: BinaryIO, path: str, offset: int = 0,
               progress: ProgressFn | None = None) -> None:
        if offset:
            raise BackendError("SCP can't resume uploads")
        start = fp.tell()
        fp.seek(0, os.SEEK_END)
        size = fp.tell() - start
        fp.seek(start)
        chan = self._channel(f"scp -t -- {_q(path)}")
        try:
            self._ack(chan)
            name = self.basename(path).replace("\n", " ")
            chan.sendall(f"C0644 {size} {name}\n".encode())
            self._ack(chan)
            left = size
            while left > 0:
                chunk = fp.read(min(BLOCK, left))
                if not chunk:
                    raise BackendError("The local file got shorter during the upload")
                chan.sendall(chunk)
                left -= len(chunk)
                if progress:
                    progress(len(chunk))
            chan.sendall(b"\x00")
            self._ack(chan)
        finally:
            chan.close()
