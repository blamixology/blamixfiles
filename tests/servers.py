"""In-process test servers: SFTP (paramiko) and FTP/FTPS (pyftpdlib). No daemons needed."""
from __future__ import annotations

import datetime
import os
import posixpath
import socket
import threading
from pathlib import Path

import paramiko
from paramiko import SFTPAttributes, SFTPHandle, SFTPServer, SFTPServerInterface
from paramiko.sftp import SFTP_OK, SFTP_OP_UNSUPPORTED

USER, PASSWORD = "tester", "pw"
_HOST_KEY: paramiko.PKey | None = None


def host_key() -> paramiko.PKey:
    global _HOST_KEY
    if _HOST_KEY is None:
        _HOST_KEY = paramiko.RSAKey.generate(2048)
    return _HOST_KEY


# ------------------------------------------------------------------ SFTP
class _Handle(SFTPHandle):
    def stat(self):
        try:
            return SFTPAttributes.from_stat(os.fstat(self.readfile.fileno()))
        except OSError as e:
            return SFTPServer.convert_errno(e.errno)

    def chattr(self, attr):
        return SFTP_OK


class _StubSFTP(SFTPServerInterface):
    """Serves one folder as '/'. SFTP paths are POSIX on every OS: normalise them with
    posixpath (os.path would turn '/' into '\\' on Windows and escape to the drive root)
    and refuse anything that would still land outside ROOT."""

    ROOT = "/tmp"

    def _real(self, path: str) -> str:
        rel = self.canonicalize(path).lstrip("/")
        real = os.path.abspath(os.path.join(self.ROOT, *[p for p in rel.split("/") if p]))
        root = os.path.abspath(self.ROOT)
        if os.path.commonpath([real, root]) != root:
            raise PermissionError(13, "outside the test root", path)
        return real

    def canonicalize(self, path):
        return posixpath.normpath("/" + path.lstrip("/")) if path not in (".", "") else "/"

    def list_folder(self, path):
        real = self._real(path)
        try:
            out = []
            for name in os.listdir(real):
                attr = SFTPAttributes.from_stat(os.lstat(os.path.join(real, name)))
                attr.filename = name
                out.append(attr)
            return out
        except OSError as e:
            return SFTPServer.convert_errno(e.errno)

    def stat(self, path):
        try:
            return SFTPAttributes.from_stat(os.stat(self._real(path)))
        except OSError as e:
            return SFTPServer.convert_errno(e.errno)

    def lstat(self, path):
        try:
            return SFTPAttributes.from_stat(os.lstat(self._real(path)))
        except OSError as e:
            return SFTPServer.convert_errno(e.errno)

    def open(self, path, flags, attr):
        real = self._real(path)
        try:
            fd = os.open(real, flags | getattr(os, "O_BINARY", 0), 0o644)
        except OSError as e:
            return SFTPServer.convert_errno(e.errno)
        if flags & os.O_WRONLY:
            mode = "ab" if flags & os.O_APPEND else "wb"
        elif flags & os.O_RDWR:
            mode = "a+b" if flags & os.O_APPEND else "r+b"
        else:
            mode = "rb"
        f = os.fdopen(fd, mode)
        h = _Handle(flags)
        h.filename = real
        h.readfile = f
        h.writefile = f
        return h

    def remove(self, path):
        try:
            os.remove(self._real(path))
        except OSError as e:
            return SFTPServer.convert_errno(e.errno)
        return SFTP_OK

    def rename(self, oldpath, newpath):
        new = self._real(newpath)
        if os.path.exists(new):          # plain SFTP rename refuses an existing target
            return SFTPServer.convert_errno(17)
        try:
            os.rename(self._real(oldpath), new)
        except OSError as e:
            return SFTPServer.convert_errno(e.errno)
        return SFTP_OK

    def posix_rename(self, oldpath, newpath):
        try:
            os.replace(self._real(oldpath), self._real(newpath))
        except OSError as e:
            return SFTPServer.convert_errno(e.errno)
        return SFTP_OK

    def mkdir(self, path, attr):
        try:
            os.mkdir(self._real(path))
        except OSError as e:
            return SFTPServer.convert_errno(e.errno)
        return SFTP_OK

    def rmdir(self, path):
        try:
            os.rmdir(self._real(path))
        except OSError as e:
            return SFTPServer.convert_errno(e.errno)
        return SFTP_OK

    def chattr(self, path, attr):
        real = self._real(path)
        try:
            if attr._flags & attr.FLAG_PERMISSIONS:
                os.chmod(real, attr.st_mode)
            if attr._flags & attr.FLAG_AMTIME:
                os.utime(real, (attr.st_atime, attr.st_mtime))
        except OSError as e:
            return SFTPServer.convert_errno(e.errno)
        return SFTP_OK

    def readlink(self, path):
        try:
            return os.readlink(self._real(path))
        except OSError as e:
            return SFTPServer.convert_errno(e.errno)

    def symlink(self, target_path, path):
        return SFTP_OP_UNSUPPORTED


class _SSHIface(paramiko.ServerInterface):
    def get_allowed_auths(self, username):
        return "password"

    def check_auth_password(self, username, password):
        ok = username == USER and password == PASSWORD
        return paramiko.AUTH_SUCCESSFUL if ok else paramiko.AUTH_FAILED

    def check_channel_request(self, kind, chanid):
        return paramiko.OPEN_SUCCEEDED if kind == "session" else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED


class SFTPTestServer:
    """with SFTPTestServer(root) as srv: sftp://tester:pw@127.0.0.1:srv.port/"""

    __test__ = False

    def __init__(self, root: Path):
        self.root = str(root)
        self._sock = socket.create_server(("127.0.0.1", 0))
        self.port = self._sock.getsockname()[1]
        self._transports: list[paramiko.Transport] = []

    def __enter__(self):
        threading.Thread(target=self._serve, daemon=True).start()
        return self

    def __exit__(self, *exc):
        self._sock.close()
        for t in self._transports:
            t.close()

    def _serve(self):
        root = self.root

        class Stub(_StubSFTP):
            ROOT = root

        while True:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            t = paramiko.Transport(conn)
            t.add_server_key(host_key())
            t.set_subsystem_handler("sftp", SFTPServer, Stub)
            try:
                t.start_server(server=_SSHIface())
            except Exception:
                continue
            self._transports.append(t)
            threading.Thread(target=self._accept_channels, args=(t,), daemon=True).start()

    @staticmethod
    def _accept_channels(t):
        keep = []        # an unreferenced paramiko Channel closes itself when collected
        while t.is_active():
            ch = t.accept(1)
            if ch is not None:
                keep.append(ch)


# ------------------------------------------------------------------ FTP / FTPS
def self_signed_cert(folder: Path) -> Path:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=30)).sign(key, hashes.SHA256()))
    pem = folder / "cert.pem"
    pem.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                                      serialization.NoEncryption())
                    + cert.public_bytes(serialization.Encoding.PEM))
    return pem


class FTPTestServer:
    """with FTPTestServer(root, tls=True) as srv: ..."""

    __test__ = False

    def __init__(self, root: Path, tls: bool = False, certdir: Path | None = None):
        from pyftpdlib.authorizers import DummyAuthorizer
        from pyftpdlib.handlers import FTPHandler
        from pyftpdlib.servers import FTPServer

        auth = DummyAuthorizer()
        auth.add_user(USER, PASSWORD, str(root), perm="elradfmwMT")
        if tls:
            from pyftpdlib.handlers import TLS_FTPHandler   # needs pyOpenSSL: only import for FTPS

            class H(TLS_FTPHandler):
                pass
            H.certfile = str(self_signed_cert(certdir or Path(root).parent))
            H.tls_control_required = True
            H.tls_data_required = True
        else:
            class H(FTPHandler):
                pass
        H.authorizer = auth
        H.passive_ports = None
        self.server = FTPServer(("127.0.0.1", 0), H)
        self.port = self.server.address[1]

    def __enter__(self):
        threading.Thread(target=self.server.serve_forever, kwargs={"timeout": 0.2}, daemon=True).start()
        return self

    def __exit__(self, *exc):
        self.server.close_all()


# ------------------------------------------------------------------ real OpenSSH (for SCP)
class OpenSSHServer:
    """A throwaway sshd on 127.0.0.1 with key login for the current user.
    Needs OpenSSH installed and root (sshd's privilege separation); tests skip otherwise."""

    __test__ = False

    @staticmethod
    def available() -> bool:
        import shutil
        return (shutil.which("sshd") is not None or os.path.exists("/usr/sbin/sshd")) \
            and hasattr(os, "geteuid") and os.geteuid() == 0

    def __init__(self, workdir: Path):
        import getpass
        import subprocess
        self.dir = Path(workdir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.user = getpass.getuser()
        hk = self.dir / "host_ed25519"
        self.client_key = self.dir / "client_ed25519"
        for k in (hk, self.client_key):
            subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(k)], check=True)
        (self.dir / "authorized_keys").write_text((self.dir / "client_ed25519.pub").read_text())
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        self.port = s.getsockname()[1]
        s.close()
        cfg = self.dir / "sshd_config"
        cfg.write_text(
            f"Port {self.port}\nListenAddress 127.0.0.1\nHostKey {hk}\n"
            f"AuthorizedKeysFile {self.dir / 'authorized_keys'}\nPasswordAuthentication no\n"
            "PubkeyAuthentication yes\nPermitRootLogin yes\nStrictModes no\nUsePAM no\n"
            f"PidFile {self.dir / 'sshd.pid'}\n"
            "Subsystem sftp internal-sftp\n")
        self._cfg = cfg
        self.proc = None

    def __enter__(self):
        import subprocess
        import time
        os.makedirs("/run/sshd", exist_ok=True)
        sshd = "/usr/sbin/sshd"
        self.proc = subprocess.Popen([sshd, "-D", "-e", "-f", str(self._cfg)],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        for _ in range(50):
            try:
                socket.create_connection(("127.0.0.1", self.port), timeout=0.2).close()
                return self
            except OSError:
                time.sleep(0.1)
        self.proc.kill()
        raise RuntimeError("sshd didn't start: " + self.proc.stderr.read().decode())

    def __exit__(self, *exc):
        if self.proc:
            self.proc.terminate()
            self.proc.wait(5)


# ------------------------------------------------------------------ WebDAV (wsgidav + cheroot)
class WebDAVTestServer:
    """with WebDAVTestServer(root, tls=False) as srv: dav://tester:pw@127.0.0.1:srv.port/"""

    __test__ = False

    def __init__(self, root: Path, tls: bool = False, certdir: Path | None = None):
        from cheroot import wsgi
        from wsgidav.wsgidav_app import WsgiDAVApp
        config = {
            "host": "127.0.0.1", "port": 0,
            "provider_mapping": {"/": str(root)},
            "simple_dc": {"user_mapping": {"*": {USER: {"password": PASSWORD}}}},
            "http_authenticator": {"accept_basic": True, "accept_digest": False, "default_to_digest": False},
            "verbose": 0, "logging": {"enable": False, "enable_loggers": []},
            "property_manager": True, "lock_storage": True,
        }
        app = WsgiDAVApp(config)
        self.server = wsgi.Server(("127.0.0.1", 0), app, numthreads=8)
        if tls:
            from cheroot.ssl.builtin import BuiltinSSLAdapter
            pem = self_signed_cert(certdir or Path(root).parent)
            self.server.ssl_adapter = BuiltinSSLAdapter(str(pem), str(pem))
        self.server.prepare()
        self.port = self.server.bind_addr[1]

    def __enter__(self):
        threading.Thread(target=self.server.serve, daemon=True).start()
        return self

    def __exit__(self, *exc):
        self.server.stop()


# ------------------------------------------------------------------ S3 (moto)
class S3TestServer:
    """An in-process S3 API (moto). Site: host="http://127.0.0.1:<port>", any keys."""

    __test__ = False

    def __init__(self):
        from moto.server import ThreadedMotoServer
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        self.port = s.getsockname()[1]
        s.close()
        self.server = ThreadedMotoServer(ip_address="127.0.0.1", port=self.port, verbose=False)
        self.endpoint = f"http://127.0.0.1:{self.port}"

    def __enter__(self):
        self.server.start()
        return self

    def __exit__(self, *exc):
        self.server.stop()
