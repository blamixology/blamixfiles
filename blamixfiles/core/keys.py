"""SSH keys: make a new Ed25519 key pair and put the public half on a server (like ssh-copy-id).

The private key is written in the OpenSSH format every SSH client reads (optionally encrypted with a
passphrase), with permissions only you can read; the public key goes next to it as <name>.pub.
Installing appends it to ~/.ssh/authorized_keys over SFTP (creating .ssh with mode 700 and the file
with 600), unless that exact key is already there.
"""
from __future__ import annotations

import base64
import getpass
import os
import socket
import stat as statmod
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


class KeyError_(Exception):
    pass


def default_comment() -> str:
    try:
        user = getpass.getuser()
    except Exception:  # noqa: BLE001
        user = "user"
    return f"{user}@{socket.gethostname()} (BlamixFiles)"


def default_key_path() -> Path:
    return Path.home() / ".ssh" / "id_ed25519_blamixfiles"


def generate(path: Path, passphrase: str = "", comment: str = "", overwrite: bool = False) -> str:
    """Create the key pair; returns the public key line."""
    path = Path(path).expanduser()
    pub = path.with_name(path.name + ".pub")
    if not overwrite and (path.exists() or pub.exists()):
        raise KeyError_(f"{path} already exists: pick another name, or delete the old key first")
    key = Ed25519PrivateKey.generate()
    enc = (serialization.BestAvailableEncryption(passphrase.encode("utf-8")) if passphrase
           else serialization.NoEncryption())
    private = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH, enc)
    public = key.public_key().public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH)
    line = public.decode("ascii") + " " + (comment or default_comment())
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(private)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    pub.write_text(line + "\n", encoding="ascii")
    return line


def read_public_key(path: Path) -> str:
    """The first key line of a .pub file (checked to look like an SSH public key)."""
    text = Path(path).expanduser().read_text(encoding="utf-8", errors="replace").strip()
    line = text.splitlines()[0].strip() if text else ""
    parts = line.split()
    if len(parts) < 2 or not parts[0].startswith(("ssh-", "ecdsa-", "sk-")):
        raise KeyError_(f"{path} doesn't look like an SSH public key (.pub)")
    try:
        base64.b64decode(parts[1], validate=True)
    except ValueError:
        raise KeyError_(f"{path} doesn't look like an SSH public key (.pub)") from None
    return line


def _key_id(line: str) -> tuple[str, str]:
    p = line.split()
    return (p[0], p[1]) if len(p) >= 2 else ("", "")


def install(backend, public_line: str) -> bool:
    """Add the key to ~/.ssh/authorized_keys on the server. Returns False if it was already there."""
    if not getattr(backend.caps, "chmod", False):
        raise KeyError_("This connection can't set file permissions: use an SFTP site")
    home = backend.home().rstrip("/") or "/"
    ssh_dir = backend.join(home, ".ssh")
    auth = backend.join(ssh_dir, "authorized_keys")
    if backend.stat(ssh_dir) is None:
        backend.mkdir(ssh_dir)
    backend.chmod(ssh_dir, 0o700)
    existing = ""
    if backend.stat(auth) is not None:
        existing = backend.read_bytes(auth).decode("utf-8", errors="replace")
    wanted = _key_id(public_line)
    for ln in existing.splitlines():
        fields = ln.split()
        # options may come first: compare the type + key blob wherever they are
        for i in range(len(fields) - 1):
            if (fields[i], fields[i + 1]) == wanted:
                return False
    new = existing + ("" if not existing or existing.endswith("\n") else "\n") + public_line.strip() + "\n"
    backend.write_bytes(auth, new.encode("utf-8"))
    backend.chmod(auth, 0o600)
    return True


def private_mode_ok(path: Path) -> bool:
    """On Linux/macOS, SSH refuses private keys others can read."""
    if os.name == "nt":
        return True
    try:
        return not (os.stat(path).st_mode & (statmod.S_IRWXG | statmod.S_IRWXO))
    except OSError:
        return False
