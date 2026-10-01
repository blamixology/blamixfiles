"""Command line: scriptable transfers with saved sites or URLs.

  blamixfiles sites
  blamixfiles ls   <site>:/path | sftp://user@host/path
  blamixfiles get  <site>:/remote/file-or-folder  ./local-folder
  blamixfiles put  ./local-file-or-folder  <site>:/remote/folder   [--limit 512]
  blamixfiles sync   ./dist mysite:/var/www --mirror --dry-run
  blamixfiles sync   "Deploy web"            (a profile saved in the app)
  blamixfiles profiles
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
import threading
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
        st = _store()
        _store_cache.append(st)
        ssh.set_site_resolver(lambda sid: st.sites.get(sid))      # jump hosts
    return _store_cache[0]


def resolve(target: str) -> tuple[Site, str]:
    """'mysite:/var/www' (saved site) or a URL -> (Site, path)."""
    if "://" in target:
        site, path = Site.from_url(target)
        if site.auth == "ask" and not site.password and not site.is_ssh:
            site.password = getpass.getpass(f"Password for {site.username}@{site.host}: ")
        return site, path
    name, sep, path = target.partition(":")
    if sep and len(name) == 1 and os.name == "nt":          # C:\path is a local path, not a site
        raise UsageError(f"'{target}' looks like a local path; remote targets are <site>:/path")
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


def _run(jobs_fn, policy: str, quiet: bool, limit_kb: int = 0, verify: bool = False) -> int:
    def on_change(j: E.Job) -> None:
        if quiet or j.is_dir:
            return
        if j.status == E.DONE:
            v = f"  [{j.verified} ok]" if j.verified not in ("", "unsupported") else (
                "  [not verified: server can't checksum]" if j.verified == "unsupported" else "")
            print(f"✔ {j.src} → {j.dst}{v}")
        elif j.status == E.SKIPPED:
            print(f"· skipped (exists) {j.dst}")
        elif j.status == E.FAILED:
            print(f"✖ {j.src}: {j.error}", file=sys.stderr)
    eng = E.TransferEngine(lambda s: connect(s), workers=4, on_change=on_change,
                           policy=policy if policy != "ask" else "overwrite",
                           limit_up=limit_kb * 1024, limit_down=limit_kb * 1024, verify=verify)
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
    return _run(lambda eng: eng.download(site, entry, a.dest), a.policy, a.quiet, a.limit, a.verify)


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
    return _run(queue, a.policy, a.quiet, a.limit, a.verify)


WATCH_STOP = threading.Event()      # (tests set it instead of pressing Ctrl+C)


def cmd_watch(a) -> int:
    from .core import watch as W
    import time
    local = os.path.abspath(a.local)
    if not os.path.isdir(local):
        raise UsageError(f"Not a folder: {a.local}")
    site, path = resolve(a.remote)
    b = connect(site)             # settle host key / password questions first
    path = path or b.home()
    join = b.join
    b.close()
    if site.production and not a.yes:
        if not _yes(f"{site.label} is marked as production. Upload every change to {path}?"):
            return 1
    ignore = W.DEFAULT_IGNORE + tuple(a.ignore or ())

    def on_change(j: E.Job) -> None:
        if a.quiet:
            return
        stamp = time.strftime("%H:%M:%S")
        if j.status == E.DONE:
            print(f"{stamp} ✔ {j.dst}", flush=True)
        elif j.status == E.FAILED:
            print(f"{stamp} ✖ {j.src}: {j.error}", file=sys.stderr, flush=True)
    eng = E.TransferEngine(lambda s: connect(s), workers=2, on_change=on_change, policy="overwrite",
                           limit_up=a.limit * 1024)
    w = W.FolderWatcher(local, lambda rels: W.queue_uploads(eng, site, local, path, rels, join),
                        ignore=ignore, interval=a.interval,
                        on_error=lambda m: print(f"watch: {m}", file=sys.stderr))
    w.start()
    if not a.quiet:
        print(f"Watching {local} → {site.label}:{path}  (Ctrl+C to stop; deletes are not uploaded)", flush=True)
    try:
        while not WATCH_STOP.wait(1):
            pass
    except KeyboardInterrupt:
        pass
    finally:
        WATCH_STOP.clear()
        w.stop()
        eng.wait(30)
        eng.shutdown()
    failed = sum(1 for j in eng.jobs if j.status == E.FAILED)
    return 1 if failed else 0


def cmd_import(a) -> int:
    from .importers import filezilla_default_path, import_filezilla
    path = a.file or str(filezilla_default_path())
    if not os.path.exists(path):
        raise UsageError(f"Not found: {path}")
    sites = import_filezilla(path)
    added = store().import_sites(sites)
    print(f"Imported {added} new site(s) from {path} ({len(sites) - added} already there).")
    return 0


def cmd_profiles(a) -> int:
    for p in store().sync_profiles:
        site = store().sites.get(p["site_id"])
        o = p.get("options", {})
        arrow = {"upload": "→", "download": "←", "both": "⇄"}.get(o.get("direction", "upload"), "→")
        print(f"{p['name']:28} {p['local_dir']} {arrow} {site.label if site else '(deleted site)'}:{p['remote_dir']}"
              + ("  [mirror]" if o.get("mirror") else ""))
    return 0


def cmd_sync(a) -> int:
    from .core import sync as S
    from .core.backends.local import LocalBackend
    if a.remote is None:                       # a saved profile
        prof_d = store().find_profile(a.source)
        if prof_d is None:
            raise UsageError(f"No sync profile named '{a.source}' (see: blamixfiles profiles)")
        prof = S.SyncProfile.from_dict(prof_d)
        site = store().sites.get(prof.site_id)
        if site is None:
            raise UsageError(f"The site of profile '{prof.name}' was deleted")
        site, local_root, remote_root, opt = site.copy(), prof.local_dir, prof.remote_dir, prof.options
    else:
        site, remote_root = resolve(a.remote)
        local_root = a.source
        opt = S.SyncOptions(tolerance=2.0 if site.is_ssh else 60.0)
    if a.direction:
        opt.direction = a.direction
    if a.mirror:
        opt.mirror = True
    if a.size_only:
        opt.compare = "size"
    if a.checksum:
        opt.compare = "checksum"
    if a.exclude:
        opt.excludes = opt.excludes + a.exclude
    if opt.direction == "both":
        opt.mirror = False
    if not os.path.isdir(local_root):
        raise UsageError(f"Local folder not found: {local_root}")

    b = connect(site)
    try:
        remote_root = remote_root or b.home()
        loc = S.scan(LocalBackend(), os.path.abspath(local_root), opt.excludes)
        rem = S.scan(b, remote_root, opt.excludes)
        plan = S.compare(loc, rem, os.path.abspath(local_root), remote_root, opt)
        if plan.to_check:
            S.resolve_checksums(plan, LocalBackend(), b)
        if a.json:
            print(json.dumps({"summary": plan.summary(), "actions": [
                {"action": x.kind, "path": x.rel, "dir": x.is_dir, "reason": x.reason, "bytes": x.size}
                for x in plan.actions]}, indent=2))
        elif not a.quiet or a.dry_run:
            for x in plan.actions:
                print(f"{S.ACTION_LABELS[x.kind]:24} {x.rel}{'/' if x.is_dir else ''}   ({x.reason})")
            print(plan.summary() + (f", {plan.transfer_bytes() / 1e6:.1f} MB to transfer"
                                    if plan.transfer_bytes() else ""))
        if a.dry_run or not any(x.kind != S.CONFLICT for x in plan.actions):
            return 0
        deletes = [x for x in plan.actions if x.kind in S.DELETES]
        if deletes and not a.yes:
            if not sys.stdin.isatty():
                raise UsageError(f"The plan deletes {len(deletes)} item(s): add --yes to allow that "
                                 "in scripts (or run with --dry-run first)")
            if not _yes(f"Delete {len(deletes)} item(s)?"):
                return 1
        return _run(lambda eng: S.apply(plan, site, eng, b, LocalBackend()), "overwrite", a.quiet, a.limit,
                    a.verify)
    finally:
        b.close()


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
        p.add_argument("--verify", action="store_true", help="compare checksums after each file")
        p.add_argument("-q", "--quiet", action="store_true")
        p.set_defaults(fn=fn)
    p = sub.add_parser("sync", help="compare a local and a remote folder and bring them in line")
    p.add_argument("source", help="a saved profile name, or a local folder")
    p.add_argument("remote", nargs="?", help="<site>:/path or a URL (when source is a folder)")
    p.add_argument("--direction", choices=["upload", "download", "both"])
    p.add_argument("--mirror", action="store_true", help="also delete what the source side doesn't have")
    p.add_argument("--size-only", action="store_true", help="compare sizes only, ignore timestamps")
    p.add_argument("--exclude", action="append", metavar="PATTERN", help="skip matching names/paths")
    p.add_argument("--dry-run", action="store_true", help="show the plan, change nothing")
    p.add_argument("--yes", action="store_true", help="allow deletes without asking")
    p.add_argument("--json", action="store_true", help="print the plan as JSON")
    p.add_argument("--limit", type=int, default=0, metavar="KB/s")
    p.add_argument("--verify", action="store_true", help="compare checksums after each file")
    p.add_argument("--checksum", action="store_true",
                   help="compare same-size files by content (slower; needs server-side hashing)")
    p.add_argument("-q", "--quiet", action="store_true")
    p.set_defaults(fn=cmd_sync)
    p = sub.add_parser("watch", help="upload every file that changes in a local folder")
    p.add_argument("local", help="local folder to watch")
    p.add_argument("remote", help="<site>:/path or a URL")
    p.add_argument("--ignore", action="append", metavar="PATTERN",
                   help="also skip names matching this (repeatable; .git, node_modules, … are skipped already)")
    p.add_argument("--interval", type=float, default=1.0, metavar="SECONDS", help="how often to look (default 1)")
    p.add_argument("--limit", type=int, default=0, metavar="KB/s")
    p.add_argument("--yes", action="store_true", help="don't ask for production sites")
    p.add_argument("-q", "--quiet", action="store_true")
    p.set_defaults(fn=cmd_watch)
    p = sub.add_parser("profiles", help="list saved sync profiles")
    p.set_defaults(fn=cmd_profiles)
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
