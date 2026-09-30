"""One connection behind a file pane: a private worker thread owns the backend, so
the UI never blocks. Handles the questions a connection can raise (host key,
certificate, password, 2FA) by asking on the UI thread, and reconnects once when
an operation finds the connection dropped."""
from __future__ import annotations

from typing import Any, Callable

import ftplib

import paramiko

from ..core import ssh
from ..core.backends import make_backend, open_backend
from ..core.backends.ftp import UntrustedCertificate
from ..core.backends.local import LocalBackend
from ..core.backends.sftp import SFTPBackend
from ..core.errors import friendly, is_connection_error
from ..core.vfs import Backend
from .bridge import Worker, ask_on_ui


class LoginCancelled(Exception):
    pass


class Session:
    def __init__(self, site=None, parent_widget=None, on_site_changed: Callable | None = None,
                 log: Callable[[str, bool], None] | None = None):
        """site=None -> the local filesystem."""
        self.site = site.copy() if site is not None else None
        self.parent = parent_widget
        self.on_site_changed = on_site_changed or (lambda s: None)
        self.log = log or (lambda m, e=False: None)
        self.backend: Backend | None = LocalBackend() if site is None else None
        self.worker = Worker(f"session-{site.host if site else 'local'}")
        self._closed = False

    @property
    def is_local(self) -> bool:
        return self.site is None

    @property
    def label(self) -> str:
        return "This computer" if self.site is None else self.site.label

    # ------------------------------------------------------------ running things
    def run(self, fn: Callable[[Backend], Any], ok: Callable[[Any], None] | None = None,
            err: Callable[[str], None] | None = None) -> None:
        """fn(backend) runs on the session thread (connecting first if needed)."""
        def job():
            b = self._ensure()
            try:
                return fn(b)
            except Exception as e:
                if self.is_local or not is_connection_error(e):
                    raise
                self.log("Connection lost, reconnecting …", True)
                self._drop()
                return fn(self._ensure())
        self.worker.submit(job, ok, (lambda e: err(friendly(e))) if err else None)

    def close(self) -> None:
        self._closed = True
        self.worker.submit(self._drop)
        self.worker.stop()

    # ------------------------------------------------------------ connecting
    def _drop(self) -> None:
        if self.backend is not None and not self.is_local:
            try:
                self.backend.close()
            except Exception:
                pass
            self.backend = None

    def _ensure(self) -> Backend:
        if self.backend is not None and (self.is_local or self.backend.connected):
            return self.backend
        self._drop()
        site = self.site
        # "ask": SFTP tries your SSH keys/agent first, FTP asks right away
        if site.auth == "ask" and not site.password and not site.is_ssh:
            self._prompt_password()
        for _ in range(4):
            b = make_backend(site, interactive=self._interactive)
            self.log(f"Connecting to {site.host}:{site.effective_port} …", False)
            try:
                b.connect()
            except ssh.UnknownHostKey as e:
                self._trust_host(e, changed=False)
                continue
            except ssh.ChangedHostKey as e:
                self._trust_host(e, changed=True)
                continue
            except (ssh.AuthCancelled, ssh.NeedsInput):
                raise
            except (paramiko.AuthenticationException, ftplib.error_perm) as e:
                login_failed = isinstance(e, paramiko.AuthenticationException) or str(e).startswith("530")
                if not (login_failed and site.auth == "ask"):
                    raise
                if site.password:
                    self.log("Login failed: wrong password?", True)
                elif site.is_ssh:
                    self.log("Your SSH keys weren't accepted; asking for the password", False)
                site.password = ""
                self._prompt_password()
                continue
            except UntrustedCertificate as e:
                from .dialogs import ask_certificate
                if not ask_on_ui(lambda e=e: ask_certificate(self.parent, e.host, e.fingerprint, e.reason)):
                    raise LoginCancelled("Certificate not trusted") from None
                site.tls_pinned = e.fingerprint
                self.on_site_changed(site)
                continue
            self.backend = b
            banner = getattr(b, "banner", "")
            self.log(f"Connected to {site.label}" + (f" ({banner.splitlines()[0][:80]})" if banner else ""),
                     False)
            return b
        raise LoginCancelled("Could not establish a trusted connection")

    def _prompt_password(self) -> None:
        from .dialogs import ask_password
        pw = ask_on_ui(lambda: ask_password(self.parent, self.site))
        if pw is None:
            raise LoginCancelled("Login cancelled")
        self.site.password = pw          # kept for this session (and its transfers) only

    def _trust_host(self, e, changed: bool) -> None:
        from .dialogs import ask_host_key
        fp = ssh.fingerprint(e.key)
        if not ask_on_ui(lambda: ask_host_key(self.parent, e.host_id, e.key.get_name(), fp, changed)):
            raise LoginCancelled("Host key not trusted") from None
        ssh.trust_host_key(e.host_id, e.key, replace=changed)

    def _interactive(self, title: str, instructions: str, prompts: list):
        from .dialogs import AuthPromptDialog

        def ask():
            dlg = AuthPromptDialog(self.site.label, title, instructions, prompts, self.parent)
            return dlg.answers() if dlg.exec() else None
        return ask_on_ui(ask)

    # ------------------------------------------------------------ transfers
    def transfer_connector(self, site) -> Backend:
        """Called on transfer worker threads. SFTP: open another channel on this
        session's SSH connection (no new login). FTP: a new login with the same
        (possibly prompted) credentials and pinned certificate."""
        b = self.backend
        if isinstance(b, SFTPBackend) and b.connected:
            return SFTPBackend.on_client(self.site, b.client)
        return open_backend(self.site)
