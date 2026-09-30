"""Import saved sites from FileZilla (sitemanager.xml)."""
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
