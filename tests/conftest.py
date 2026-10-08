import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def pytest_unconfigure(config):
    """Shut Qt down in order before Python's own teardown. Otherwise the interpreter destroys what the
    tests left (windows, dialogs, timers, highlighters) in an arbitrary order with the application still
    alive, which on Linux ends in "shared QObject was deleted directly" and a segfault (exit code 139
    although every test passed)."""
    import gc
    import sys
    if "PySide6.QtCore" not in sys.modules:
        return
    from PySide6.QtCore import QCoreApplication, QEvent
    app = QCoreApplication.instance()
    if app is None:
        return
    widgets = getattr(app, "topLevelWidgets", lambda: [])()
    for w in widgets:
        w.close()
        w.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    app.processEvents()
    del widgets
    gc.collect()
    app.shutdown()                        # destroys the application object now, not at interpreter exit


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Every test gets its own data folder (known_hosts, vault, settings)."""
    home = tmp_path / "data"
    home.mkdir()
    monkeypatch.setenv("BLAMIXFILES_HOME", str(home))
    return home
