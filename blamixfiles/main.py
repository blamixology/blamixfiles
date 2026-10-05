"""Entry point: unlock (or create) the vault, then show the main window."""
from __future__ import annotations

import os
import sys


def main() -> None:
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("BlamixFiles.Transfer.1")
        except Exception:
            pass

    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import QApplication, QDialog

    app = QApplication(sys.argv)
    app.setApplicationName("BlamixFiles")
    app.setOrganizationName("BlamixFiles")

    from . import keychain
    from .models import Store
    from .paths import assets_dir, vault_path
    from .settings import Settings
    from .ui import theme
    from .vault import Vault, VaultError, WrongPassword

    # the theme must be chosen before any widget module is imported (they read its colors)
    settings = Settings()
    theme.set_theme(os.environ.get("BLAMIXFILES_THEME") or settings["theme"], app)

    from .ui.bridge import init_bridge
    from .ui.dialogs import UnlockDialog
    from .ui.main_window import MainWindow

    init_bridge()
    theme.apply_palette(app)
    ico = assets_dir() / ("app.ico" if sys.platform == "win32" else "app.png")
    if ico.exists():
        app.setWindowIcon(QIcon(str(ico)))
    app.setDesktopFileName("blamixfiles")

    if os.environ.get("BLAMIXFILES_SELFTEST"):
        sys.exit(_selftest(app))

    path = vault_path()
    create = not Vault.exists(path)
    account = keychain.account_for(path)
    holder: dict = {}

    def attempt(pw: str) -> str:
        try:
            if create:
                holder["store"] = Store(Vault.create(path, pw), {})
            else:
                v, data = Vault.open(path, pw)
                holder["store"] = Store(v, data)
            holder["pw"] = pw
            return ""
        except WrongPassword:
            return "Wrong master password."
        except VaultError as e:
            return str(e)
        except Exception as e:  # noqa: BLE001 (corrupted file etc.)
            return f"Could not open the vault: {e}"

    # silent unlock with the password remembered in the OS keychain
    if not create and keychain.available() and not os.environ.get("BLAMIXFILES_NO_KEYCHAIN"):
        saved = keychain.load(account)
        if saved and attempt(saved):
            keychain.delete(account)        # stale (password changed): ask again
            holder.pop("store", None)

    if "store" not in holder:
        dlg = UnlockDialog(create, attempt, remember=keychain.backend_name())
        if dlg.exec() != QDialog.Accepted:
            sys.exit(0)
        if keychain.available():
            if dlg.remember_checked():
                keychain.save(account, holder["pw"])
            else:
                keychain.delete(account)
    win = MainWindow(holder["store"], settings)
    win.keychain_account = account
    win.show()
    win.restore_tabs()
    win.updates.start()
    _clean_drag_cache()
    sys.exit(app.exec())


def _clean_drag_cache() -> None:
    """Files downloaded for drops on the desktop: the OS has copied them long ago."""
    import shutil
    import time

    from .paths import data_dir
    d = data_dir() / "drag-out"
    if d.is_dir():
        for p in d.iterdir():
            try:
                if time.time() - p.stat().st_mtime > 3600:
                    shutil.rmtree(p, ignore_errors=True)
            except OSError:
                pass


def _selftest(app) -> int:
    """Packaging check: build the main window with a throwaway vault and quit."""
    import tempfile
    from pathlib import Path

    from .models import Store
    from .settings import Settings
    from .ui.main_window import MainWindow
    from .vault import Vault

    import threading
    # never hang a build or `dev.sh check`: give up after 90 s with a clear exit code
    watchdog = threading.Timer(90, lambda: (print("SELFTEST TIMEOUT", flush=True), os._exit(4)))
    watchdog.daemon = True
    watchdog.start()

    tmp = Path(tempfile.mkdtemp(prefix="blamixfiles-selftest-"))
    os.environ["BLAMIXFILES_HOME"] = str(tmp)      # never touch the real data folder
    store = Store(Vault.create(tmp / "v.bfv", "selftest", n_log2=10), {})
    print("selftest: window", flush=True)
    win = MainWindow(store, Settings())
    win.show()
    app.processEvents()
    print("selftest: libraries", flush=True)
    import pygments.lexers  # noqa: F401  (bundled?)
    import httpx  # noqa: F401
    import boto3                         # the S3 model must survive packaging/pruning
    boto3.session.Session().client("s3", region_name="us-east-1", aws_access_key_id="x",
                                   aws_secret_access_key="y")
    print("selftest: closing", flush=True)
    win.close()
    print("SELFTEST OK", flush=True)
    return 0


if __name__ == "__main__":
    main()
