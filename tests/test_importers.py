"""Importers: WinSCP (ini + password de-obfuscation) and BlamixShell (its vault)."""
from __future__ import annotations

import json
import os

import pytest

from blamixfiles import importers as I
from blamixfiles.models import Site, Store
from blamixfiles.vault import Vault


def winscp_obfuscate(host: str, user: str, password: str) -> str:
    """WinSCP's EncryptPassword (simple algorithm), to produce test data."""
    hexs = "0123456789ABCDEF"

    def ch(c: int) -> str:
        v = (~c ^ 0xA3) & 0xFF
        return hexs[v >> 4] + hexs[v & 0x0F]
    data = (user + host + password).encode()
    shift = 7
    out = ch(0xFF) + ch(0x00) + ch(len(data)) + ch(shift)
    out += "".join(ch(0x41 + i) for i in range(shift))       # padding in front
    out += "".join(ch(b) for b in data)
    out += "".join(ch(0x30 + i) for i in range(5))           # padding after
    return out


def test_winscp_password_roundtrip():
    enc = winscp_obfuscate("example.com", "deploy", "s3cr3t pässword")
    assert I.winscp_password("example.com", "deploy", enc) == "s3cr3t pässword"


def write_ini(path, master=False):
    pw = winscp_obfuscate("web.example.com", "deploy", "hunter2")
    path.write_text(f"""[Configuration\\Security]
UseMasterPassword={1 if master else 0}

[Sessions\\Default%20Settings]
HostName=

[Sessions\\Clients/ACME%20web]
HostName=web.example.com
PortNumber=2222
UserName=deploy
Password={pw}
RemoteDirectory=/var/www/acme
LocalDirectory=C:%5CProjects%5Cacme

[Sessions\\Old%20FTP]
HostName=ftp.example.com
FSProtocol=5
Ftps=3
PortNumber=21
UserName=bob
FtpPasvMode=0

[Sessions\\Keys]
HostName=10.0.0.5
UserName=root
PublicKeyFile=C:%5Ckeys%5Cid.ppk
Tunnel=1

[Sessions\\Nextcloud]
HostName=cloud.example.com
FSProtocol=6
Ftps=1
UserName=mike

[Sessions\\Bucket]
HostName=s3.eu-central-1.amazonaws.com
FSProtocol=7
UserName=AKIAX
S3DefaultRegion=eu-central-1
""", encoding="utf-8")


def test_import_winscp_ini(tmp_path):
    p = tmp_path / "WinSCP.ini"
    write_ini(p)
    sites, notes = I.import_winscp(str(p))
    by = {s.name: s for s in sites}
    assert set(by) == {"ACME web", "Old FTP", "Keys", "Nextcloud", "Bucket"}    # defaults skipped
    acme = by["ACME web"]
    assert (acme.protocol, acme.host, acme.port, acme.username, acme.group) == (
        "sftp", "web.example.com", 2222, "deploy", "Clients")
    assert acme.password == "hunter2" and acme.auth == "password"
    assert acme.remote_dir == "/var/www/acme" and acme.local_dir == "C:\\Projects\\acme"
    ftp = by["Old FTP"]
    assert ftp.protocol == "ftps" and ftp.port == 0 and not ftp.ftp_passive
    assert by["Keys"].auth == "key" and by["Keys"].key_path == "C:\\keys\\id.ppk"
    assert by["Nextcloud"].protocol == "webdavs"
    assert by["Bucket"].protocol == "s3" and by["Bucket"].s3_region == "eu-central-1"
    assert any("tunnel" in n.lower() for n in notes)


def test_import_winscp_with_master_password(tmp_path):
    p = tmp_path / "WinSCP.ini"
    write_ini(p, master=True)
    sites, notes = I.import_winscp(str(p))
    acme = next(s for s in sites if s.name == "ACME web")
    assert acme.password == "" and acme.auth == "ask"
    assert any("master password" in n for n in notes)


def write_blamixshell_vault(path, password, data):
    """Same layout BlamixShell writes: b"SDV1" | salt | n_log2 | nonce | AES-GCM."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
    salt, n_log2, nonce = os.urandom(16), 10, os.urandom(12)
    key = Scrypt(salt=salt, length=32, n=2 ** n_log2, r=8, p=1).derive(password.encode())
    ct = AESGCM(key).encrypt(nonce, json.dumps(data).encode(), b"SDV1")
    path.write_bytes(b"SDV1" + salt + bytes([n_log2]) + nonce + ct)


SHELL_DATA = {"servers": [
    {"id": "bastion1", "name": "Bastion", "host": "bastion.example.com", "port": 22, "username": "ops",
     "auth": "key", "key_path": "~/.ssh/id_ed25519", "group": "Prod"},
    {"id": "web1", "name": "Web 1", "host": "10.0.1.10", "port": 2200, "username": "deploy",
     "auth": "password", "password": "pw1", "jump_id": "bastion1", "group": "Prod/EU", "color": "#ff0000"},
    {"id": "ssm1", "name": "SSM box", "host": "i-0abc", "connection": "ssm-shell"},
], "snippets": []}


def test_import_blamixshell(tmp_path):
    vault = tmp_path / "vault.sdv"
    write_blamixshell_vault(vault, "shellpw", SHELL_DATA)
    with pytest.raises(I.ImportError_, match="Wrong"):
        I.import_blamixshell(vault, "nope")
    sites, notes = I.import_blamixshell(vault, "shellpw")
    by = {s.name: s for s in sites}
    assert set(by) == {"Bastion", "Web 1"} and any("SSM box" in n for n in notes)
    assert by["Web 1"].protocol == "sftp" and by["Web 1"].port == 2200 and by["Web 1"].password == "pw1"
    assert by["Web 1"].jump_id == "bastion1" and by["Bastion"].auth == "key"

    # into a store that already has the bastion: the jump link points at the existing copy
    store = Store(Vault.create(tmp_path / "bf.bfv", "pw", n_log2=10), {})
    mine = Site(name="my bastion", protocol="sftp", host="bastion.example.com", username="ops")
    store.upsert(mine)
    assert store.import_sites(sites) == 1
    web = next(s for s in store.sites.values() if s.name == "Web 1")
    assert web.jump_id == mine.id


def test_not_a_blamixshell_vault(tmp_path):
    p = tmp_path / "x.sdv"
    p.write_bytes(b"BFV1" + os.urandom(80))
    with pytest.raises(I.ImportError_, match="isn't a BlamixShell vault"):
        I.import_blamixshell(p, "pw")
