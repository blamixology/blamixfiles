"""FTP, explicit FTPS (AUTH TLS) and implicit FTPS (port 990), on stdlib ftplib.

Fixes the classic ftplib problems:
- FTPS data connections reuse the control connection's TLS session. vsftpd
  (require_ssl_reuse), FileZilla Server and ProFTPD refuse data connections
  without it ("425 Unable to build data connection").
- The address in the PASV reply is ignored (ftplib's default) so NATed servers work.
- MLSD first (exact UTC times, sizes, types), falling back to parsing LIST output
  in Unix and DOS/IIS formats.
- Self-signed certificates can be pinned by SHA-256 fingerprint instead of turning
  verification off.
"""
from __future__ import annotations

import calendar
import ftplib
import hashlib
import re
import socket
import ssl
import time
from typing import BinaryIO

from ..vfs import Backend, BackendError, Capabilities, Entry, ProgressFn

BLOCK = 64 * 1024


class UntrustedCertificate(Exception):
    """The FTPS server's certificate isn't trusted. `fingerprint` can be pinned."""

    def __init__(self, fingerprint: str, host: str, reason: str):
        super().__init__(f"The server's TLS certificate isn't trusted ({reason})")
        self.fingerprint = fingerprint
        self.host = host
        self.reason = reason


class CertificateChanged(Exception):
    pass


class _TLS(ftplib.FTP_TLS):
    """FTP_TLS whose data connections resume the control connection's TLS session."""

    def ntransfercmd(self, cmd, rest=None):
        conn, size = ftplib.FTP.ntransfercmd(self, cmd, rest)
        if self._prot_p:
            conn = self.context.wrap_socket(conn, server_hostname=self.host,
                                            session=self.sock.session)
        return conn, size


class _ImplicitTLS(_TLS):
    """Implicit FTPS: TLS from the first byte (normally port 990)."""

    def connect(self, host="", port=0, timeout=-999, source_address=None):
        if host:
            self.host = host
        if port:
            self.port = port
        if timeout != -999:
            self.timeout = timeout
        if source_address is not None:
            self.source_address = source_address
        sock = socket.create_connection((self.host, self.port), self.timeout,
                                        source_address=self.source_address)
        self.sock = self.context.wrap_socket(sock, server_hostname=self.host)
        self.af = self.sock.family
        self.file = self.sock.makefile("r", encoding=self.encoding)
        self.welcome = self.getresp()
        return self.welcome


# ------------------------------------------------------------------ LIST parsing
_UNIX = re.compile(
    r"^(?P<mode>[-dlbcpsDw][-rwxsStTl+.@]{9})\S*\s+\d+\s+(?P<owner>\S+)\s+(?P<group>\S+)\s+"
    r"(?P<size>\d+)\s+(?P<month>\w{3})\s+(?P<day>\d{1,2})\s+(?P<yt>\d{4}|\d{1,2}:\d{2})\s(?P<name>.+)$")
_DOS = re.compile(
    r"^(?P<date>\d{2}-\d{2}-\d{2,4})\s+(?P<time>\d{1,2}:\d{2}(?:AM|PM)?)\s+(?P<dir><DIR>|\d+)\s+(?P<name>.+)$",
    re.I)
_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def _mode_from_string(s: str) -> int:
    kinds = {"d": 0o040000, "l": 0o120000, "-": 0o100000}
    mode = kinds.get(s[0], 0o100000)
    bits = [0o400, 0o200, 0o100, 0o040, 0o020, 0o010, 0o004, 0o002, 0o001]
    for ch, bit in zip(s[1:10], bits):
        if ch not in "-ST":          # S/T: setuid/sticky shown without the x bit
            mode |= bit
    return mode


def parse_list_line(line: str, now: time.struct_time | None = None) -> dict | None:
    """One LIST line -> {name, is_dir, is_link, target, size, mtime, mode, owner}; None if unparsable."""
    now = now or time.gmtime()
    m = _UNIX.match(line)
    if m:
        name = m["name"]
        target = ""
        kind = m["mode"][0]
        if kind == "l" and " -> " in name:
            name, target = name.split(" -> ", 1)
        month = _MONTHS.get(m["month"].lower(), 1)
        day = int(m["day"])
        yt = m["yt"]
        if ":" in yt:   # this year (or last year if that date is in the future)
            hh, mm = (int(x) for x in yt.split(":"))
            year = now.tm_year
            if (month, day) > (now.tm_mon, now.tm_mday + 1):
                year -= 1
        else:
            year, hh, mm = int(yt), 0, 0
        try:
            mtime = calendar.timegm((year, month, day, hh, mm, 0))
        except (ValueError, OverflowError):
            mtime = 0
        return dict(name=name, is_dir=kind == "d", is_link=kind == "l", target=target,
                    size=int(m["size"]), mtime=float(mtime), mode=_mode_from_string(m["mode"]),
                    owner=f"{m['owner']}:{m['group']}")
    m = _DOS.match(line)
    if m:
        mo, dd, yy = (int(x) for x in m["date"].split("-"))
        if yy < 100:
            yy += 2000 if yy < 70 else 1900
        t = m["time"].upper()
        pm = t.endswith("PM")
        hh, mi = (int(x) for x in t.rstrip("APM").split(":"))
        if pm and hh < 12:
            hh += 12
        if t.endswith("AM") and hh == 12:
            hh = 0
        is_dir = m["dir"].upper() == "<DIR>"
        return dict(name=m["name"], is_dir=is_dir, is_link=False, target="",
                    size=0 if is_dir else int(m["dir"]),
                    mtime=float(calendar.timegm((yy, mo, dd, hh, mi, 0))), mode=None, owner="")
    return None


def _mlsd_time(s: str) -> float:
    s = s.split(".")[0]
    try:
        return float(calendar.timegm(time.strptime(s, "%Y%m%d%H%M%S")))
    except ValueError:
        return 0.0


# ------------------------------------------------------------------ backend
class FTPBackend(Backend):
    name = "ftp"

    def __init__(self, site):
        self.site = site
        self.ftp: ftplib.FTP | None = None
        self.features: set[str] = set()
        self.caps = Capabilities(resume=True, rename=True, chmod=True, set_mtime=False,
                                 symlinks=False, atomic_replace=False)
        self._mlsd = True
        self.secure = site.protocol in ("ftps", "ftps-implicit")

    # ---- TLS
    def _context(self, verify: bool) -> ssl.SSLContext:
        ctx = ssl.create_default_context()
        if not verify:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        return ctx

    def _open(self, verify: bool) -> ftplib.FTP:
        site = self.site
        enc = site.ftp_encoding or "utf-8"
        port = site.effective_port
        if site.protocol == "ftp":
            f = ftplib.FTP(encoding=enc)
        elif site.protocol == "ftps":
            f = _TLS(context=self._context(verify), encoding=enc)
        else:
            f = _ImplicitTLS(context=self._context(verify), encoding=enc)
        f.connect(site.host, port, timeout=20)
        if site.protocol == "ftps":
            f.auth()
        return f

    @staticmethod
    def _peer_fingerprint(f: ftplib.FTP) -> str:
        der = f.sock.getpeercert(binary_form=True) or b""
        fp = hashlib.sha256(der).hexdigest().upper()
        return ":".join(fp[i:i + 2] for i in range(0, len(fp), 2))

    def connect(self) -> None:
        site = self.site
        if not self.secure:
            f = self._open(False)
        elif site.tls_pinned:
            f = self._open(False)
            fp = self._peer_fingerprint(f)
            if fp != site.tls_pinned:
                f.close()
                raise CertificateChanged(
                    "The server's TLS certificate changed since you trusted it "
                    f"(now {fp}). This can mean someone is intercepting the connection.")
        else:
            try:
                f = self._open(site.tls_verify)
            except ssl.SSLCertVerificationError as e:
                probe = self._open(False)
                fp = self._peer_fingerprint(probe)
                probe.close()
                raise UntrustedCertificate(fp, site.host, e.verify_message or str(e)) from None
        try:
            user = site.username or "anonymous"
            pw = site.password if site.auth != "anonymous" else "anonymous@"
            if site.auth == "anonymous":
                user = "anonymous"
            f.login(user, pw or "")
            if self.secure:
                f.prot_p()
            f.set_pasv(site.ftp_passive)
            try:
                feat = f.sendcmd("FEAT")
                self.features = {ln.strip().split(" ")[0].upper() for ln in feat.splitlines()[1:-1]}
            except ftplib.Error:
                self.features = set()
            if "UTF8" in self.features:
                try:
                    f.sendcmd("OPTS UTF8 ON")
                except ftplib.Error:
                    pass
            self._mlsd = "MLST" in self.features or not self.features
            self.caps.set_mtime = "MFMT" in self.features
            f.voidcmd("TYPE I")
        except Exception:
            try:
                f.close()
            except Exception:
                pass
            raise
        self.ftp = f

    def close(self) -> None:
        if self.ftp:
            try:
                self.ftp.quit()
            except Exception:
                try:
                    self.ftp.close()
                except Exception:
                    pass
        self.ftp = None

    @property
    def connected(self) -> bool:
        return self.ftp is not None and self.ftp.sock is not None

    def home(self) -> str:
        try:
            return self.ftp.pwd() or "/"
        except ftplib.Error:
            return "/"

    # ---- listing
    def list(self, path: str) -> list[Entry]:
        if self._mlsd:
            try:
                return self._list_mlsd(path)
            except ftplib.error_perm as e:
                if not str(e).startswith(("500", "501", "502", "504")):
                    raise
                self._mlsd = False
        return self._list_list(path)

    def _list_mlsd(self, path: str) -> list[Entry]:
        out = []
        for name, facts in self.ftp.mlsd(path, facts=["type", "size", "modify", "unix.mode", "perm"]):
            kind = facts.get("type", "").lower()
            if kind in ("cdir", "pdir") or name in (".", ".."):
                continue
            mode = None
            if "unix.mode" in facts:
                try:
                    mode = int(facts["unix.mode"], 8) | (0o040000 if kind == "dir" else 0o100000)
                except ValueError:
                    pass
            out.append(Entry(name=name, path=self.join(path, name), is_dir=kind == "dir",
                             size=int(facts.get("size", 0) or 0),
                             mtime=_mlsd_time(facts.get("modify", "")), mode=mode,
                             is_link=kind.startswith(("os.unix=symlink", "os.unix=slink"))))
        return out

    def _list_list(self, path: str) -> list[Entry]:
        lines: list[str] = []
        # "LIST -a" shows dotfiles on most servers; fall back to plain LIST
        try:
            self.ftp.retrlines(f"LIST -a {path}", lines.append)
        except ftplib.error_perm:
            lines.clear()
            self.ftp.retrlines(f"LIST {path}", lines.append)
        out = []
        for ln in lines:
            d = parse_list_line(ln)
            if not d or d["name"] in (".", ".."):
                continue
            out.append(Entry(name=d["name"], path=self.join(path, d["name"]), is_dir=d["is_dir"],
                             size=d["size"], mtime=d["mtime"], mode=d["mode"], owner=d["owner"],
                             is_link=d["is_link"], link_target=d["target"]))
        return out

    def stat(self, path: str) -> Entry | None:
        if self._mlsd and "MLST" in self.features:
            try:
                resp = self.ftp.sendcmd(f"MLST {path}")
            except ftplib.error_perm as e:
                if str(e).startswith("550"):
                    return None
                raise
            for line in resp.splitlines()[1:-1]:
                facts_s, _, _name = line.strip().partition(" ")
                facts = dict(f.split("=", 1) for f in facts_s.split(";") if "=" in f)
                facts = {k.lower(): v for k, v in facts.items()}
                kind = facts.get("type", "").lower()
                return Entry(name=self.basename(path), path=path, is_dir=kind in ("dir", "cdir"),
                             size=int(facts.get("size", 0) or 0),
                             mtime=_mlsd_time(facts.get("modify", "")))
        try:
            return super().stat(path)
        except ftplib.error_perm as e:
            if str(e).startswith("550"):
                return None
            raise

    # ---- changes
    def mkdir(self, path: str) -> None:
        self.ftp.mkd(path)

    def remove(self, path: str) -> None:
        self.ftp.delete(path)

    def rmdir(self, path: str) -> None:
        self.ftp.rmd(path)

    def rename(self, src: str, dst: str) -> None:
        self.ftp.rename(src, dst)

    def chmod(self, path: str, mode: int) -> None:
        try:
            self.ftp.sendcmd(f"SITE CHMOD {mode & 0o7777:o} {path}")
        except ftplib.error_perm as e:
            raise BackendError(f"The server refused to change permissions: {e}") from None

    def set_mtime(self, path: str, mtime: float) -> None:
        if self.caps.set_mtime:
            stamp = time.strftime("%Y%m%d%H%M%S", time.gmtime(mtime))
            try:
                self.ftp.sendcmd(f"MFMT {stamp} {path}")
            except ftplib.Error:
                pass

    # ---- data. A failed or cancelled transfer leaves the control connection in an
    # unknown state, so we drop it; the caller reconnects for the next operation.
    def download(self, path: str, fp: BinaryIO, offset: int = 0,
                 progress: ProgressFn | None = None) -> None:
        def cb(chunk: bytes) -> None:
            fp.write(chunk)
            if progress:
                progress(len(chunk))
        try:
            self.ftp.retrbinary(f"RETR {path}", cb, blocksize=BLOCK, rest=offset or None)
        except BaseException:
            self._drop()
            raise

    def upload(self, fp: BinaryIO, path: str, offset: int = 0,
               progress: ProgressFn | None = None) -> None:
        cmd = f"APPE {path}" if offset else f"STOR {path}"
        if offset:   # APPE appends: make sure the remote is exactly `offset` bytes
            try:
                size = self.ftp.size(path)
            except ftplib.Error:
                size = None
            if size != offset:
                cmd = f"STOR {path}"
                fp.seek(fp.tell() - offset)
        try:
            self.ftp.storbinary(cmd, fp, blocksize=BLOCK,
                                callback=(lambda c: progress(len(c))) if progress else None)
        except BaseException:
            self._drop()
            raise

    def _drop(self) -> None:
        try:
            if self.ftp:
                self.ftp.close()
        except Exception:
            pass
        self.ftp = None
