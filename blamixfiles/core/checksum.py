"""Checksums: local hashing and the shell commands servers use for it."""
from __future__ import annotations

import hashlib
import shlex

ALGOS = ("sha256", "md5")                 # preference order
_TOOLS = {"sha256": ["sha256sum", "shasum -a 256", "sha256 -q"], "md5": ["md5sum", "md5 -q"]}


def local_hash(path: str, algo: str, should_stop=lambda: False) -> str:
    h = hashlib.new(algo)
    with open(path, "rb") as f:
        while chunk := f.read(1024 * 1024):
            if should_stop():
                from .vfs import Cancelled
                raise Cancelled()
            h.update(chunk)
    return h.hexdigest()


def remote_hash_via_shell(run, path: str, algo: str) -> str | None:
    """run(cmd) -> (code, stdout, stderr). Tries GNU coreutils, then BSD/macOS tools."""
    for tool in _TOOLS.get(algo, []):
        code, out, _err = run(f"{tool} -- {shlex.quote(path)} 2>/dev/null || {tool} {shlex.quote(path)}")
        if code == 0 and out.strip():
            word = out.strip().split()[0].lower()
            if len(word) in (32, 64) and all(c in "0123456789abcdef" for c in word):
                return word
    return None
