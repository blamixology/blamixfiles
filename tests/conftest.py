import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Every test gets its own data folder (known_hosts, vault, settings)."""
    home = tmp_path / "data"
    home.mkdir()
    monkeypatch.setenv("BLAMIXFILES_HOME", str(home))
    return home
