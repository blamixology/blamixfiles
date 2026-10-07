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

URL_SCHEMES = {"sftp": "sftp", "scp": "scp", "ftp": "ftp", "ftps": "ftps",
               "dav": "webdav", "webdav": "webdav", "davs": "webdavs", "webdavs": "webdavs",
               "ftpes": "ftps", "ftpis": "ftps-implicit", "s3": "s3", "smb": "smb"}


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
    ftp_tz: str = "auto"              # FTP LIST times: "auto" (detect with MDTM) or minutes from UTC, e.g. "120"
    tls_verify: bool = True           # FTPS: verify the server certificate
    tls_pinned: str = ""              # FTPS/WebDAVS: sha256 of a self-signed cert the user trusted
    s3_region: str = ""               # S3: region (empty = us-east-1 / provider default)
    jump_id: str = ""                 # SFTP/SCP: id of another saved site used as a jump host
    parallel: int = 3                 # simultaneous transfers for this site
    limit_up_kb: int = 0              # speed limit for this site in KB/s (0 = none), on top of the global one
    limit_down_kb: int = 0
    oauth_client_id: str = ""         # cloud drives: the app registration (empty = BLAMIXFILES_<P>_CLIENT_ID)
    oauth_client_secret: str = ""
    oauth_token: str = ""             # JSON: access + refresh token (kept in the vault)
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
    def is_cloud(self) -> bool:
        return self.protocol in ("gdrive", "dropbox", "onedrive")

    @property
    def address(self) -> str:
        if self.is_cloud:
            from .core.backends import PROTOCOLS
            return f"{PROTOCOLS[self.protocol]}" + (f" ({self.username})" if self.username else "")
        user = f"{self.username}@" if self.username else ""
        from .core.backends import DEFAULT_PORTS
        port = f":{self.port}" if self.port and self.port != DEFAULT_PORTS.get(self.protocol) else ""
        return f"{self.protocol}://{user}{self.host}{port}"

    @property
    def is_ssh(self) -> bool:
        return self.protocol in ("sftp", "scp")

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
        s.limit_up_kb = max(0, int(s.limit_up_kb or 0))
        s.limit_down_kb = max(0, int(s.limit_down_kb or 0))
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
            raise ValueError(f"Unknown protocol '{u.scheme}' (use sftp, scp, ftp, ftps, ftpis, dav, davs or s3)")
        if not u.hostname:
            raise ValueError("No host name in the address")
        s = cls(protocol=proto, host=u.hostname, port=u.port or 0,
                username=unquote(u.username or ""), password=unquote(u.password or ""))
        if proto in ("webdav", "webdavs", "s3") and not s.password and s.username:
            s.auth = "ask"
        if proto.startswith("ftp") and not s.username:
            s.auth = "anonymous"
        elif not s.password and proto in ("sftp", "scp"):
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
        self.sync_profiles: list[dict] = list(data.get("sync_profiles", []))

    def to_dict(self) -> dict:
        return {"version": 1, "sites": [asdict(s) for s in self.sites.values()],
                "groups": sorted(self.groups), "bookmarks": self.bookmarks,
                "sync_profiles": self.sync_profiles}

    def save(self) -> None:
        self.vault.save(self.to_dict())

    def keep_tokens(self) -> None:
        """Cloud drives refresh their sign-in now and then: store the new token in the vault."""
        from .core import oauth

        def saver(site_id: str, token: str) -> None:
            site = self.sites.get(site_id)
            if site is not None and site.oauth_token != token:
                site.oauth_token = token
                self.save()
        oauth.TOKEN_SAVER = saver

    def upsert(self, site: Site) -> None:
        self.sites[site.id] = site
        if site.group:
            self.groups.add(site.group)
        self.save()

    def delete(self, site_id: str) -> None:
        self.sites.pop(site_id, None)
        for s in self.sites.values():
            if s.jump_id == site_id:
                s.jump_id = ""
        self.sync_profiles = [p for p in self.sync_profiles if p.get("site_id") != site_id]
        self.save()

    # ---- sync profiles (stored as dicts; see core.sync.SyncProfile)
    def save_profile(self, profile: dict) -> None:
        self.sync_profiles = [p for p in self.sync_profiles if p["name"].lower() != profile["name"].lower()]
        self.sync_profiles.append(profile)
        self.sync_profiles.sort(key=lambda p: p["name"].lower())
        self.save()

    def find_profile(self, name: str) -> dict | None:
        return next((p for p in self.sync_profiles if p["name"].lower() == name.lower()), None)

    def delete_profile(self, name: str) -> None:
        self.sync_profiles = [p for p in self.sync_profiles if p["name"].lower() != name.lower()]
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
        """Add sites that aren't here yet (same protocol, host, port and user = same site).
        Jump-host links between imported sites are kept, also when the jump host
        already existed (it then points at the existing copy)."""
        existing = {(s.protocol, s.host.lower(), s.effective_port, s.username): s.id for s in self.sites.values()}
        same: dict[str, str] = {}             # imported id -> id of the site that's kept
        added_sites: list[Site] = []
        for s in sites:
            key = (s.protocol, s.host.lower(), s.effective_port, s.username)
            if not s.host:
                continue
            if key in existing:
                same[s.id] = existing[key]
                continue
            if s.id in self.sites:            # an id clash with an unrelated site: new id
                old = s.id
                s.id = Site().id
                same[old] = s.id
            self.sites[s.id] = s
            existing[key] = s.id
            if s.group:
                self.groups.add(s.group)
            added_sites.append(s)
        for s in added_sites:
            if s.jump_id:
                s.jump_id = same.get(s.jump_id, s.jump_id)
                if s.jump_id not in self.sites:
                    s.jump_id = ""
        if added_sites:
            self.save()
        return len(added_sites)


def open_or_create(path: Path, password: str, create: bool) -> Store:
    if create:
        return Store(Vault.create(path, password), {})
    v, data = Vault.open(path, password)
    return Store(v, data)
