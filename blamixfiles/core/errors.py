"""Turn any backend exception into one short sentence for the user."""
from __future__ import annotations

import ftplib
import socket
import ssl

from . import ssh
from .vfs import BackendError, Cancelled

# errors after which the connection is gone and should be reopened
CONNECTION_ERRORS = (EOFError, ConnectionError, socket.timeout, TimeoutError, ssl.SSLError,
                     ftplib.error_temp)


def friendly(e: BaseException) -> str:
    if isinstance(e, Cancelled):
        return "Cancelled"
    if isinstance(e, BackendError):
        return str(e)
    if isinstance(e, ftplib.error_perm):
        msg = str(e)
        code, _, text = msg.partition(" ")
        if code == "530":
            return "Login failed: check the username and password."
        if code == "550":
            return f"Not found or not allowed: {text}".rstrip(": ")
        if code == "553":
            return f"The server refused that file name: {text}"
        return msg
    if isinstance(e, ftplib.error_temp):
        return f"Temporary server error: {e}"
    if isinstance(e, ssl.SSLCertVerificationError):
        return f"TLS certificate problem: {e.verify_message or e}"
    if isinstance(e, ssl.SSLError):
        return f"TLS error: {e.reason or e}"
    if isinstance(e, PermissionError):
        return f"Permission denied: {e.filename}" if e.filename else "Permission denied"
    if isinstance(e, FileNotFoundError):
        return f"No such file or folder: {e.filename}" if e.filename else "No such file or folder"
    return ssh.friendly_error(e)


def is_connection_error(e: BaseException) -> bool:
    if isinstance(e, CONNECTION_ERRORS):
        return True
    try:
        import paramiko
        if isinstance(e, paramiko.SSHException) and "not open" in str(e).lower():
            return True
        if isinstance(e, OSError) and "socket is closed" in str(e).lower():
            return True
    except ImportError:
        pass
    return False
