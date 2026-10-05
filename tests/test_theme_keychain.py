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


def test_theme_tokens_match():
    from blamixfiles.ui import theme
    assert theme.DARK.keys() == theme.LIGHT.keys()
    assert theme.set_theme("light") == "light" and theme.C["bg"] == theme.LIGHT["bg"]
    assert not theme.is_dark()
    assert theme.set_theme("dark") == "dark" and theme.is_dark()
    assert theme.set_theme("system") in ("dark", "light")
    theme.set_theme("dark")
    assert "#" in theme.build_qss() and "{C[" not in theme.build_qss()
