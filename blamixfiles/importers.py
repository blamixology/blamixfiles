"""Import saved sites from FileZilla (sitemanager.xml), WinSCP (WinSCP.ini or the Windows
registry) and BlamixShell (its encrypted vault)."""
from __future__ import annotations

import base64
import os
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

from .models import Site

# FileZilla's ServerProtocol enum
_FZ_PROTOCOLS = {"0": "ftp", "1": "sftp", "3": "ftps-implicit", "4": "ftps", "6": "ftp"}


def filezilla_default_path() -> Path:
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", "")) / "FileZilla" / "sitemanager.xml"
    # Linux and macOS both use ~/.config/filezilla
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "filezilla" / "sitemanager.xml"


def _remote_dir(value: str) -> str:
    """FileZilla stores paths as '1 0 3 var 3 www' (type, prefix, then length-prefixed parts)."""
    value = (value or "").strip()
    if not value:
        return ""
    try:
        head, _, rest = value.partition(" ")
        _prefix_len, _, rest = rest.partition(" ")
        parts = []
        while rest:
            n_s, _, rest = rest.partition(" ")
            n = int(n_s)
            parts.append(rest[:n])
            rest = rest[n + 1:]
        return "/" + "/".join(parts) if head == "1" else "/".join(parts)
    except (ValueError, IndexError):
        return ""


def _text(node, tag: str) -> str:
    el = node.find(tag)
    return (el.text or "").strip() if el is not None and el.text else ""


def _server(node, group: str) -> Site | None:
    host = _text(node, "Host")
    if not host:
        return None
    proto = _FZ_PROTOCOLS.get(_text(node, "Protocol") or "0")
    if proto is None:        # S3, WebDAV, Google Drive… (not supported yet)
        return None
    s = Site(name=_text(node, "Name"),
             protocol=proto, host=host, port=int(_text(node, "Port") or 0), username=_text(node, "User"),
             group=group)
    from .core.backends import DEFAULT_PORTS
    if s.port == DEFAULT_PORTS.get(proto):
        s.port = 0
    pw = node.find("Pass")
    if pw is not None and pw.text:
        s.password = (base64.b64decode(pw.text).decode("utf-8", "replace")
                      if pw.get("encoding") == "base64" else pw.text)
    logon = _text(node, "Logontype")
    if logon == "0":
        s.auth = "anonymous"
    elif logon in ("2", "3"):
        s.auth = "ask"
    elif logon == "5":
        s.auth = "key"
        s.key_path = _text(node, "Keyfile")
    elif proto == "sftp" and not s.password:
        s.auth = "agent"
    s.remote_dir = _remote_dir(_text(node, "RemoteDir"))
    s.local_dir = _text(node, "LocalDir")
    s.ftp_passive = _text(node, "PasvMode") != "MODE_ACTIVE"
    enc = _text(node, "EncodingType")
    if enc == "Custom":
        s.ftp_encoding = _text(node, "CustomEncoding") or "utf-8"
    comments = _text(node, "Comments")
    if _text(node, "Protocol") in ("", "0"):
        comments = ("FileZilla used 'TLS if available' for this site: switch the protocol to FTPS "
                    "if the server supports it.\n" + comments).strip()
    s.notes = comments
    return s


def import_filezilla(path: Path) -> list[Site]:
    root = ET.parse(path).getroot()
    servers = root.find("Servers")
    out: list[Site] = []

    def walk(node, group: str) -> None:
        for child in node:
            if child.tag == "Server":
                s = _server(child, group)
                if s:
                    out.append(s)
            elif child.tag == "Folder":
                name = (child.text or "").strip() or "Imported"
                walk(child, f"{group}/{name}" if group else name)
    if servers is not None:
        walk(servers, "")
    return out


# ======================================================================= WinSCP
# Sessions live in WinSCP.ini ([Sessions\Folder/Name] sections) or, by default on
# Windows, in the registry under HKCU\Software\Martin Prikryl\WinSCP 2\Sessions.
# Names and values are URL-encoded (%20 …). Passwords are obfuscated (not encrypted)
# unless a WinSCP master password is set; then they can't be read and are skipped.
_WINSCP_PROTOCOLS = {"0": "scp", "1": "sftp", "2": "sftp", "5": "ftp", "6": "webdav", "7": "s3"}
_WINSCP_REG = r"Software\Martin Prikryl\WinSCP 2"
_PW_MAGIC = 0xA3
_PW_FLAG = 0xFF


class ImportError_(Exception):
    """Something the user should hear about (bad file, wrong password …)."""


def winscp_default_source() -> str:
    """Path of WinSCP.ini if there is one, else "registry" on Windows, else ""."""
    candidates = []
    if sys.platform == "win32":
        candidates.append(Path(os.environ.get("APPDATA", "")) / "WinSCP.ini")
        for base in (os.environ.get("ProgramFiles(x86)", ""), os.environ.get("ProgramFiles", "")):
            if base:
                candidates.append(Path(base) / "WinSCP" / "WinSCP.ini")
    for c in candidates:
        if c.is_file():
            return str(c)
    return "registry" if sys.platform == "win32" else ""


def _unquote(v: str) -> str:
    from urllib.parse import unquote
    return unquote(v or "")


def winscp_password(host: str, user: str, encoded: str) -> str:
    """Undo WinSCP's password obfuscation (the 'simple' algorithm, keyed on user + host)."""
    hexs = "0123456789ABCDEF"
    s = encoded.strip().upper()
    pos = 0

    def nxt() -> int:
        nonlocal pos
        if pos + 2 > len(s):
            raise ValueError("truncated")
        a, b = hexs.index(s[pos]), hexs.index(s[pos + 1])
        pos += 2
        return (~(((a << 4) + b) ^ _PW_MAGIC)) & 0xFF

    flag = nxt()
    if flag == _PW_FLAG:
        nxt()                       # (version byte)
        length = nxt()
    else:
        length = flag
    skip = nxt() * 2                # random padding in front (read first: nxt() moves pos)
    pos += skip
    raw = bytes(nxt() for _ in range(length))
    text = raw.decode("utf-8", "replace") if raw else ""
    if flag == _PW_FLAG:
        key = user + host
        if text.startswith(key):
            text = text[len(key):]
    return text


def _winscp_ini(path: str) -> tuple[dict[str, dict[str, str]], bool]:
    """{session name: {key: value}} and whether a master password protects passwords."""
    sessions: dict[str, dict[str, str]] = {}
    master = False
    cur: dict[str, str] | None = None
    raw = Path(path).read_bytes()
    text = raw.decode("utf-8-sig", "replace") if not raw.startswith(b"\xff\xfe") else raw.decode("utf-16")
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(";"):
            continue
        if line.startswith("[") and line.endswith("]"):
            sect = line[1:-1]
            if sect.startswith("Sessions\\"):
                cur = sessions.setdefault(_unquote(sect[len("Sessions\\"):]), {})
            elif sect == "Configuration\\Security":
                cur = {}
                sessions.setdefault("\0security", cur)
            else:
                cur = None
            continue
        if cur is not None and "=" in line:
            k, _, v = line.partition("=")
            cur[k.strip()] = v.strip()
    sec = sessions.pop("\0security", {})
    master = sec.get("UseMasterPassword", "0") == "1"
    return sessions, master


def _winscp_registry() -> tuple[dict[str, dict[str, str]], bool]:
    import winreg  # noqa: PLC0415 (Windows only)
    sessions: dict[str, dict[str, str]] = {}
    master = False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _WINSCP_REG + r"\Configuration\Security") as k:
            master = str(winreg.QueryValueEx(k, "UseMasterPassword")[0]) == "1"
    except OSError:
        pass
    try:
        root = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _WINSCP_REG + r"\Sessions")
    except OSError:
        raise ImportError_("No WinSCP sessions found in the registry.") from None
    with root:
        i = 0
        while True:
            try:
                name = winreg.EnumKey(root, i)
            except OSError:
                break
            i += 1
            vals: dict[str, str] = {}
            with winreg.OpenKey(root, name) as k:
                j = 0
                while True:
                    try:
                        vn, vv, _t = winreg.EnumValue(k, j)
                    except OSError:
                        break
                    j += 1
                    vals[vn] = str(vv)
            sessions[_unquote(name)] = vals
    return sessions, master


def import_winscp(source: str) -> tuple[list[Site], list[str]]:
    """source: a WinSCP.ini path, or "registry". Returns (sites, notes for the user)."""
    from .core.backends import DEFAULT_PORTS
    sessions, master = _winscp_registry() if source == "registry" else _winscp_ini(source)
    sites: list[Site] = []
    notes: list[str] = []
    for full_name, v in sessions.items():
        if full_name == "Default Settings":
            continue
        host = _unquote(v.get("HostName", ""))
        if not host:
            continue
        proto = _WINSCP_PROTOCOLS.get(v.get("FSProtocol", "2"), "sftp")
        ftps = v.get("Ftps", "0")
        if proto == "ftp" and ftps == "1":
            proto = "ftps-implicit"
        elif proto == "ftp" and ftps in ("2", "3"):
            proto = "ftps"
        elif proto == "webdav" and ftps != "0":
            proto = "webdavs"
        group, _, name = full_name.rpartition("/")
        user = _unquote(v.get("UserName", ""))
        s = Site(name=name or host, protocol=proto, host=host, username=user, group=group)
        try:
            s.port = int(v.get("PortNumber", "0") or 0)
        except ValueError:
            s.port = 0
        if s.port == DEFAULT_PORTS.get(proto):
            s.port = 0
        if "Password" in v and master:
            s.auth = "ask"                      # saved, but locked by WinSCP's master password
        elif "Password" in v:
            try:
                s.password = winscp_password(host, user, v["Password"])
            except (ValueError, IndexError):
                notes.append(f"{full_name}: couldn't read the saved password")
        key = _unquote(v.get("PublicKeyFile", ""))
        if key and s.is_ssh:
            s.auth, s.key_path = "key", key
        elif s.is_ssh and not s.password and "Password" not in v:
            s.auth = "agent" if v.get("TryAgent", "1") != "0" else "ask"
        if proto == "s3":
            s.s3_region = _unquote(v.get("S3DefaultRegion", ""))
            if not s.password:
                s.auth = "ask"
        s.remote_dir = _unquote(v.get("RemoteDirectory", ""))
        s.local_dir = _unquote(v.get("LocalDirectory", ""))
        s.ftp_passive = v.get("FtpPasvMode", "1") != "0"
        if v.get("Tunnel", "0") == "1":
            notes.append(f"{full_name}: uses an SSH tunnel in WinSCP; set its jump host in the site settings")
        sites.append(s)
    if master:
        notes.append("WinSCP protects its passwords with a master password, so they weren't imported: "
                     "you'll be asked for each password on first connect.")
        for s in sites:
            if not s.password and s.auth == "password":
                s.auth = "ask"
    return sites, notes


# ======================================================================= BlamixShell
# BlamixShell (formerly ShellDeck) keeps its servers in vault.sdv, encrypted with its
# own master password: b"SDV1" | salt(16) | n_log2(1) | nonce(12) | AES-GCM ciphertext.
def blamixshell_default_path() -> Path | None:
    names = ("BlamixShell", "ShellDeck")
    if sys.platform == "win32":
        bases = [Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))]
    elif sys.platform == "darwin":
        bases = [Path.home() / "Library" / "Application Support"]
    else:
        bases = [Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))]
    for base in bases:
        for n in names:
            d = base / n
            pointer = d / "vault-location.txt"
            if pointer.is_file():
                target = pointer.read_text(encoding="utf-8").strip()
                if target:
                    p = Path(os.path.expandvars(os.path.expanduser(target)))
                    if p.is_file():
                        return p
            if (d / "vault.sdv").is_file():
                return d / "vault.sdv"
    return None


def _open_blamixshell_vault(path: Path, password: str) -> dict:
    import json

    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
    raw = Path(path).read_bytes()
    if len(raw) < 4 + 16 + 1 + 12 + 16 or raw[:4] != b"SDV1":
        raise ImportError_("That isn't a BlamixShell vault (vault.sdv).")
    salt, n_log2, nonce, ct = raw[4:20], raw[20], raw[21:33], raw[33:]
    key = Scrypt(salt=salt, length=32, n=2 ** n_log2, r=8, p=1).derive(password.encode("utf-8"))
    try:
        plain = AESGCM(key).decrypt(nonce, ct, b"SDV1")
    except InvalidTag:
        raise ImportError_("Wrong BlamixShell master password.") from None
    return json.loads(plain.decode("utf-8"))


def import_blamixshell(path: Path, password: str) -> tuple[list[Site], list[str]]:
    """Servers from a BlamixShell vault, as SFTP sites (same ids, so jump hosts still match)."""
    data = _open_blamixshell_vault(path, password)
    sites: list[Site] = []
    notes: list[str] = []
    for d in data.get("servers", []):
        conn = d.get("connection", "ssh")
        label = d.get("name") or d.get("host", "")
        if conn != "ssh":
            notes.append(f"{label}: skipped (AWS Systems Manager connections aren't supported for file transfer)")
            continue
        if not d.get("host"):
            continue
        port = int(d.get("port") or 22)
        s = Site(name=d.get("name", ""), protocol="sftp", host=d["host"], port=0 if port == 22 else port,
                 username=d.get("username", ""), group=d.get("group", ""), color=d.get("color", ""),
                 notes=d.get("notes", ""), jump_id=d.get("jump_id", ""))
        s.auth = d.get("auth", "password") if d.get("auth") in ("password", "key", "agent") else "password"
        s.password, s.key_path = d.get("password", ""), d.get("key_path", "")
        s.key_data, s.passphrase = d.get("key_data", ""), d.get("passphrase", "")
        if s.auth == "password" and not s.password:
            s.auth = "ask"
        if d.get("id"):
            s.id = d["id"]
        sites.append(s)
    return sites, notes
