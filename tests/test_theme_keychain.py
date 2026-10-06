import sys
import uuid

import pytest

from blamixfiles import keychain


@pytest.mark.skipif(not keychain.available(), reason="no OS keychain here")
def test_keychain_roundtrip():
    """Windows Credential Manager / macOS Keychain / Secret Service. A CI runner may have a keychain
    tool but a locked or missing store: there only a refused save is tolerated (skipped)."""
    acct = "test-" + uuid.uuid4().hex
    try:
        if not keychain.save(acct, "pässword ✓ 123"):
            if sys.platform == "win32":
                pytest.fail("Credential Manager refused the save")
            pytest.skip("the keychain here is locked or not usable")
        assert keychain.load(acct) == "pässword ✓ 123"
        assert keychain.save(acct, "second")          # overwrite
        assert keychain.load(acct) == "second"
    finally:
        keychain.delete(acct)
    assert keychain.load(acct) is None


def test_keychain_commands_per_platform(monkeypatch):
    """The macOS / Linux commands are built correctly (no real keychain needed)."""
    calls = []

    class R:
        returncode = 0
        stdout = b"stored-secret\n"

    def fake_run(cmd, stdin=None):
        calls.append((cmd, stdin))
        return R()
    monkeypatch.setattr(keychain, "_run", fake_run)
    monkeypatch.setattr(keychain.sys, "platform", "darwin")
    assert keychain.save("acct", "pw") and keychain.load("acct") == "stored-secret" and keychain.delete("acct")
    assert calls[0][0][:3] == ["security", "add-generic-password", "-U"] and keychain._wrap("pw") in calls[0][0]
    assert calls[1][0][:2] == ["security", "find-generic-password"] and calls[2][0][1] == "delete-generic-password"
    calls.clear()
    monkeypatch.setattr(keychain.sys, "platform", "linux")
    assert keychain.save("acct", "pw") and keychain.load("acct") == "stored-secret" and keychain.delete("acct")
    assert calls[0][0][:2] == ["secret-tool", "store"] and calls[0][1] == keychain._wrap("pw").encode()   # via stdin
    assert keychain._wrap("pw") not in calls[0][0]
    assert calls[1][0][:2] == ["secret-tool", "lookup"] and calls[2][0][:2] == ["secret-tool", "clear"]
    monkeypatch.setattr(keychain, "_run", lambda cmd, stdin=None: None)               # tool missing
    assert keychain.load("acct") is None and not keychain.save("acct", "pw") and not keychain.delete("acct")


def test_non_ascii_password_survives_macos_hex_output(monkeypatch):
    """`security -w` prints a non-ASCII password as hex: the wrapped (ASCII) form round-trips anyway."""
    store = {}

    class R:
        returncode = 0

        def __init__(self, out=b""):
            self.stdout = out

    def fake_security(cmd, stdin=None):
        if cmd[1] == "add-generic-password":
            store["v"] = cmd[cmd.index("-w") + 1]
            return R()
        v = store["v"]
        out = v if v.isascii() else v.encode("utf-8").hex()          # what the real tool does
        return R(out.encode() + b"\n")
    monkeypatch.setattr(keychain, "_run", fake_security)
    monkeypatch.setattr(keychain.sys, "platform", "darwin")
    for pw in ("pässword ✓ 123", "plain-ascii", "with\nnewline", "b64:looks-like-the-prefix"):
        assert keychain.save("acct", pw) and keychain.load("acct") == pw
    store["v"] = "old-plain-secret"                                    # saved by an older version
    assert keychain.load("acct") == "old-plain-secret"


def test_account_is_per_vault(tmp_path):
    a, b = keychain.account_for(tmp_path / "a.bfv"), keychain.account_for(tmp_path / "b.bfv")
    assert a != b and a == keychain.account_for(tmp_path / "a.bfv")


def test_themes():
    from blamixfiles.ui import theme
    keys = set(theme.THEMES["Midnight"]["colors"])
    for name, t in theme.THEMES.items():
        assert set(t["colors"]) == keys, name
        assert theme.set_theme(name) == name and theme.C["bg"] == t["colors"]["bg"]
        assert theme.is_dark() == t["dark"]
        qss = theme.build_qss()
        assert "{C[" not in qss and "None" not in qss
    assert theme.set_theme("light") == "Light" and not theme.is_dark()      # old setting values
    assert theme.set_theme("dark") == "Midnight"
    assert theme.set_theme(theme.SYSTEM) in ("Midnight", "Light")
    assert theme.set_theme("nonsense") == theme.DEFAULT_THEME


LIVE_SWITCH = r"""
import os, sys, tempfile
from pathlib import Path
from PySide6.QtWidgets import QApplication
from blamixfiles.models import Site, Store
from blamixfiles.settings import Settings
from blamixfiles.ui import theme
from blamixfiles.ui.bridge import init_bridge
from blamixfiles.ui.main_window import MainWindow
from blamixfiles.vault import Vault

app = QApplication([])
init_bridge()
theme.set_theme("Midnight", app)
theme.apply_palette(app)
st = Store(Vault.create(Path(tempfile.mkdtemp()) / "v.bfv", "pw", n_log2=10), {})
st.upsert(Site(name="web", protocol="ftp", host="h"))
win = MainWindow(st, Settings())
win.show()
app.processEvents()
dark_qss = app.styleSheet()
win.show_message("hello")                                   # inline style with the muted color
assert theme.THEMES["Midnight"]["colors"]["muted"] in win.status_label.styleSheet()
key_before = win.sidebar_btn.icon().cacheKey()

win.set_theme("Light")
app.processEvents()
assert theme.CURRENT == "Light" and not theme.is_dark()
assert app.styleSheet() != dark_qss and theme.C["bg"] in app.styleSheet()
assert theme.THEMES["Midnight"]["colors"]["muted"] not in win.status_label.styleSheet()
assert win.sidebar_btn.icon().cacheKey() != key_before      # icons were redrawn in the new colors
assert win.settings["theme"] == "Light"

win.set_theme("Nord")
assert theme.CURRENT == "Nord" and theme.THEMES["Nord"]["colors"]["bg"] in app.styleSheet()
win.close()
print("LIVE OK")
"""


def test_live_theme_switch(tmp_path):
    """Own process: swapping the app stylesheet next to other tests' leftover widgets can crash Qt."""
    import os
    import subprocess
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", BLAMIXFILES_HOME=str(tmp_path))
    r = subprocess.run([sys.executable, "-c", LIVE_SWITCH], capture_output=True, text=True, env=env, timeout=120,
                       cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    assert "LIVE OK" in r.stdout, r.stdout + r.stderr
