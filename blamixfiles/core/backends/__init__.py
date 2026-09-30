"""Protocol backends. `open_backend(site)` returns a connected Backend for a Site."""
from __future__ import annotations

from ..vfs import Backend

PROTOCOLS = {
    "sftp": "SFTP (SSH)",
    "ftp": "FTP",
    "ftps": "FTPS (explicit TLS)",
    "ftps-implicit": "FTPS (implicit TLS)",
}
DEFAULT_PORTS = {"sftp": 22, "scp": 22, "ftp": 21, "ftps": 21, "ftps-implicit": 990}


def make_backend(site, interactive=None) -> Backend:
    """Create (not yet connected) backend for a Site."""
    proto = site.protocol
    if proto == "sftp":
        from .sftp import SFTPBackend
        return SFTPBackend(site, interactive=interactive)
    if proto in ("ftp", "ftps", "ftps-implicit"):
        from .ftp import FTPBackend
        return FTPBackend(site)
    raise ValueError(f"Unsupported protocol: {proto}")


def open_backend(site, interactive=None) -> Backend:
    b = make_backend(site, interactive)
    b.connect()
    return b
