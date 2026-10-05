import io
import json
import zipfile
from pathlib import Path

import pytest

from blamixfiles import updater as U

RELEASE = {
    "tag_name": "v1.2.0", "html_url": "https://github.com/x/y/releases/tag/v1.2.0", "body": "- fixes",
    "assets": [
        {"name": "BlamixFiles-1.2.0-x64.msi", "browser_download_url": "https://x/a.msi", "size": 10,
         "digest": "sha256:" + "ab" * 32},
        {"name": "BlamixFiles-windows-x64.zip", "browser_download_url": "https://x/a.zip", "size": 20},
        {"name": "BlamixFiles-linux-x64.tar.gz", "browser_download_url": "https://x/a.tgz", "size": 30},
    ],
}


def test_versions():
    assert U.parse_version("v1.2") == (1, 2, 0)
    assert U.parse_version("0.1.0.dev0") == (0, 1, 0)
    assert U.parse_version("nonsense") == ()
    assert U.is_newer("v1.0.1", "1.0.0") and not U.is_newer("v1.0.0", "1.0.0")
    assert not U.is_newer("garbage", "1.0.0")


def test_release_and_asset_pick():
    rel = U.parse_release(RELEASE)
    assert rel.version == "1.2.0" and len(rel.assets) == 3
    assert rel.assets[0].sha256 == "ab" * 32
    assert U.pick_asset(rel, "msi").name.endswith(".msi")
    assert U.pick_asset(rel, "portable").name == "BlamixFiles-windows-x64.zip"
    assert U.pick_asset(rel, "linux") is None and U.pick_asset(rel, "source") is None


def test_check_uses_the_network_helper(monkeypatch):
    class R(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): return False
    monkeypatch.setattr(U, "_get", lambda url, timeout=10: R(json.dumps(RELEASE).encode()))
    assert U.check("1.0.0").tag == "v1.2.0"
    assert U.check("1.2.0") is None

    def boom(url, timeout=10):
        raise OSError("offline")
    monkeypatch.setattr(U, "_get", boom)
    with pytest.raises(U.UpdateError):
        U.check("1.0.0")


def test_download_checks_size_and_digest(monkeypatch, tmp_path):
    class R(io.BytesIO):
        headers = {}
        def __enter__(self): return self
        def __exit__(self, *a): return False
    monkeypatch.setattr(U, "_get", lambda url, timeout=30: R(b"x" * 10))
    ok = U.Asset("a.msi", "u", 10, __import__("hashlib").sha256(b"x" * 10).hexdigest())
    assert U.download(ok, tmp_path).read_bytes() == b"x" * 10
    with pytest.raises(U.UpdateError, match="checksum"):
        U.download(U.Asset("b.msi", "u", 10, "00" * 32), tmp_path)
    assert not (tmp_path / "b.msi").exists()
    with pytest.raises(U.UpdateError, match="incomplete"):
        U.download(U.Asset("c.msi", "u", 99), tmp_path)


def test_policy(monkeypatch):
    monkeypatch.delenv("BLAMIXFILES_UPDATE_CHECK", raising=False)
    monkeypatch.setattr(U, "_registry_policy", lambda: None)
    monkeypatch.setattr(U, "_ini_policy", lambda: None)
    assert U.update_check_policy() is None
    assert U.updates_allowed({"check_updates": True}) and not U.updates_allowed({"check_updates": False})
    monkeypatch.setenv("BLAMIXFILES_UPDATE_CHECK", "off")
    assert U.update_check_policy() is False and not U.updates_allowed({"check_updates": True})
    monkeypatch.setenv("BLAMIXFILES_UPDATE_CHECK", "1")
    assert U.updates_allowed({"check_updates": False})


def test_update_file_checks(tmp_path):
    assert U.file_version(Path("BlamixFiles-1.4.2-x64.msi")) == "1.4.2"
    msi = tmp_path / "BlamixFiles-1.4.2-x64.msi"
    msi.write_bytes(b"msi")
    assert U.check_update_file(msi, "msi") == ""
    assert "pick the .msi" in U.check_update_file(tmp_path / "a.zip", "msi")
    assert "SHA-256" in U.check_update_file(msi, "msi", "00" * 32)
    assert U.check_update_file(msi, "msi", "sha256:" + U.sha256_of(msi)) == ""
    assert U.check_update_file(msi, "source")                      # not installable from source
    good, bad = tmp_path / "ok.zip", tmp_path / "bad.zip"
    with zipfile.ZipFile(good, "w") as z:
        z.writestr("BlamixFiles/BlamixFiles.exe", "x")
    with zipfile.ZipFile(bad, "w") as z:
        z.writestr("other.txt", "x")
    assert U.check_update_file(good, "portable") == ""
    assert "doesn't contain" in U.check_update_file(bad, "portable")
    (tmp_path / "junk.zip").write_bytes(b"not a zip")
    assert "valid zip" in U.check_update_file(tmp_path / "junk.zip", "portable")


def test_scripts_keep_data_and_restart(tmp_path, monkeypatch):
    app = tmp_path / "app"
    portable = U.portable_update_script(tmp_path / "new", app, 123)
    assert "/XD data" in portable and "BlamixFiles.exe" in portable and "PID eq 123" in portable
    msi = U.msi_update_script(tmp_path / "x.msi", app, 123)
    assert "msiexec /i" in msi and "BlamixFiles.exe" in msi
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    assert U.msi_scope_args(tmp_path / "local" / "Programs" / "BlamixFiles") == "ALLUSERS=2 MSIINSTALLPERUSER=1"
    assert U.msi_scope_args(tmp_path / "Program Files" / "BlamixFiles") == "ALLUSERS=1"


def test_updates_controller(monkeypatch, tmp_path):
    """Quiet check shows the status-bar button; 'skip' hides it; up to date does nothing."""
    from PySide6.QtWidgets import QApplication

    from blamixfiles.models import Store
    from blamixfiles.settings import Settings
    from blamixfiles.ui import update as UI
    from blamixfiles.ui.bridge import init_bridge
    from blamixfiles.ui.main_window import MainWindow
    from blamixfiles.vault import Vault

    app = QApplication.instance() or QApplication([])
    init_bridge()
    monkeypatch.delenv("BLAMIXFILES_UPDATE_CHECK", raising=False)
    monkeypatch.setattr(U, "_registry_policy", lambda: None)
    monkeypatch.setattr(U, "_ini_policy", lambda: None)
    st = Store(Vault.create(tmp_path / "v.bfv", "pw", n_log2=10), {})
    win = MainWindow(st, Settings())
    rel = U.parse_release(RELEASE)
    win.updates._checked(False, rel, "")
    assert not win.updates.button.isHidden() and "1.2.0" in win.updates.button.text()
    assert win.settings["last_update_check"] > 0

    class Skip:
        def __init__(self, *a, **k): self.choice = "skip"
        def exec(self): return 0
    monkeypatch.setattr(UI, "UpdateDialog", Skip)
    win.updates.show_update(rel)
    assert win.updates.button.isHidden() and win.settings["skip_version"] == "1.2.0"
    win.updates._checked(False, rel, "")                     # skipped version: stays hidden
    assert win.updates.button.isHidden()
    win.updates._checked(False, None, "")
    assert win.updates.button.isHidden()
    win.close()
    app.processEvents()
