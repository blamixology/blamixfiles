"""Sites (saved connections) and their encrypted store."""
from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .vault import Vault

COLORS = ["", "#ff6b6b", "#ffa94d", "#ffd43b", "#69db7c", "#38d9a9",
          "#4dabf7", "#748ffc", "#b197fc", "#f783ac"]

URL_SCHEMES = {"sftp": "sftp", "ftp": "ftp", "ftps": "ftps",
               "ftpes": "ftps", "ftpis": "ftps-implicit"}


def _id() -> str:
    return uuid.uuid4().hex[:12]


@dataclass
class Site:
    name: str = ""
    protocol: str = "sftp"            # sftp | ftp | ftps | ftps-implicit
    host: str = ""
    port: int = 0                     # 0 = protocol default
    username: str = ""
    auth: str = "password"            # password | key | agent | ask  (FTP: password | anonymous | ask)
    password: str = ""
    key_path: str = ""
    key_data: str = ""
    passphrase: str = ""
    remote_dir: str = ""              # opened on connect ("" = login directory)
    local_dir: str = ""               # local pane starts here ("" = home)
    group: str = ""
    color: str = ""
    production: bool = False          # tints the tab red, asks before deleting
    ftp_passive: bool = True
    ftp_encoding: str = "utf-8"
    tls_verify: bool = True           # FTPS: verify the server certificate
    tls_pinned: str = ""              # FTPS: sha256 of a self-signed cert the user trusted
    parallel: int = 3                 # simultaneous transfers for this site
    notes: str = ""
    last_connected: float = 0.0
    id: str = field(default_factory=_id)

    @property
    def effective_port(self) -> int:
        from .core.backends import DEFAULT_PORTS
        return int(self.port) or DEFAULT_PORTS.get(self.protocol, 22)

    @property
    def label(self) -> str:
        return self.name or self.address

    @property
    def address(self) -> str:
        user = f"{self.username}@" if self.username else ""
        from .core.backends import DEFAULT_PORTS
        port = f":{self.port}" if self.port and self.port != DEFAULT_PORTS.get(self.protocol) else ""
        return f"{self.protocol}://{user}{self.host}{port}"

    @property
    def is_ssh(self) -> bool:
        return self.protocol == "sftp"

    def matches(self, query: str) -> bool:
        q = query.strip().lower()
        hay = " ".join([self.name, self.host, self.username, self.group, self.protocol, self.notes]).lower()
        return all(term in hay for term in q.split())

    @classmethod
    def from_dict(cls, d: dict) -> "Site":
        known = {f.name for f in fields(cls)}
        s = cls(**{k: v for k, v in d.items() if k in known})
        s.port = int(s.port or 0)
        s.parallel = max(1, min(10, int(s.parallel or 3)))
        return s

    def copy(self) -> "Site":
        return Site.from_dict(asdict(self))

    @classmethod
    def from_url(cls, url: str) -> tuple["Site", str]:
        """'sftp://user:pw@host:2222/var/www' -> (Site, '/var/www').
        A bare 'host' or 'user@host' means SFTP."""
        url = url.strip()
        if "://" not in url:
            url = "sftp://" + url
        u = urlsplit(url)
        proto = URL_SCHEMES.get(u.scheme.lower())
        if not proto:
            raise ValueError(f"Unknown protocol '{u.scheme}' (use sftp, ftp, ftps or ftpis)")
        if not u.hostname:
            raise ValueError("No host name in the address")
        s = cls(protocol=proto, host=u.hostname, port=u.port or 0,
                username=unquote(u.username or ""), password=unquote(u.password or ""))
        if proto.startswith("ftp") and not s.username:
            s.auth = "anonymous"
        elif not s.password and proto == "sftp":
            s.auth = "ask"             # keys/agent first, then ask for a password
        path = unquote(u.path or "")
        s.remote_dir = path
        return s, path


class Store:
    """All sites in memory; written to the encrypted vault on every change."""

    def __init__(self, vault: Vault, data: dict):
        self.vault = vault
        self.sites: dict[str, Site] = {}
        self.groups: set[str] = set(data.get("groups", []))
        for d in data.get("sites", []):
            s = Site.from_dict(d)
            self.sites[s.id] = s
        self.bookmarks: list[dict] = list(data.get("bookmarks", []))

    def to_dict(self) -> dict:
        return {"version": 1, "sites": [asdict(s) for s in self.sites.values()],
                "groups": sorted(self.groups), "bookmarks": self.bookmarks}

    def save(self) -> None:
        self.vault.save(self.to_dict())

    def upsert(self, site: Site) -> None:
        self.sites[site.id] = site
        if site.group:
            self.groups.add(site.group)
        self.save()

    def delete(self, site_id: str) -> None:
        self.sites.pop(site_id, None)
        self.save()

    def touch(self, site_id: str) -> None:
        s = self.sites.get(site_id)
        if s:
            s.last_connected = time.time()
            self.save()

    def find(self, name_or_id: str) -> Site | None:
        if name_or_id in self.sites:
            return self.sites[name_or_id]
        low = name_or_id.lower()
        for s in self.sites.values():
            if s.name.lower() == low:
                return s
        return None

    def all_groups(self) -> list[str]:
        out = set(self.groups) | {s.group for s in self.sites.values() if s.group}
        for g in list(out):
            parts = g.split("/")
            for i in range(1, len(parts)):
                out.add("/".join(parts[:i]))
        return sorted(out, key=str.lower)

    def import_sites(self, sites: list[Site]) -> int:
        existing = {(s.protocol, s.host.lower(), s.effective_port, s.username) for s in self.sites.values()}
        added = 0
        for s in sites:
            key = (s.protocol, s.host.lower(), s.effective_port, s.username)
            if not s.host or key in existing:
                continue
            self.sites[s.id] = s
            existing.add(key)
            if s.group:
                self.groups.add(s.group)
            added += 1
        if added:
            self.save()
        return added


def open_or_create(path: Path, password: str, create: bool) -> Store:
    if create:
        return Store(Vault.create(path, password), {})
    v, data = Vault.open(path, password)
    return Store(v, data)
