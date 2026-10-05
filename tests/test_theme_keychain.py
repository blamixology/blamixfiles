import sys
import uuid

import pytest

from blamixfiles import keychain


@pytest.mark.skipif(sys.platform != "win32" or not keychain.available(), reason="Windows Credential Manager only")
def test_keychain_roundtrip():
    acct = "test-" + uuid.uuid4().hex
    try:
        assert keychain.load(acct) is None
        assert keychain.save(acct, "pässword ✓ 123")
        assert keychain.load(acct) == "pässword ✓ 123"
        assert keychain.save(acct, "second")          # overwrite
        assert keychain.load(acct) == "second"
    finally:
        keychain.delete(acct)
    assert keychain.load(acct) is None


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
