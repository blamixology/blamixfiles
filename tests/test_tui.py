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


def _kinds(app):
    return [type(s).__name__ for s in app.screen_stack]


def test_tui_site_add_edit_delete(tmp_path):
    store = open_or_create(vault_path(), "master", create=True)

    async def go():
        app = T.BlamixFilesTUI(store)
        async with app.run_test(size=(150, 40)) as pilot:
            await until(lambda: isinstance(app.screen, T.SitesScreen))
            await pilot.press("n")
            await until(lambda: isinstance(app.screen, T.SiteScreen))
            app.screen.query_one("#name").value = "Shop"
            app.screen.query_one("#host").value = "ftp.example.com"
            app.screen.query_one("#username").value = "me"
            app.screen.query_one("#password").value = "secret"
            app.screen.query_one("#port").value = "abc"
            await pilot.click("#save")                                    # bad port: stays open
            await until(lambda: "number" in str(app.screen.query_one("#err").render()))
            app.screen.query_one("#port").value = "2121"
            app.screen.query_one("#protocol").value = "ftp"
            await pilot.pause()
            app.screen.query_one("#save").press()
            await until(lambda: isinstance(app.screen, T.SitesScreen))
            (site,) = store.sites.values()
            assert (site.name, site.host, site.port, site.protocol, site.auth) == ("Shop", "ftp.example.com", 2121, "ftp", "password")
            assert app.screen.query_one(T.DataTable).row_count == 1

            await pilot.press("e")
            await until(lambda: isinstance(app.screen, T.SiteScreen))
            app.screen.query_one("#group").value = "Work"
            await pilot.click("#save")
            await until(lambda: isinstance(app.screen, T.SitesScreen))
            assert next(iter(store.sites.values())).group == "Work" and len(store.sites) == 1

            await pilot.press("d")
            await until(lambda: isinstance(app.screen, T.ConfirmScreen))
            await pilot.click("#yes")
            await until(lambda: not store.sites)
    asyncio.run(go())


def test_tui_view_and_edit_file(tmp_path, monkeypatch):
    import contextlib
    srv_root = tmp_path / "srv"
    srv_root.mkdir()
    (srv_root / "note.txt").write_text("line one\n")
    (srv_root / "blob.bin").write_bytes(b"\x00\x01\x02")
    with FTPTestServer(srv_root) as srv:
        site = Site(name="ftp", protocol="ftp", host="127.0.0.1", port=srv.port, username=USER, password=PASSWORD)
        store = open_or_create(vault_path(), "master", create=True)
        store.upsert(site)

        def fake_editor(cmd, check=False):
            with open(cmd[-1], "a", encoding="utf-8") as f:
                f.write("added by the editor\n")

        async def go():
            app = T.BlamixFilesTUI(store)
            monkeypatch.setattr(app, "suspend", lambda: contextlib.nullcontext())
            monkeypatch.setattr(T.subprocess, "run", fake_editor)
            async with app.run_test(size=(150, 40)) as pilot:
                await until(lambda: isinstance(app.screen, T.SitesScreen))
                await pilot.press("enter")
                await until(lambda: isinstance(app.screen, T.BrowserScreen) and "/note.txt" in app.screen.remote.entries)
                scr = app.screen
                await pilot.press("tab")
                await until(lambda: scr.remote.has_focus_within)
                names = [e.name for e in T.sort_entries([e for k, e in scr.remote.entries.items() if k != ".."])]
                scr.remote.table.move_cursor(row=names.index("note.txt"))
                await pilot.press("v")                                    # view
                await until(lambda: isinstance(app.screen, T.TextScreen))
                assert "line one" in app.screen.text
                await pilot.press("escape")
                await until(lambda: isinstance(app.screen, T.BrowserScreen))

                await pilot.press("e")                                    # edit in "the editor"
                await until(lambda: "added by the editor" in (srv_root / "note.txt").read_text())
                assert (srv_root / "note.txt").read_text().startswith("line one")

                scr.remote.table.move_cursor(row=names.index("blob.bin"))
                await pilot.press("v")                                    # binary: refuses with a message, no screen
                await asyncio.sleep(0.5)
                assert isinstance(app.screen, T.BrowserScreen)
        asyncio.run(go())


def test_tui_two_factor_prompt_and_theme(tmp_path):
    from blamixfiles.settings import Settings
    store = open_or_create(vault_path(), "master", create=True)
    settings = Settings()
    settings["theme"] = "Nord"

    async def go():
        import threading
        app = T.BlamixFilesTUI(store, settings)
        async with app.run_test(size=(120, 36)) as pilot:
            await until(lambda: isinstance(app.screen, T.SitesScreen))
            assert app.theme == "blamix-nord"                              # from the saved setting
            app.theme = "blamix-light"                                     # picked in the command palette
            assert settings["theme"] == "Light"

            answer = []
            t = threading.Thread(target=lambda: answer.append(app._interactive("", "", [("Code: ", False)])))
            t.start()
            await until(lambda: isinstance(app.screen, T.AuthPromptScreen))
            await pilot.press(*"123456", "enter")
            await until(lambda: not t.is_alive())
            assert answer == [["123456"]]
    asyncio.run(go())


def test_tui_theme_names():
    assert T.resolve_theme_name("system") == "Midnight" and T.resolve_theme_name("light") == "Light"
    assert T.resolve_theme_name("Nord") == "Nord" and T.resolve_theme_name("nonsense") == "Midnight"
