"""Command line: scriptable transfers with saved sites or URLs.

  blamixfiles sites
  blamixfiles ls   <site>:/path | sftp://user@host/path
  blamixfiles get  <site>:/remote/file-or-folder  ./local-folder
  blamixfiles put  ./local-file-or-folder  <site>:/remote/folder   [--limit 512]
  blamixfiles sync   ./dist mysite:/var/www --mirror --dry-run
  blamixfiles sync   "Deploy web"            (a profile saved in the app)
  blamixfiles profiles
  blamixfiles import filezilla|winscp|blamixshell [file]

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

from . import __version__, keychain
from .core import engine as E
from .core import ssh
from .core.backends import open_backend
from .core.backends.ftp import UntrustedCertificate
from .core.errors import friendly
from .models import Site, Store
from .paths import transfer_log_path, vault_path
from .vault import Vault, WrongPassword


class UsageError(Exception):
    pass


def _store() -> Store:
    path = vault_path()
    if not Vault.exists(path):
        raise UsageError("No vault yet: open the BlamixFiles app once to create it, or use a URL.")
    pw = os.environ.get("BLAMIXFILES_VAULT_PASSWORD")
    if not pw:
        pw = keychain.load(keychain.account_for(path))     # remembered by the app, if enabled
        if pw:
            try:
                v, data = Vault.open(path, pw)
                return Store(v, data)
            except WrongPassword:
                pass                                        # stale: ask instead
        pw = getpass.getpass("Master password: ")
    try:
        v, data = Vault.open(path, pw)
    except WrongPassword:
        raise UsageError("Wrong master password") from None
    return Store(v, data)


_store_cache: list[Store] = []


def store() -> Store:
    if not _store_cache:
        st = _store()
        st.keep_tokens()
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


def cmd_tui(a) -> int:
    try:
        from .tui import BlamixFilesTUI
    except ImportError:
        raise UsageError("The terminal UI needs Textual: pip install \"blamixfiles[tui]\"") from None
    BlamixFilesTUI().run()
    return 0


def _human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return str(n)


def cmd_find(a) -> int:
    from .core import search as F
    try:
        q = F.Query(name=a.name or "", case_sensitive=a.case_sensitive,
                    min_size=F.parse_size(a.larger) if a.larger else 0,
                    max_size=F.parse_size(a.smaller) if a.smaller else 0,
                    newer_than=time.time() - F.parse_age(a.newer) if a.newer else 0,
                    older_than=time.time() - F.parse_age(a.older) if a.older else 0,
                    kind=a.type, max_depth=a.depth)
    except ValueError as e:
        raise UsageError(str(e)) from None
    site, path = resolve(a.target)
    b = connect(site)
    try:
        hits = list(F.find(b, path or b.home(), q, limit=a.limit))
    finally:
        b.close()
    if a.json:
        print(json.dumps([{"path": e.path, "dir": e.is_dir, "size": e.size, "mtime": e.mtime} for e in hits], indent=2))
    else:
        for e in hits:
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(e.mtime)) if e.mtime else " " * 16
            print(f"{'' if e.is_dir else _human(e.size):>10}  {when}  {e.path}{'/' if e.is_dir else ''}")
    return 0 if hits else 1


def cmd_du(a) -> int:
    from .core import search as F
    site, path = resolve(a.target)
    b = connect(site)
    try:
        u = F.folder_size(b, path or b.home())
    finally:
        b.close()
    if a.json:
        print(json.dumps({"bytes": u.bytes, "files": u.files, "folders": u.folders, "unreadable": u.unreadable}))
    else:
        print(f"{_human(u.bytes)}  ({u.bytes} bytes) in {u.files} file(s), {u.folders} folder(s)"
              + (f"; {u.unreadable} folder(s) couldn't be read" if u.unreadable else ""))
    return 0


def cmd_keygen(a) -> int:
    from .core import keys as K
    path = a.path or str(K.default_key_path())
    pw = ""
    if not a.no_passphrase and sys.stdin.isatty():
        pw = getpass.getpass("Passphrase for the new key (empty = none): ")
        if pw and getpass.getpass("Again: ") != pw:
            raise UsageError("The passphrases don't match")
    try:
        line = K.generate(path, pw, a.comment or "")
    except K.KeyError_ as e:
        raise UsageError(str(e)) from None
    print(f"Created {path} and {path}.pub")
    print(line)
    return 0


def cmd_copy_id(a) -> int:
    from .core import keys as K
    site, _ = resolve(a.target if ":" in a.target or "://" in a.target else a.target + ":")
    if not site.is_ssh:
        raise UsageError(f"{site.label} isn't an SSH site")
    pub = a.key or (site.key_path + ".pub" if site.key_path else str(K.default_key_path()) + ".pub")
    try:
        line = K.read_public_key(pub)
    except (OSError, K.KeyError_) as e:
        raise UsageError(str(e)) from None
    b = connect(site)
    try:
        added = K.install(b, line)
    except K.KeyError_ as e:
        raise UsageError(str(e)) from None
    finally:
        b.close()
    print(f"{'Added' if added else 'Already there:'} {pub} → {site.label}:~/.ssh/authorized_keys")
    return 0


def cmd_schedule(a) -> int:
    from .core import schedule as SC
    if not SC.available():
        raise UsageError("No scheduler found (schtasks on Windows, crontab elsewhere)")
    if a.list or not a.profile:
        rows = SC.listing()
        for name, what in sorted(rows.items()):
            print(f"{name:30} {what}")
        if not rows:
            print("No scheduled syncs.")
        return 0
    if store().find_profile(a.profile) is None:
        raise UsageError(f"No sync profile named '{a.profile}' (see: blamixfiles profiles)")
    if a.off:
        print("Removed." if SC.remove(a.profile) else "It wasn't scheduled.")
        return 0
    try:
        when = SC.When.parse(a.daily or "", a.every or "")
    except ValueError as e:
        raise UsageError(str(e)) from None
    if not keychain.load(keychain.account_for(vault_path())):
        print("Note: a scheduled sync opens the vault with the password saved in the OS keychain. Switch on "
              "File > Unlock with … in the app (or it will fail to start).", file=sys.stderr)
    SC.install(a.profile, when)
    print(f"'{a.profile}' will sync {when.describe()}. Results: blamixfiles log")
    return 0


def cmd_login(a) -> int:
    import json as _json

    from .core import oauth
    site = store().find(a.site)
    if site is None:
        raise UsageError(f"No saved site named '{a.site}' (add it in the app: Google Drive, Dropbox or OneDrive)")
    if not site.is_cloud:
        raise UsageError(f"{site.label} doesn't use a browser sign-in")
    try:
        cid, secret = oauth.client_for(site)
        print("Opening the sign-in page in your browser … (finish there; waiting up to 5 minutes)", file=sys.stderr)
        token = oauth.authorize(site.protocol, cid, secret,
                                open_browser=lambda url: (print(url, file=sys.stderr), __import__("webbrowser").open(url)))
    except oauth.OAuthError as e:
        raise UsageError(str(e)) from None
    site.oauth_token = _json.dumps(token)
    store().save()
    print(f"Signed in. {site.label} is ready.")
    return 0


def cmd_diagnostics(a) -> int:
    from . import diagnostics
    from .settings import Settings
    print(diagnostics.report(Settings().data), end="")
    return 0


def cmd_log(a) -> int:
    lines = E.read_log(transfer_log_path(), a.lines)
    if a.json:
        keys = ["time", "status", "direction", "site", "source", "destination", "bytes", "seconds", "error"]
        print(json.dumps([dict(zip(keys, ln.split("\t"))) for ln in lines], indent=2))
    else:
        for ln in lines:
            print(ln.replace("\t", "  "))
    return 0


def cmd_mkdir(a) -> int:
    site, path = resolve(a.target)
    if not path or path == "/":
        raise UsageError("Give the folder to create, e.g. mysite:/var/www/new")
    b = connect(site)
    try:
        b.makedirs(path)
    finally:
        b.close()
    return 0


def cmd_rm(a) -> int:
    site, path = resolve(a.target)
    if not path or path.rstrip("/") == "":
        raise UsageError("Refusing to delete the root folder")
    b = connect(site)
    try:
        entry = b.stat(path)
        if entry is None:
            raise UsageError(f"Not found on the server: {path}")
        if entry.is_dir and not entry.is_link and not a.recursive:
            raise UsageError(f"{path} is a folder: add -r to delete it and everything in it")
        if (site.production or entry.is_dir) and not a.yes:
            if not sys.stdin.isatty():
                raise UsageError("Add --yes to delete from a script")
            if not _yes(f"Delete {path} on {site.label}?"):
                return 1
        if entry.is_dir and not entry.is_link:
            b.remove_tree(path)
        else:
            b.remove(path)
    finally:
        b.close()
    return 0


def cmd_mv(a) -> int:
    site, src = resolve(a.source)
    dst = a.dest
    if "://" in dst or (":" in dst and not dst.startswith("/")):
        site2, dst = resolve(dst)
        if site2.id != site.id and site2.address != site.address:
            raise UsageError("mv works inside one server: use get and put to move between servers")
    if not src or not dst.startswith("/"):
        raise UsageError("Give absolute paths, e.g. mysite:/old/name /new/name")
    b = connect(site)
    try:
        if b.stat(src) is None:
            raise UsageError(f"Not found on the server: {src}")
        b.makedirs(b.parent(dst))
        b.rename(src, dst)
    finally:
        b.close()
    return 0


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


def _run(jobs_fn, policy: str, quiet: bool, limit_kb: int = 0, verify: bool = False,
         as_json: bool = False, extra: dict | None = None, on_done=None) -> int:
    """Run queued transfers. With as_json the only output is one JSON document on stdout."""
    quiet = quiet or as_json

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
                           limit_up=limit_kb * 1024, limit_down=limit_kb * 1024, verify=verify,
                           log_path=transfer_log_path())
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
    skipped = [j for j in eng.jobs if j.status == E.SKIPPED and not j.is_dir]
    total = sum(j.size for j in done)
    if on_done is not None:
        hooks = on_done({"status": "failed" if failed else "ok", "files": len(done), "bytes": total,
                         "failed": len(failed)})
        if hooks and not as_json:
            for h in hooks:
                print(f"· {h}", file=sys.stderr)
        if hooks:
            extra = dict(extra or {}, hooks=hooks)
    if as_json:
        doc = dict(extra or {})
        doc["result"] = {"done": len(done), "skipped": len(skipped), "bytes": total,
                         "failed": [{"path": j.src, "error": j.error} for j in failed]}
        print(json.dumps(doc, indent=2))
    elif not quiet:
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
    return _run(lambda eng: eng.download(site, entry, a.dest), a.policy, a.quiet, a.limit, a.verify, a.json)


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
    return _run(queue, a.policy, a.quiet, a.limit, a.verify, a.json)


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
                           limit_up=a.limit * 1024, log_path=transfer_log_path())
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
    from . import importers as I
    notes: list[str] = []
    if a.source == "filezilla":
        path = a.file or str(I.filezilla_default_path())
        if not os.path.exists(path):
            raise UsageError(f"Not found: {path}")
        sites = I.import_filezilla(path)
    elif a.source == "winscp":
        path = a.file or I.winscp_default_source()
        if not path:
            raise UsageError("WinSCP.ini not found: pass its path")
        if path != "registry" and not os.path.exists(path):
            raise UsageError(f"Not found: {path}")
        try:
            sites, notes = I.import_winscp(path)
        except I.ImportError_ as e:
            raise UsageError(str(e)) from None
        path = "the Windows registry" if path == "registry" else path
    else:
        found = I.blamixshell_default_path()
        path = a.file or (str(found) if found else "")
        if not path or not os.path.exists(path):
            raise UsageError("BlamixShell vault not found: pass the path to vault.sdv")
        pw = os.environ.get("BLAMIXSHELL_VAULT_PASSWORD")
        if pw is None:
            if not sys.stdin.isatty():
                raise UsageError("Set BLAMIXSHELL_VAULT_PASSWORD (no terminal to ask for it)")
            pw = getpass.getpass("BlamixShell master password: ")
        try:
            sites, notes = I.import_blamixshell(path, pw)
        except I.ImportError_ as e:
            raise UsageError(str(e)) from None
    added = store().import_sites(sites)
    print(f"Imported {added} new site(s) from {path} ({len(sites) - added} already there).")
    for n in notes:
        print(f"  note: {n}")
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
        hooks = prof if (prof.after_command or prof.webhook_url) else None
    else:
        site, remote_root = resolve(a.remote)
        local_root = a.source
        hooks = None
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
        doc = {"summary": plan.summary(), "actions": [
            {"action": x.kind, "path": x.rel, "dir": x.is_dir, "reason": x.reason, "bytes": x.size}
            for x in plan.actions]}
        if a.json and (a.dry_run or not any(x.kind != S.CONFLICT for x in plan.actions)):
            print(json.dumps(doc, indent=2))
            return 0
        if a.json:
            pass                                    # printed once, with the result, after the run
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
        on_done = None
        if hooks is not None:
            def on_done(result):
                result.update(site=site.label, local=os.path.abspath(local_root), remote=remote_root)
                return S.run_hooks(hooks.name, hooks.after_command, hooks.webhook_url, result)
        return _run(lambda eng: S.apply(plan, site, eng, b, LocalBackend()), "overwrite", a.quiet, a.limit,
                    a.verify, a.json, extra=doc, on_done=on_done)
    finally:
        b.close()


# the sub-commands (the packaged app hands these to the command line instead of opening a window)
COMMANDS = {"tui", "sites", "ls", "get", "put", "find", "du", "mkdir", "rm", "mv", "keygen", "copy-id", "sync",
            "schedule", "watch", "profiles", "import", "log", "diagnostics", "login"}


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="blamixfiles", description="BlamixFiles command line")
    ap.add_argument("--version", action="version", version=f"BlamixFiles {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("tui", help="full-screen terminal UI (browse, copy, rename, delete)")
    p.set_defaults(fn=cmd_tui)
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
        p.add_argument("--json", action="store_true", help="print the result as one JSON document")
        p.set_defaults(fn=fn)
    p = sub.add_parser("find", help="search a server folder (and below) by name, size or age")
    p.add_argument("target", help="<site>:/path or a URL")
    p.add_argument("--name", help='a wildcard pattern ("*.log") or part of the name')
    p.add_argument("--case-sensitive", action="store_true")
    p.add_argument("--larger", metavar="SIZE", help="at least this big, e.g. 10M")
    p.add_argument("--smaller", metavar="SIZE", help="at most this big, e.g. 500k")
    p.add_argument("--newer", metavar="AGE", help="changed within, e.g. 12h, 7d")
    p.add_argument("--older", metavar="AGE", help="not changed for, e.g. 30d")
    p.add_argument("--type", choices=["any", "file", "folder"], default="any")
    p.add_argument("--depth", type=int, default=0, help="how many folder levels down (0 = all)")
    p.add_argument("--limit", type=int, default=10_000)
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_find)
    p = sub.add_parser("du", help="total size of a server folder")
    p.add_argument("target", help="<site>:/path or a URL")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_du)
    p = sub.add_parser("keygen", help="make a new SSH key pair (Ed25519)")
    p.add_argument("path", nargs="?", help="private key file (default ~/.ssh/id_ed25519_blamixfiles)")
    p.add_argument("--comment", help="text at the end of the public key")
    p.add_argument("--no-passphrase", action="store_true", help="don't ask for a passphrase")
    p.set_defaults(fn=cmd_keygen)
    p = sub.add_parser("copy-id", help="add your public key to a server's authorized_keys")
    p.add_argument("target", help="a saved SSH site (or <site>:, or a URL)")
    p.add_argument("--key", help="the .pub file (default: the site's key + .pub, or the BlamixFiles key)")
    p.set_defaults(fn=cmd_copy_id)
    p = sub.add_parser("schedule", help="run a sync profile on a schedule (Task Scheduler / cron)")
    p.add_argument("profile", nargs="?")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--daily", metavar="HH:MM", help="every day at this time")
    g.add_argument("--every", metavar="HOURS", help="every N hours (1-23)")
    g.add_argument("--off", action="store_true", help="stop running it")
    g.add_argument("--list", action="store_true", help="show the scheduled profiles")
    p.set_defaults(fn=cmd_schedule)
    p = sub.add_parser("login", help="sign in to a Google Drive / Dropbox / OneDrive site (opens the browser)")
    p.add_argument("site")
    p.set_defaults(fn=cmd_login)
    p = sub.add_parser("diagnostics", help="print versions, settings and recent transfers for a problem report")
    p.set_defaults(fn=cmd_diagnostics)
    p = sub.add_parser("log", help="show the transfer log (one line per finished file)")
    p.add_argument("-n", "--lines", type=int, default=50, help="how many of the latest lines (default 50)")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_log)
    p = sub.add_parser("mkdir", help="create a folder on the server (with its parents)")
    p.add_argument("target", help="<site>:/path or a URL")
    p.set_defaults(fn=cmd_mkdir)
    p = sub.add_parser("rm", help="delete a file or folder on the server")
    p.add_argument("target", help="<site>:/path or a URL")
    p.add_argument("-r", "--recursive", action="store_true", help="needed for folders")
    p.add_argument("--yes", action="store_true", help="don't ask (needed in scripts)")
    p.set_defaults(fn=cmd_rm)
    p = sub.add_parser("mv", help="rename or move a file or folder on the server")
    p.add_argument("source", help="<site>:/path or a URL")
    p.add_argument("dest", help="the new path on the same server (/new/path, or <site>:/new/path)")
    p.set_defaults(fn=cmd_mv)
    p = sub.add_parser("sync", help="compare a local and a remote folder and bring them in line")
    p.add_argument("source", help="a saved profile name, or a local folder")
    p.add_argument("remote", nargs="?", help="<site>:/path or a URL (when source is a folder)")
    p.add_argument("--direction", choices=["upload", "download", "both"])
    p.add_argument("--mirror", action="store_true", help="also delete what the source side doesn't have")
    p.add_argument("--size-only", action="store_true", help="compare sizes only, ignore timestamps")
    p.add_argument("--exclude", action="append", metavar="PATTERN", help="skip matching names/paths")
    p.add_argument("--dry-run", action="store_true", help="show the plan, change nothing")
    p.add_argument("--yes", action="store_true", help="allow deletes without asking")
    p.add_argument("--json", action="store_true", help="print the plan (and, when it runs, the result) as JSON")
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
    p.add_argument("source", choices=["filezilla", "winscp", "blamixshell"])
    p.add_argument("file", nargs="?", help='the file to read (default: where that app keeps it); '
                   'for winscp also "registry"')
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
