"""FTP LIST times are in the server's time zone: auto-detect with MDTM, or set per site."""
from __future__ import annotations

import calendar
import ftplib
import time

from blamixfiles.core.backends.ftp import FTPBackend, fmt_tz, parse_tz
from blamixfiles.models import Site
from blamixfiles.ui.dialogs import _tz_from_text

NOW = time.gmtime()
# a file changed at 10:41:37 UTC today (well, this year), on a server running at UTC+2
UTC = calendar.timegm((NOW.tm_year, 1, 2, 10, 41, 37))


def list_line(name, local_hh_mm, size=10, kind="-"):
    return f"{kind}rw-r--r--   1 www  www  {size:>8} Jan 02 {local_hh_mm} {name}"


class FakeFTP:
    def __init__(self, lines, mdtm: dict, mdtm_ok=True):
        self.lines, self.mdtm, self.mdtm_ok, self.sent = lines, mdtm, mdtm_ok, []

    def retrlines(self, cmd, cb):
        for ln in self.lines:
            cb(ln)

    def sendcmd(self, cmd):
        self.sent.append(cmd)
        if not self.mdtm_ok:
            raise ftplib.error_perm("500 Unknown command")
        path = cmd.split(" ", 1)[1]
        if path not in self.mdtm:
            raise ftplib.error_perm("550 Not a plain file")
        return "213 " + time.strftime("%Y%m%d%H%M%S", time.gmtime(self.mdtm[path]))


def backend(lines, mdtm, tz="auto", features=("MDTM",), mdtm_ok=True):
    b = FTPBackend(Site(protocol="ftp", host="h", ftp_tz=tz))
    b.ftp = FakeFTP(lines, mdtm, mdtm_ok)
    b.features = set(features)
    b._mlsd = False
    return b


def test_detects_server_time_zone_from_mdtm():
    lines = [list_line("logs", "12:41", kind="d"),            # folders aren't used to compare
             list_line("index.php", "12:41")]                # 10:41 UTC shown as 12:41 local
    b = backend(lines, {"/www/index.php": UTC})
    entries = {e.name: e for e in b.list("/www")}
    assert b.tz_offset == 2 * 3600 and "UTC+2" in b.tz_note
    assert entries["index.php"].mtime == UTC - 37              # corrected (LIST has no seconds)
    assert entries["logs"].mtime == UTC - 37
    b.ftp.sent.clear()
    b.list("/www")
    assert b.ftp.sent == []                                     # detected once per connection


def test_half_hour_zone_and_negative_zone():
    b = backend([list_line("a.txt", "16:11")], {"/a.txt": UTC})         # UTC+5:30
    b.list("/")
    assert b.tz_offset == 5 * 3600 + 1800 and fmt_tz(b.tz_offset) == "UTC+5:30"
    b = backend([list_line("a.txt", "05:41")], {"/a.txt": UTC})         # UTC-5
    b.list("/")
    assert b.tz_offset == -5 * 3600


def test_manual_setting_skips_detection():
    b = backend([list_line("a.txt", "13:41")], {"/a.txt": UTC}, tz="180")
    e = b.list("/")[0]
    assert b.ftp.sent == [] and e.mtime == UTC - 37


def test_no_mdtm_means_utc():
    b = backend([list_line("a.txt", "10:41")], {}, features=("SIZE",))
    assert b.list("/")[0].mtime == UTC - 37 and b.tz_offset == 0 and "no MDTM" in b.tz_note


def test_old_files_only_waits_for_a_better_folder():
    old = "-rw-r--r--   1 www  www        10 Jan 02  2019 old.txt"
    b = backend([old], {"/old.txt": UTC})
    b.list("/")
    assert b.tz_offset is None                                  # nothing precise to compare yet


def test_parse_tz_and_dialog_text():
    assert parse_tz("auto") is None and parse_tz("120") == 7200 and parse_tz("-330") == -19800
    assert _tz_from_text("UTC+2") == "120" and _tz_from_text("-5:30") == "-330"
    assert _tz_from_text("gmt-3") == "-180" and _tz_from_text("Auto-detect") == "auto"
    assert _tz_from_text("nonsense") == "auto"
