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

    from .models import Store
    from .paths import assets_dir, vault_path
    from .settings import Settings
    from .ui.bridge import init_bridge
    from .ui.dialogs import UnlockDialog
    from .ui.main_window import MainWindow
    from .ui.theme import apply_palette
    from .vault import Vault, VaultError, WrongPassword

    init_bridge()
    apply_palette(app)
    ico = assets_dir() / ("app.ico" if sys.platform == "win32" else "app.png")
    if ico.exists():
        app.setWindowIcon(QIcon(str(ico)))
    app.setDesktopFileName("blamixfiles")

    if os.environ.get("BLAMIXFILES_SELFTEST"):
        sys.exit(_selftest(app))

    path = vault_path()
    create = not Vault.exists(path)
    holder: dict = {}

    def attempt(pw: str) -> str:
        try:
            if create:
                holder["store"] = Store(Vault.create(path, pw), {})
            else:
                v, data = Vault.open(path, pw)
                holder["store"] = Store(v, data)
            return ""
        except WrongPassword:
            return "Wrong master password."
        except VaultError as e:
            return str(e)
        except Exception as e:  # noqa: BLE001 (corrupted file etc.)
            return f"Could not open the vault: {e}"

    dlg = UnlockDialog(create, attempt)
    if dlg.exec() != QDialog.Accepted:
        sys.exit(0)
    win = MainWindow(holder["store"], Settings())
    win.show()
    sys.exit(app.exec())


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
