"""Full-screen terminal UI (Textual): `blamixfiles tui`. Works over SSH on a headless box.

Unlock the vault, pick a site, then browse this computer (left) and the server (right):
copy with F5, make folders, rename, delete. Transfers go through the same queue engine as the
app and the CLI (parallel, resume, overwrite questions). Sites are added or edited in the app.
"""
from __future__ import annotations

import collections
import os
import threading
import time

import paramiko
from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, DataTable, Footer, Header, Input, Label, Static

from . import keychain
from .core import engine as E
from .core import ssh
from .core.backends import open_backend
from .core.backends.ftp import UntrustedCertificate
from .core.backends.local import LocalBackend
from .core.errors import friendly
from .core.vfs import sort_entries
from .models import Site, Store
from .paths import vault_path
from .vault import Vault, VaultError, WrongPassword

ACCENT = "#7c8cff"


def human_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "K", "M", "G", "T"):
        if size < 1024 or unit == "T":
            return f"{int(size)}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return str(n)


def human_time(ts: float) -> str:
    if not ts:
        return ""
    t = time.localtime(ts)
    return time.strftime("%d %b %H:%M" if abs(time.time() - ts) < 180 * 86400 else "%d %b  %Y", t)


class Declined(Exception):
    """The user said no to a question (host key, certificate, password)."""


# ============================================================== modals
class _Modal(ModalScreen):
    DEFAULT_CSS = """
    _Modal { align: center middle; }
    _Modal > Vertical { width: 70; height: auto; padding: 1 2; background: $panel; border: round $accent; }
    _Modal .row { height: auto; margin-top: 1; align-horizontal: right; }
    _Modal .row Button { margin-left: 1; }
    _Modal Input { margin-top: 1; }
    """
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def action_cancel(self) -> None:
        self.dismiss(None)


class ConfirmScreen(_Modal):
    def __init__(self, message: str, yes: str = "Yes", danger: bool = False):
        super().__init__()
        self.message, self.yes, self.danger = message, yes, danger

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(self.message)
            with Horizontal(classes="row"):
                yield Button("Cancel", id="no")
                yield Button(self.yes, id="yes", variant="error" if self.danger else "primary")

    @on(Button.Pressed)
    def _pressed(self, ev: Button.Pressed) -> None:
        self.dismiss(ev.button.id == "yes")

    def action_cancel(self) -> None:
        self.dismiss(False)


class PromptScreen(_Modal):
    def __init__(self, title: str, value: str = "", password: bool = False, error: str = ""):
        super().__init__()
        self.title_text, self.value, self.password, self.error = title, value, password, error

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(self.title_text)
            if self.error:
                yield Label(self.error, classes="err")
            yield Input(self.value, password=self.password, id="text")

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    @on(Input.Submitted)
    def _submitted(self, ev: Input.Submitted) -> None:
        self.dismiss(ev.value)


class ChoiceScreen(_Modal):
    """'File exists': returns a policy name (or 'overwrite-all' …), or 'cancel'."""

    def __init__(self, name: str, src: str, dst: str):
        super().__init__()
        self.name_, self.src, self.dst = name, src, dst

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(f"[b]{self.name_}[/b] already exists.")
            yield Label(f"new: {self.src}\nexisting: {self.dst}")
            with Horizontal(classes="row"):
                yield Button("Overwrite", id="overwrite", variant="primary")
                yield Button("Overwrite all", id="overwrite-all")
                yield Button("Skip", id="skip")
                yield Button("Skip all", id="skip-all")
                yield Button("Cancel", id="cancel")

    @on(Button.Pressed)
    def _pressed(self, ev: Button.Pressed) -> None:
        self.dismiss(ev.button.id)

    def action_cancel(self) -> None:
        self.dismiss("cancel")


class UnlockScreen(Screen):
    DEFAULT_CSS = """
    UnlockScreen { align: center middle; }
    UnlockScreen > Vertical { width: 56; height: auto; padding: 1 2; border: round $accent; }
    UnlockScreen .err { color: $error; }
    """

    def __init__(self, create: bool):
        super().__init__()
        self.create = create

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("[b]BlamixFiles[/b]  " + ("create your vault" if self.create else "unlock"))
            yield Input(password=True, placeholder="Master password", id="pw")
            if self.create:
                yield Input(password=True, placeholder="Confirm password", id="pw2")
            yield Label("", id="err", classes="err")

    def on_mount(self) -> None:
        self.query_one("#pw").focus()

    @on(Input.Submitted)
    def _submitted(self, ev: Input.Submitted) -> None:
        pw = self.query_one("#pw", Input).value
        if self.create:
            if ev.input.id == "pw":
                self.query_one("#pw2").focus()
                return
            if len(pw) < 8:
                return self._fail("Use at least 8 characters.")
            if pw != self.query_one("#pw2", Input).value:
                return self._fail("Passwords don't match.")
        err = self.app.unlock(pw, self.create)
        if err:
            self._fail(err)
            self.query_one("#pw", Input).select_all()
            self.query_one("#pw").focus()

    def _fail(self, msg: str) -> None:
        self.query_one("#err", Label).update(msg)


# ============================================================== site list
class SitesScreen(Screen):
    BINDINGS = [
        Binding("enter", "connect", "Connect"),
        Binding("slash", "filter", "Filter"),
        Binding("u", "url", "Connect to URL"),
        Binding("q", "app.quit", "Quit"),
    ]
    DEFAULT_CSS = """
    SitesScreen #filter { margin: 0 1; }
    SitesScreen DataTable { height: 1fr; margin: 0 1; }
    """

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield Input(placeholder="Filter sites  (press / )", id="filter")
        yield DataTable(cursor_type="row", zebra_stripes=True)
        yield Static("", id="msg")
        yield Footer()

    def on_mount(self) -> None:
        self.title = "BlamixFiles"
        self.sub_title = "sites"
        t = self.query_one(DataTable)
        t.add_columns("Name", "Protocol", "Address", "Group")
        self.fill()
        t.focus()

    def fill(self, needle: str = "") -> None:
        t = self.query_one(DataTable)
        t.clear()
        needle = needle.lower()
        sites = sorted(self.app.store.sites.values(), key=lambda s: (s.group.lower(), s.label.lower()))
        for s in sites:
            if needle and needle not in f"{s.label} {s.address} {s.group}".lower():
                continue
            name = Text(s.label, style="bold red") if s.production else Text(s.label)
            t.add_row(name, s.protocol, s.address, s.group, key=s.id)

    @on(Input.Changed, "#filter")
    def _changed(self, ev: Input.Changed) -> None:
        self.fill(ev.value)

    @on(Input.Submitted, "#filter")
    def _filter_done(self) -> None:
        self.query_one(DataTable).focus()

    def action_filter(self) -> None:
        self.query_one("#filter").focus()

    def _selected(self) -> Site | None:
        t = self.query_one(DataTable)
        if not t.row_count:
            return None
        key = t.coordinate_to_cell_key(t.cursor_coordinate).row_key.value
        return self.app.store.sites.get(key)

    def action_connect(self) -> None:
        if isinstance(self.app.focused, Input):
            return
        site = self._selected()
        if site is not None:
            self.app.open_site(site.copy(), saved=site)

    @on(DataTable.RowSelected)
    def _row(self) -> None:
        self.action_connect()

    def action_url(self) -> None:
        def got(text: str | None) -> None:
            if not text:
                return
            try:
                site, path = Site.from_url(text.strip())
            except Exception as e:  # noqa: BLE001
                self.app.notify(f"Not a valid address: {e}", severity="error")
                return
            self.app.open_site(site, start=path)
        self.app.push_screen(PromptScreen("Address (sftp://user@host/path, ftp://…, s3://…)"), got)

    def show(self, msg: str) -> None:
        self.query_one("#msg", Static).update(msg)


# ============================================================== one pane
class FilePane(Vertical):
    DEFAULT_CSS = """
    FilePane { width: 1fr; border: round $surface; }
    FilePane:focus-within { border: round $accent; }
    FilePane .path { height: 1; padding: 0 1; background: $boost; }
    FilePane DataTable { height: 1fr; }
    """

    def __init__(self, title: str, backend, **kw):
        super().__init__(**kw)
        self.heading = title
        self.backend = backend
        self.lock = threading.Lock()          # a backend serves one operation at a time
        self.path = ""
        self.entries: dict[str, object] = {}
        self.marked: set[str] = set()
        self.show_hidden = True

    def compose(self) -> ComposeResult:
        yield Label(self.heading, classes="path")
        yield DataTable(cursor_type="row", zebra_stripes=True)

    def on_mount(self) -> None:
        t = self.query_one(DataTable)
        t.add_column("Name", width=None, key="name")
        t.add_column("Size", key="size")
        t.add_column("Modified", key="mod")
        t.add_column("Perms", key="perm")

    @property
    def table(self) -> DataTable:
        return self.query_one(DataTable)

    # ---- loading
    def load(self, path: str | None = None, select: str = "") -> None:
        """List `path` (default: the current or home folder) on a worker thread."""
        def work() -> None:
            try:
                with self.lock:
                    p = path if path is not None else (self.path or self.backend.home())
                    p = self.backend.normalize(p) if p else p
                    entries = sort_entries(self.backend.list(p))
            except Exception as e:  # noqa: BLE001
                self.app.call_from_thread(self.app.notify, friendly(e), severity="error")
                return
            self.app.call_from_thread(self._fill, p, entries, select)
        self.app.run_worker(work, thread=True, group=f"list-{id(self)}", exclusive=True)

    def _fill(self, path: str, entries: list, select: str = "") -> None:
        self.path = path
        self.marked.clear()
        self.query_one(".path", Label).update(f"{self.heading}  {path or 'This PC'}")
        self.entries = {}
        self._show(entries, select)

    def _show(self, entries: list, select: str = "") -> None:
        t = self.table
        t.clear()
        self._all = entries
        if self.path not in ("", "/"):
            up = self.backend.parent(self.path)
            t.add_row(Text("..", style=ACCENT), "", "", "", key="..")
            self.entries[".."] = up
        row_of = {}
        for e in entries:
            if not self.show_hidden and e.name.startswith("."):
                continue
            self.entries[e.path] = e
            style = f"bold {ACCENT}" if e.is_dir else ""
            name = Text(e.name + ("/" if e.is_dir else ""), style=style)
            t.add_row(name, "" if e.is_dir else human_size(e.size), human_time(e.mtime), e.perms, key=e.path)
            row_of[e.path] = t.row_count - 1
        if select and select in row_of:
            t.move_cursor(row=row_of[select])

    # ---- selection
    def current(self):
        t = self.table
        if not t.row_count:
            return None
        key = t.coordinate_to_cell_key(t.cursor_coordinate).row_key.value
        return key, self.entries.get(key)

    def selection(self) -> list:
        """Marked entries, or the one under the cursor ('..' never counts)."""
        if self.marked:
            return [self.entries[k] for k in self.marked if k in self.entries]
        cur = self.current()
        if cur and cur[0] != ".." and cur[1] is not None:
            return [cur[1]]
        return []

    def toggle_mark(self) -> None:
        cur = self.current()
        if not cur or cur[0] == "..":
            return
        key = cur[0]
        self.marked.symmetric_difference_update({key})
        t = self.table
        e = self.entries[key]
        name = Text(("● " if key in self.marked else "") + e.name + ("/" if e.is_dir else ""),
                    style=f"bold {ACCENT}" if e.is_dir else "")
        t.update_cell(key, "name", name)
        t.action_cursor_down()

    def enter(self) -> None:
        cur = self.current()
        if not cur:
            return
        key, e = cur
        if key == "..":
            self.go_up()
        elif getattr(e, "is_dir", False):
            self.load(e.path)

    def go_up(self) -> None:
        if self.path in ("", "/"):
            return
        old = self.path
        self.load(self.backend.parent(old), select=old)

    def toggle_hidden(self) -> None:
        self.show_hidden = not self.show_hidden
        self._show(getattr(self, "_all", []))


# ============================================================== browser
class BrowserScreen(Screen):
    BINDINGS = [
        Binding("tab", "switch", "Other pane", priority=True),
        Binding("enter", "open", "Open"),
        Binding("backspace", "up", "Up"),
        Binding("space", "mark", "Mark"),
        Binding("f5,c", "copy", "Copy →/←"),
        Binding("f7,m", "mkdir", "New folder"),
        Binding("f2,r", "rename", "Rename"),
        Binding("delete,d", "delete", "Delete"),
        Binding("ctrl+r", "refresh", "Refresh"),
        Binding("h", "hidden", "Hidden files"),
        Binding("x", "cancel_transfers", "Cancel transfers"),
        Binding("t", "retry", "Retry failed"),
        Binding("escape,q", "back", "Sites"),
    ]
    DEFAULT_CSS = """
    BrowserScreen #panes { height: 1fr; }
    BrowserScreen #status { height: 1; padding: 0 1; background: $boost; }
    """

    def __init__(self, site: Site, backend, start: str = "", saved: Site | None = None):
        super().__init__()
        self.site = site
        self.saved = saved
        self.start = start
        self.local = FilePane("This computer", LocalBackend(), id="local")
        self.remote = FilePane(site.label, backend, id="remote")
        self.engine: E.TransferEngine | None = None
        self.policy = "ask"
        self._events: collections.deque = collections.deque()

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Horizontal(id="panes"):
            yield self.local
            yield self.remote
        yield Static("No transfers", id="status")
        yield Footer()

    def on_mount(self) -> None:
        self.title = "BlamixFiles"
        self.sub_title = self.site.label
        start_local = self.app.settings_last_dir or os.path.expanduser("~")
        self.local.load(start_local)
        self.remote.load(self.start or None)
        self.engine = E.TransferEngine(lambda s: self.app.connect(s), workers=4, on_change=self._job_changed,
                                       policy=self.policy, ask=self._ask_overwrite)
        self.local.table.focus()
        self.set_interval(0.3, self._tick)

    # ---- helpers
    @property
    def active(self) -> FilePane:
        return self.remote if self.remote.has_focus_within else self.local

    @property
    def other(self) -> FilePane:
        return self.local if self.active is self.remote else self.remote

    def _status(self) -> None:
        jobs = [j for j in (self.engine.jobs if self.engine else []) if not j.is_dir]
        if not jobs:
            self.query_one("#status", Static).update("No transfers")
            return
        run = [j for j in jobs if j.status == E.RUNNING]
        queued = sum(j.status == E.QUEUED for j in jobs)
        done = sum(j.status in (E.DONE, E.SKIPPED) for j in jobs)
        failed = sum(j.status == E.FAILED for j in jobs)
        parts = []
        if run:
            j = run[0]
            pct = f" {j.done * 100 // j.size}%" if j.size else ""
            parts.append(f"▶ {j.name}{pct}  {human_size(int(j.speed))}/s")
        if queued:
            parts.append(f"{queued} queued")
        parts.append(f"{done}/{len(jobs)} done")
        if failed:
            parts.append(f"[red]{failed} failed[/red] (t = retry)")
        self.query_one("#status", Static).update("   ".join(parts))

    # ---- transfers
    # The engine calls on_change from its worker threads, sometimes holding its own lock, so this
    # only records the event; a timer on the UI thread does the real work (no cross-thread waits).
    def _job_changed(self, job: E.Job) -> None:
        if job.status in (E.DONE, E.FAILED):
            self._events.append(job)

    def _tick(self) -> None:
        refresh: set[int] = set()
        while self._events:
            job = self._events.popleft()
            if job.status == E.FAILED:
                self.app.notify(f"{job.name}: {job.error}", severity="error")
            elif not job.is_dir:
                refresh.add(0 if job.kind == "upload" else 1)       # 0 = server pane, 1 = this computer
        if self.engine is not None:
            self._status()
            if refresh and not self.engine.pending():
                for which in refresh:
                    pane = self.remote if which == 0 else self.local
                    pane.load(pane.path)

    def _ask_overwrite(self, job: E.Job, existing) -> str:
        src = f"{human_size(job.size)}  {human_time(job.mtime)}"
        dst = f"{human_size(existing.size)}  {human_time(existing.mtime)}"
        r = self.app.ask_modal(ChoiceScreen(os.path.basename(job.dst.rstrip("/\\")), src, dst))
        return r or "cancel"

    def action_copy(self) -> None:
        src, dst = self.active, self.other
        items = src.selection()
        if not items:
            return
        if not dst.path and src is self.remote:
            self.app.notify("Open a folder on this computer first.", severity="warning")
            return
        if self.site.production and src is self.local:
            self.app.push_screen(ConfirmScreen(f"{self.site.label} is a PRODUCTION site. Upload {len(items)} item(s) "
                                               f"to {dst.path}?", "Upload", danger=True),
                                 lambda ok: ok and self._queue(src, dst, items))
            return
        self._queue(src, dst, items)

    def _queue(self, src: FilePane, dst: FilePane, items: list) -> None:
        for e in items:
            try:
                if src is self.local:
                    self.engine.upload(self.site, e.path, dst.path, self.remote.backend.join)
                else:
                    self.engine.download(self.site, e, dst.path)
            except OSError as ex:
                self.app.notify(str(ex), severity="error")
        src.marked.clear()
        self.app.notify(f"Queued {len(items)} item(s)")
        self._status()

    def action_cancel_transfers(self) -> None:
        if self.engine:
            self.engine.cancel()

    def action_retry(self) -> None:
        if self.engine:
            self.engine.retry()

    # ---- file actions
    def action_switch(self) -> None:
        self.other.table.focus()

    def action_open(self) -> None:
        self.active.enter()

    @on(DataTable.RowSelected)
    def _row_selected(self, ev: DataTable.RowSelected) -> None:
        pane = ev.control.parent
        if isinstance(pane, FilePane):
            pane.enter()

    def action_up(self) -> None:
        self.active.go_up()

    def action_mark(self) -> None:
        self.active.toggle_mark()

    def action_hidden(self) -> None:
        self.local.toggle_hidden()
        self.remote.toggle_hidden()

    def action_refresh(self) -> None:
        self.local.load(self.local.path)
        self.remote.load(self.remote.path)

    def _run(self, pane: FilePane, fn, done_msg: str, select: str = "") -> None:
        def work() -> None:
            try:
                with pane.lock:
                    fn(pane.backend)
            except Exception as e:  # noqa: BLE001
                self.app.call_from_thread(self.app.notify, friendly(e), severity="error")
                return
            self.app.call_from_thread(self.app.notify, done_msg)
            self.app.call_from_thread(pane.load, pane.path, select)
        self.app.run_worker(work, thread=True)

    def action_mkdir(self) -> None:
        pane = self.active

        def got(name: str | None) -> None:
            if name and name.strip():
                path = pane.backend.join(pane.path, name.strip())
                self._run(pane, lambda b: b.mkdir(path), f"Created {name.strip()}", select=path)
        self.app.push_screen(PromptScreen("New folder name"), got)

    def action_rename(self) -> None:
        pane = self.active
        cur = pane.current()
        if not cur or cur[0] == ".." or cur[1] is None:
            return
        e = cur[1]

        def got(name: str | None) -> None:
            if name and name.strip() and name.strip() != e.name:
                dst = pane.backend.join(pane.backend.parent(e.path), name.strip())
                self._run(pane, lambda b: b.rename(e.path, dst), f"Renamed to {name.strip()}", select=dst)
        self.app.push_screen(PromptScreen("Rename", e.name), got)

    def action_delete(self) -> None:
        pane = self.active
        items = pane.selection()
        if not items:
            return
        what = items[0].name if len(items) == 1 else f"{len(items)} items"
        where = "the server" if pane is self.remote else "this computer"

        def got(ok: bool | None) -> None:
            if not ok:
                return

            def rm(b) -> None:
                for e in items:
                    if e.is_dir and not e.is_link:
                        b.remove_tree(e.path)
                    else:
                        b.remove(e.path)
            self._run(pane, rm, f"Deleted {what}")
        self.app.push_screen(ConfirmScreen(f"Delete [b]{what}[/b] from {where}?", "Delete", danger=True), got)

    def action_back(self) -> None:
        busy = self.engine.pending() if self.engine else 0
        if busy:
            self.app.push_screen(ConfirmScreen(f"{busy} transfer(s) not finished. Stop them and go back?", "Stop"),
                                 lambda ok: ok and self._leave())
        else:
            self._leave()

    def _leave(self) -> None:
        self.app.settings_last_dir = self.local.path
        if self.engine:
            self.engine.cancel()
            self.engine.shutdown()
        for pane in (self.remote,):
            try:
                pane.backend.close()
            except Exception:
                pass
        self.app.pop_screen()


# ============================================================== app
class BlamixFilesTUI(App):
    TITLE = "BlamixFiles"
    BINDINGS = [Binding("ctrl+q", "quit", "Quit", show=False)]
    CSS = "Screen { background: $surface; }"

    def __init__(self, store: Store | None = None):
        super().__init__()
        self.store = store
        self.settings_last_dir = ""

    # ---- start / unlock
    def on_mount(self) -> None:
        if self.store is not None:
            self.push_screen(SitesScreen())
            return
        path = vault_path()
        create = not Vault.exists(path)
        pw = os.environ.get("BLAMIXFILES_VAULT_PASSWORD")
        if not create and not pw and keychain.available():
            pw = keychain.load(keychain.account_for(path))
        if pw and not create and not self.unlock(pw, False):
            return
        if self.store is None:
            self.push_screen(UnlockScreen(create))

    def unlock(self, pw: str, create: bool) -> str:
        """'' on success (and the site list opens), else the error to show."""
        path = vault_path()
        try:
            if create:
                self.store = Store(Vault.create(path, pw), {})
            else:
                v, data = Vault.open(path, pw)
                self.store = Store(v, data)
        except WrongPassword:
            return "Wrong master password."
        except VaultError as e:
            return str(e)
        except Exception as e:  # noqa: BLE001
            return f"Could not open the vault: {e}"
        ssh.set_site_resolver(lambda sid: self.store.sites.get(sid))      # jump hosts
        if isinstance(self.screen, UnlockScreen):
            self.pop_screen()
        self.push_screen(SitesScreen())
        return ""

    # ---- questions from worker threads
    def ask_modal(self, screen: ModalScreen):
        """Show a modal from a worker thread and wait for its answer."""
        done, box = threading.Event(), []

        def show() -> None:
            self.push_screen(screen, lambda r: (box.append(r), done.set()))
        self.call_from_thread(show)
        done.wait()
        return box[0]

    # ---- connecting
    def connect(self, site: Site):
        """open_backend with the questions a login can raise. Call from a worker thread."""
        for _ in range(4):
            try:
                return open_backend(site)
            except ssh.UnknownHostKey as e:
                msg = (f"First connection to [b]{e.host_id}[/b]\n{e.key.get_name()} {ssh.fingerprint(e.key)}\n\n"
                       "Trust this server?")
                if not self.ask_modal(ConfirmScreen(msg, "Trust")):
                    raise Declined("Host key not trusted") from None
                ssh.trust_host_key(e.host_id, e.key)
            except ssh.ChangedHostKey as e:
                raise Declined(f"HOST KEY CHANGED for {e.host_id}: refusing to connect. If the server was "
                               "reinstalled, accept the new key in the app first.") from None
            except UntrustedCertificate as e:
                msg = f"Untrusted certificate for [b]{e.host}[/b] ({e.reason})\nSHA-256 {e.fingerprint}\n\nTrust it?"
                if not self.ask_modal(ConfirmScreen(msg, "Trust")):
                    raise Declined("Certificate not trusted") from None
                site.tls_pinned = e.fingerprint
                saved = self.store.sites.get(site.id) if self.store else None
                if saved is not None:
                    saved.tls_pinned = e.fingerprint
                    self.store.save()
            except paramiko.AuthenticationException:
                if site.auth == "ask" and not site.password:
                    pw = self.ask_modal(PromptScreen(f"Password for {site.username}@{site.host}", password=True))
                    if pw is None:
                        raise Declined("No password given") from None
                    site.password = pw
                    continue
                raise
        raise Declined("Could not connect")

    def open_site(self, site: Site, saved: Site | None = None, start: str = "") -> None:
        sites = self.screen if isinstance(self.screen, SitesScreen) else None
        if sites:
            sites.show(f"Connecting to {site.label} …")

        def work() -> None:
            try:
                if site.auth == "ask" and not site.password and not site.is_ssh:
                    pw = self.ask_modal(PromptScreen(f"Password for {site.username}@{site.host}", password=True))
                    if pw is None:
                        raise Declined("No password given")
                    site.password = pw
                backend = self.connect(site)
            except Declined as e:
                self.call_from_thread(self._failed, sites, str(e))
                return
            except Exception as e:  # noqa: BLE001
                self.call_from_thread(self._failed, sites, friendly(e))
                return
            self.call_from_thread(self._connected, sites, site, backend, start, saved)
        self.run_worker(work, thread=True)

    def _failed(self, sites, msg: str) -> None:
        if sites:
            sites.show("")
        self.notify(msg, severity="error", timeout=8)

    def _connected(self, sites, site, backend, start: str, saved) -> None:
        if sites:
            sites.show("")
        self.push_screen(BrowserScreen(site, backend, start=start, saved=saved))


def main() -> None:
    BlamixFilesTUI().run()


if __name__ == "__main__":
    main()
