"""Remember the vault's master password in the OS keychain, so the app can unlock
without typing it: Windows Credential Manager, macOS Keychain, or (Linux) the Secret
Service through `secret-tool`. No extra Python package: ctypes on Windows, the system
tools elsewhere. Every function fails soft: no keychain just means "type the password".
"""
from __future__ import annotations

import base64
import hashlib
import shutil
import subprocess
import sys
from pathlib import Path

SERVICE = "BlamixFiles"


def backend_name() -> str:
    """Human name of the store, or '' when this system has none."""
    if sys.platform == "win32":
        return "Windows Credential Manager"
    if sys.platform == "darwin":
        return "macOS Keychain" if shutil.which("security") else ""
    return "the system keyring" if shutil.which("secret-tool") else ""


def available() -> bool:
    return bool(backend_name())


def account_for(vault_path: Path) -> str:
    """One entry per vault file, so a portable copy and an installed one don't collide."""
    try:
        key = str(Path(vault_path).resolve()).lower()
    except OSError:
        key = str(vault_path).lower()
    return "vault-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


# `security ... -w` prints a password as HEX when it has non-ASCII characters (or a newline), so the
# macOS / Linux stores keep the secret as plain ASCII: "b64:" + base64 of the UTF-8 text.
_PREFIX = "b64:"


def _wrap(secret: str) -> str:
    return _PREFIX + base64.b64encode(secret.encode("utf-8")).decode("ascii")


def _unwrap(stored: str) -> str:
    if stored.startswith(_PREFIX):
        try:
            return base64.b64decode(stored[len(_PREFIX):], validate=True).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return ""
    return stored                                       # saved by an older version: plain text


def load(account: str) -> str | None:
    try:
        if sys.platform == "win32":
            return _win_load(account)
        if sys.platform == "darwin":
            r = _run(["security", "find-generic-password", "-s", SERVICE, "-a", account, "-w"])
        else:
            r = _run(["secret-tool", "lookup", "service", SERVICE, "account", account])
        out = r.stdout.decode("utf-8") if r and r.returncode == 0 else ""
        return _unwrap(out.rstrip("\r\n")) or None
    except Exception:
        return None


def save(account: str, secret: str) -> bool:
    try:
        if sys.platform == "win32":
            return _win_save(account, secret)
        if sys.platform == "darwin":
            r = _run(["security", "add-generic-password", "-U", "-s", SERVICE, "-a", account, "-w", _wrap(secret)])
        else:
            r = _run(["secret-tool", "store", "--label", "BlamixFiles vault", "service", SERVICE,
                      "account", account], stdin=_wrap(secret).encode("ascii"))
        return bool(r and r.returncode == 0)
    except Exception:
        return False


def delete(account: str) -> bool:
    try:
        if sys.platform == "win32":
            return _win_delete(account)
        if sys.platform == "darwin":
            r = _run(["security", "delete-generic-password", "-s", SERVICE, "-a", account])
        else:
            r = _run(["secret-tool", "clear", "service", SERVICE, "account", account])
        return bool(r and r.returncode == 0)
    except Exception:
        return False


def _run(cmd: list[str], stdin: bytes | None = None):
    if shutil.which(cmd[0]) is None:
        return None
    return subprocess.run(cmd, input=stdin, capture_output=True, timeout=20)


# ---------------------------------------------------------------- Windows
def _target(account: str) -> str:
    return f"{SERVICE}:{account}"


def _win_api():
    import ctypes
    from ctypes import wintypes

    class CREDENTIAL(ctypes.Structure):
        _fields_ = [("Flags", wintypes.DWORD), ("Type", wintypes.DWORD),
                    ("TargetName", wintypes.LPWSTR), ("Comment", wintypes.LPWSTR),
                    ("LastWritten", wintypes.FILETIME), ("CredentialBlobSize", wintypes.DWORD),
                    ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)), ("Persist", wintypes.DWORD),
                    ("AttributeCount", wintypes.DWORD), ("Attributes", ctypes.c_void_p),
                    ("TargetAlias", wintypes.LPWSTR), ("UserName", wintypes.LPWSTR)]

    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    adv.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                              ctypes.POINTER(ctypes.POINTER(CREDENTIAL))]
    adv.CredReadW.restype = wintypes.BOOL
    adv.CredWriteW.argtypes = [ctypes.POINTER(CREDENTIAL), wintypes.DWORD]
    adv.CredWriteW.restype = wintypes.BOOL
    adv.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    adv.CredDeleteW.restype = wintypes.BOOL
    adv.CredFree.argtypes = [ctypes.c_void_p]
    return ctypes, CREDENTIAL, adv


_CRED_TYPE_GENERIC = 1
_CRED_PERSIST_LOCAL_MACHINE = 2   # this PC only: never roams to other machines


def _win_save(account: str, secret: str) -> bool:
    ctypes, CREDENTIAL, adv = _win_api()
    blob = secret.encode("utf-16-le")
    buf = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob)
    cred = CREDENTIAL()
    cred.Type = _CRED_TYPE_GENERIC
    cred.TargetName = _target(account)
    cred.UserName = account
    cred.CredentialBlobSize = len(blob)
    cred.CredentialBlob = ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte))
    cred.Persist = _CRED_PERSIST_LOCAL_MACHINE
    return bool(adv.CredWriteW(ctypes.byref(cred), 0))


def _win_load(account: str) -> str | None:
    ctypes, CREDENTIAL, adv = _win_api()
    p = ctypes.POINTER(CREDENTIAL)()
    if not adv.CredReadW(_target(account), _CRED_TYPE_GENERIC, 0, ctypes.byref(p)):
        return None
    try:
        c = p.contents
        raw = ctypes.string_at(c.CredentialBlob, c.CredentialBlobSize)
        return raw.decode("utf-16-le") or None
    finally:
        adv.CredFree(p)


def _win_delete(account: str) -> bool:
    _, _, adv = _win_api()
    return bool(adv.CredDeleteW(_target(account), _CRED_TYPE_GENERIC, 0))
