"""Command line: scriptable transfers with saved sites or URLs.

  blamixfiles sites
  blamixfiles ls   <site>:/path | sftp://user@host/path
  blamixfiles get  <site>:/remote/file-or-folder  ./local-folder
  blamixfiles put  ./local-file-or-folder  <site>:/remote/folder   [--limit 512]
  blamixfiles import filezilla [sitemanager.xml]

Saved sites come from the encrypted vault: the master password is read from
BLAMIXFILES_VAULT_PASSWORD or asked for. Exit codes: 0 ok, 1 some transfers failed,
2 connection/login problem, 3 usage error.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import time

import paramiko

from . import __version__
from .core import engine as E
from .core import ssh
from .core.backends import open_backend
from .core.backends.ftp import UntrustedCertificate
from .core.errors import friendly
from .models import Site, Store
from .paths import vault_path
from .vault import Vault, WrongPassword


class UsageError(Exception):
    pass


def _store() -> Store:
    path = vault_path()
    if not Vault.exists(path):
        raise UsageError("No vault yet: open the BlamixFiles app once to create it, or use a URL.")
    pw = os.environ.get("BLAMIXFILES_VAULT_PASSWORD") or getpass.getpass("Master password: ")
    try:
        v, data = Vault.open(path, pw)
    except WrongPassword:
        raise UsageError("Wrong master password") from None
    return Store(v, data)


_store_cache: list[Store] = []


def store() -> Store:
    if not _store_cache:
        _store_cache.append(_store())
    return _store_cache[0]


def resolve(target: str) -> tuple[Site, str]:
    """'mysite:/var/www' (saved site) or a URL -> (Site, path)."""
    if "://" in target:
        site, path = Site.from_url(target)
        if site.auth == "ask" and not site.password and not site.is_ssh:
            site.password = getpass.getpass(f"Password for {site.username}@{site.host}: ")
        return site, path
    name, sep, path = target.partition(":")
    if not sep:
        raise UsageError(f"'{target}': use <site>:/path or a URL like sftp://user@host/path")
    site = store().find(name)
    if site is None:
        raise UsageError(f"No saved site named '{name}' (see: blamixfiles sites)")
    return site.copy(), path


def _yes(question: str) -> bool:
    if not sys.stdin.isatty():
        return False
    return input(question + " [y/N] ").strip().lower() in ("y", "yes")


def connect(site: Site):
    for _ in range(4):
        try:
            return open_backend(site)
        except ssh.UnknownHostKey as e:
            print(f"First connection to {e.host_id}: {e.key.get_name()} {ssh.fingerprint(e.key)}", file=sys.stderr)
            if not _yes("Trust this server?"):
                raise UsageError("Host key not trusted (run interactively once to accept it)") from None
            ssh.trust_host_key(e.host_id, e.key)
        except ssh.ChangedHostKey as e:
            raise UsageError(f"HOST KEY CHANGED for {e.host_id}: refusing to connect. If the server was "
                             "reinstalled, accept the new key in the app first.") from None
        except UntrustedCertificate as e:
            print(f"Untrusted certificate for {e.host} ({e.reason})\nSHA-256 {e.fingerprint}", file=sys.stderr)
            if not _yes("Trust it?"):
                raise UsageError("Certificate not trusted") from None
            site.tls_pinned = e.fingerprint
            if site.id in (store().sites if _store_cache else {}):
                store().sites[site.id].tls_pinned = e.fingerprint
                store().save()
        except paramiko.AuthenticationException:
            if site.auth == "ask" and not site.password and sys.stdin.isatty():
                site.password = getpass.getpass(f"Password for {site.username}@{site.host}: ")
                continue
            raise
    raise UsageError("Could not connect")


def cmd_sites(a) -> int:
    rows = sorted(store().sites.values(), key=lambda s: (s.group, s.label.lower()))
    if a.json:
        print(json.dumps([{"name": s.label, "group": s.group, "address": s.address} for s in rows], indent=2))
    else:
        for s in rows:
            print(f"{(s.group + '/') if s.group else ''}{s.label:30}  {s.address}")
    return 0


def cmd_ls(a) -> int:
    site, path = resolve(a.target)
    b = connect(site)
    try:
        path = path or b.home()
        entries = sorted(b.list(path), key=lambda e: (not e.is_dir, e.name.lower()))
        if a.json:
            print(json.dumps([{"name": e.name, "dir": e.is_dir, "size": e.size, "mtime": e.mtime,
                               "perms": e.perms} for e in entries], indent=2))
        else:
            for e in entries:
                when = time.strftime("%Y-%m-%d %H:%M", time.localtime(e.mtime)) if e.mtime else " " * 16
                print(f"{e.perms or '':11} {e.size if not e.is_dir else '':>12}  {when}  "
                      f"{e.name}{'/' if e.is_dir else ''}")
    finally:
        b.close()
    return 0


def _run(jobs_fn, policy: str, quiet: bool, limit_kb: int = 0) -> int:
    def on_change(j: E.Job) -> None:
        if quiet or j.is_dir:
            return
        if j.status == E.DONE:
            print(f"✔ {j.src} → {j.dst}")
        elif j.status == E.SKIPPED:
            print(f"· skipped (exists) {j.dst}")
        elif j.status == E.FAILED:
            print(f"✖ {j.src}: {j.error}", file=sys.stderr)
    eng = E.TransferEngine(lambda s: connect(s), workers=4, on_change=on_change,
                           policy=policy if policy != "ask" else "overwrite",
                           limit_up=limit_kb * 1024, limit_down=limit_kb * 1024)
    try:
        jobs_fn(eng)
        eng.wait()
    except KeyboardInterrupt:
        eng.cancel()
        eng.wait(10)
        print("Cancelled.", file=sys.stderr)
        return 1
    finally:
        eng.shutdown()
    failed = [j for j in eng.jobs if j.status == E.FAILED]
    done = [j for j in eng.jobs if j.status == E.DONE and not j.is_dir]
    if not quiet:
        total = sum(j.size for j in done)
        print(f"{len(done)} file(s), {total / 1e6:.1f} MB" + (f", {len(failed)} failed" if failed else ""))
    return 1 if failed else 0


def cmd_get(a) -> int:
    site, path = resolve(a.source)
    b = connect(site)
    try:
        entry = b.stat(path)
    finally:
        b.close()
    if entry is None:
        raise UsageError(f"Not found on the server: {path}")
    os.makedirs(a.dest, exist_ok=True)
    return _run(lambda eng: eng.download(site, entry, a.dest), a.policy, a.quiet, a.limit)


def cmd_put(a) -> int:
    site, path = resolve(a.dest)
    for src in a.sources:
        if not os.path.exists(src):
            raise UsageError(f"Not found: {src}")
    b = connect(site)             # settle host key / password questions before the workers start
    path = path or b.home()
    b.close()

    def queue(eng):
        for src in a.sources:
            eng.upload(site, os.path.abspath(src), path)
    return _run(queue, a.policy, a.quiet, a.limit)


def cmd_import(a) -> int:
    from .importers import filezilla_default_path, import_filezilla
    path = a.file or str(filezilla_default_path())
    if not os.path.exists(path):
        raise UsageError(f"Not found: {path}")
    sites = import_filezilla(path)
    added = store().import_sites(sites)
    print(f"Imported {added} new site(s) from {path} ({len(sites) - added} already there).")
    return 0


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="blamixfiles", description="BlamixFiles command line")
    ap.add_argument("--version", action="version", version=f"BlamixFiles {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("sites", help="list saved sites")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_sites)
    p = sub.add_parser("ls", help="list a remote folder")
    p.add_argument("target")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_ls)
    for name, fn, helptext in (("get", cmd_get, "download a file or folder"),
                               ("put", cmd_put, "upload files or folders")):
        p = sub.add_parser(name, help=helptext)
        if name == "get":
            p.add_argument("source")
            p.add_argument("dest", nargs="?", default=".")
        else:
            p.add_argument("sources", nargs="+")
            p.add_argument("dest")
        p.add_argument("--if-exists", dest="policy", default="overwrite",
                       choices=["overwrite", "skip", "newer", "resume"])
        p.add_argument("--limit", type=int, default=0, metavar="KB/s",
                       help="speed limit in KB/s (default: unlimited)")
        p.add_argument("-q", "--quiet", action="store_true")
        p.set_defaults(fn=fn)
    p = sub.add_parser("import", help="import sites from another client")
    p.add_argument("source", choices=["filezilla"])
    p.add_argument("file", nargs="?")
    p.set_defaults(fn=cmd_import)
    a = ap.parse_args(argv)
    try:
        code = a.fn(a)
    except UsageError as e:
        print(f"blamixfiles: {e}", file=sys.stderr)
        code = 3
    except (paramiko.AuthenticationException, OSError, EOFError) as e:
        print(f"blamixfiles: {friendly(e)}", file=sys.stderr)
        code = 2
    except Exception as e:  # noqa: BLE001
        print(f"blamixfiles: {friendly(e)}", file=sys.stderr)
        code = 2
    sys.exit(code)


if __name__ == "__main__":
    main()
