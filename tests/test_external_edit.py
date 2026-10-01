"""Edit in another app: change detection (the GUI flow is tested in test_gui.py)."""
from __future__ import annotations

import os
import time

from blamixfiles.core.external_edit import ExternalEdit, ExternalEdits
from blamixfiles.models import Site


def test_poll_reports_saved_files_once_settled(tmp_path):
    ed = ExternalEdits(tmp_path / "x", settle=1.0)
    site = Site(protocol="sftp", host="h")
    local = ed.local_path(site.id, "/etc/nginx/nginx.conf")
    assert local.endswith(os.sep + "nginx.conf")
    assert ed.local_path(site.id, "/other/nginx.conf") != local          # no clash between folders
    open(local, "w").write("v1")
    e = ed.add(ExternalEdit(site=site, session=None, remote_path="/etc/nginx/nginx.conf",
                            local_path=local, remote_mtime=100, remote_size=2))
    assert ed.poll(now=0) == []
    open(local, "w").write("v2 saved")
    os.utime(local, (time.time() + 3, time.time() + 3))
    assert ed.poll(now=10) == []                       # just changed: wait
    assert ed.poll(now=11.5) == [e]
    ed.uploaded(e, 200, 8)
    assert ed.poll(now=20) == [] and e.uploads == 1
    assert not ed.remote_changed(e, 200, 8)
    assert ed.remote_changed(e, 260, 8) and ed.remote_changed(e, 200, 9)
