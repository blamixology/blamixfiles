"""Protocol backends. `open_backend(site)` returns a connected Backend for a Site."""
from __future__ import annotations

from ..vfs import Backend

PROTOCOLS = {
    "sftp": "SFTP (SSH)",
    "scp": "SCP (SSH servers without SFTP)",
    "ftp": "FTP",
    "ftps": "FTPS (explicit TLS)",
    "ftps-implicit": "FTPS (implicit TLS)",
    "webdavs": "WebDAV over HTTPS",
    "webdav": "WebDAV (plain HTTP)",
    "s3": "S3-compatible storage",
    "smb": "SMB (Windows share, NAS)",
    "gdrive": "Google Drive",
    "dropbox": "Dropbox",
    "onedrive": "OneDrive",
}
DEFAULT_PORTS = {"sftp": 22, "scp": 22, "ftp": 21, "ftps": 21, "ftps-implicit": 990,
                 "webdav": 80, "webdavs": 443, "s3": 443, "smb": 445, "gdrive": 443, "dropbox": 443, "onedrive": 443}
CLOUD = ("gdrive", "dropbox", "onedrive")         # sign in through the browser (OAuth), no host or password


def make_backend(site, interactive=None) -> Backend:
    """Create (not yet connected) backend for a Site."""
    proto = site.protocol
    if proto == "sftp":
        from .sftp import SFTPBackend
        return SFTPBackend(site, interactive=interactive)
    if proto == "scp":
        from .scp import SCPBackend
        return SCPBackend(site, interactive=interactive)
    if proto in ("ftp", "ftps", "ftps-implicit"):
        from .ftp import FTPBackend
        return FTPBackend(site)
    if proto in ("webdav", "webdavs"):
        from .webdav import WebDAVBackend
        return WebDAVBackend(site)
    if proto == "s3":
        from .s3 import S3Backend
        return S3Backend(site)
    if proto == "smb":
        from .smb import SMBBackend
        return SMBBackend(site)
    if proto == "gdrive":
        from .gdrive import GDriveBackend
        return GDriveBackend(site)
    if proto == "dropbox":
        from .dropbox import DropboxBackend
        return DropboxBackend(site)
    if proto == "onedrive":
        from .onedrive import OneDriveBackend
        return OneDriveBackend(site)
    raise ValueError(f"Unsupported protocol: {proto}")


def open_backend(site, interactive=None) -> Backend:
    b = make_backend(site, interactive)
    b.connect()
    return b
