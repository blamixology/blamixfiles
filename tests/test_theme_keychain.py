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
