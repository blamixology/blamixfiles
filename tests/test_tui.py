import asyncio
import time

import pytest

pytest.importorskip("textual")

from servers import PASSWORD, USER, FTPTestServer  # noqa: E402

from blamixfiles import tui as T  # noqa: E402
from blamixfiles.models import Site, open_or_create  # noqa: E402
from blamixfiles.paths import vault_path  # noqa: E402


async def until(cond, timeout: float = 15.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("timed out waiting for the UI")


def test_tui_browse_copy_mkdir_delete(tmp_path):
    srv_root, loc = tmp_path / "srv", tmp_path / "loc"
    (srv_root / "dir").mkdir(parents=True)
    (srv_root / "a.txt").write_text("from server")
    loc.mkdir()
    (loc / "up.txt").write_text("from here")

    with FTPTestServer(srv_root) as srv:
        site = Site(name="ftp", protocol="ftp", host="127.0.0.1", port=srv.port, username=USER, password=PASSWORD)
        store = open_or_create(vault_path(), "master", create=True)
        store.upsert(site)

        async def go():
            app = T.BlamixFilesTUI(store)
            async with app.run_test(size=(150, 40)) as pilot:
                await until(lambda: isinstance(app.screen, T.SitesScreen) and app.screen.query_one(T.DataTable).row_count == 1)
                await pilot.press("enter")                                    # connect
                await until(lambda: isinstance(app.screen, T.BrowserScreen) and "/a.txt" in app.screen.remote.entries)
                scr = app.screen
                scr.local.load(str(loc))
                await until(lambda: scr.local.path == str(loc) and any(k.endswith("up.txt") for k in scr.local.entries))

                # upload: cursor on up.txt (row 0 is ".."), F5
                scr.local.table.focus()
                await pilot.press("down", "f5")
                await until(lambda: (srv_root / "up.txt").exists() and not scr.engine.pending())
                assert (srv_root / "up.txt").read_text() == "from here"

                # the finished upload refreshes the server pane: wait for that before moving the cursor
                await until(lambda: "/up.txt" in scr.remote.entries)

                # download: switch to the server pane, cursor on a.txt (after dir/), F5
                await pilot.press("tab")
                await until(lambda: scr.remote.has_focus_within)
                names = [e.name for e in T.sort_entries([e for k, e in scr.remote.entries.items() if k != ".."])]
                await pilot.press(*(["down"] * names.index("a.txt")), "f5")
                await until(lambda: (loc / "a.txt").exists() and not scr.engine.pending())
                assert (loc / "a.txt").read_text() == "from server"

                # new folder on the server
                await pilot.press("m")
                await pilot.press(*"newdir", "enter")
                await until(lambda: (srv_root / "newdir").is_dir())

                # delete the uploaded file on the server (confirm with the Delete button)
                await until(lambda: "/up.txt" in scr.remote.entries)
                names = [e.name for e in T.sort_entries([e for k, e in scr.remote.entries.items() if k != ".."])]
                scr.remote.table.move_cursor(row=names.index("up.txt"))
                await pilot.press("d")
                await until(lambda: isinstance(app.screen, T.ConfirmScreen))
                await pilot.click("#yes")
                await until(lambda: not (srv_root / "up.txt").exists())

                await pilot.press("escape")                                   # back to the site list
                await until(lambda: isinstance(app.screen, T.SitesScreen))
        asyncio.run(go())


def test_tui_unlock_wrong_then_right(tmp_path, monkeypatch):
    open_or_create(vault_path(), "master", create=True).upsert(Site(name="x", protocol="ftp", host="h"))
    monkeypatch.delenv("BLAMIXFILES_VAULT_PASSWORD", raising=False)
    monkeypatch.setenv("BLAMIXFILES_NO_KEYCHAIN", "1")
    monkeypatch.setattr(T.keychain, "available", lambda: False)

    async def go():
        app = T.BlamixFilesTUI()
        async with app.run_test(size=(100, 30)) as pilot:
            await until(lambda: isinstance(app.screen, T.UnlockScreen))
            await pilot.press(*"wrongpass", "enter")
            await until(lambda: "Wrong" in str(app.screen.query_one("#err").render()))
            await pilot.press("ctrl+a", "backspace") if False else None
            app.screen.query_one("#pw").value = ""
            await pilot.press(*"master", "enter")
            await until(lambda: isinstance(app.screen, T.SitesScreen))
    asyncio.run(go())
