"""Run a saved sync profile on a schedule, with the app closed, using the system's own scheduler:
Windows Task Scheduler (schtasks), cron on Linux and macOS. Each entry runs

    <BlamixFiles> sync "<profile>" --yes --quiet

The vault is opened with the password remembered in the OS keychain (File > Unlock with …), so that
has to be switched on; nothing secret is written into the task. Results land in the transfer log.
"""
from __future__ import annotations

import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass

TASK_FOLDER = "BlamixFiles"
CRON_TAG = "# blamixfiles-sync:"


@dataclass
class When:
    daily: str = ""          # "HH:MM"
    every_hours: int = 0     # 1..23

    @classmethod
    def parse(cls, daily: str = "", every: str | int = "") -> "When":
        if daily:
            m = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", daily.strip())
            if not m:
                raise ValueError("Use a 24-hour time like 02:30")
            return cls(daily=f"{int(m.group(1)):02d}:{m.group(2)}")
        if every not in ("", None):
            hours = int(str(every).strip().rstrip("hH"))
            if not 1 <= hours <= 23:
                raise ValueError("Every 1 to 23 hours")
            return cls(every_hours=hours)
        raise ValueError("Give a daily time or a number of hours")

    def describe(self) -> str:
        return f"every day at {self.daily}" if self.daily else f"every {self.every_hours} hour(s)"


def sync_command(profile: str) -> list[str]:
    """How to start this BlamixFiles for a sync: the packaged app, or Python with the package."""
    if getattr(sys, "frozen", False):
        return [sys.executable, "sync", profile, "--yes", "--quiet"]
    return [sys.executable, "-m", "blamixfiles", "sync", profile, "--yes", "--quiet"]


def _task_name(profile: str) -> str:
    safe = re.sub(r'[\\/:*?"<>|]', "_", profile)
    return f"\\{TASK_FOLDER}\\Sync {safe}"


def _run(cmd: list[str], stdin: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, input=stdin, capture_output=True, text=True, timeout=30,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


# ---------------------------------------------------------------- Windows
def _win_cmdline(args: list[str]) -> str:
    return subprocess.list2cmdline(args)


def windows_create_args(profile: str, when: When) -> list[str]:
    args = ["schtasks", "/Create", "/F", "/TN", _task_name(profile), "/TR", _win_cmdline(sync_command(profile))]
    if when.daily:
        args += ["/SC", "DAILY", "/ST", when.daily]
    else:
        args += ["/SC", "HOURLY", "/MO", str(when.every_hours)]
    return args


# ---------------------------------------------------------------- cron
def cron_line(profile: str, when: When) -> str:
    if when.daily:
        h, m = when.daily.split(":")
        spec = f"{int(m)} {int(h)} * * *"
    else:
        spec = f"0 */{when.every_hours} * * *"
    cmd = " ".join(shlex.quote(a) for a in sync_command(profile))
    return f"{spec} {cmd} >/dev/null 2>&1 {CRON_TAG}{profile}"


def _read_crontab() -> list[str]:
    r = _run(["crontab", "-l"])
    return r.stdout.splitlines() if r.returncode == 0 else []


def _write_crontab(lines: list[str]) -> None:
    r = _run(["crontab", "-"], stdin="\n".join(lines).rstrip("\n") + "\n")
    if r.returncode != 0:
        raise OSError(r.stderr.strip() or "crontab refused the change")


def cron_without(lines: list[str], profile: str) -> list[str]:
    return [ln for ln in lines if not ln.rstrip().endswith(CRON_TAG + profile)]


# ---------------------------------------------------------------- public
def available() -> bool:
    return shutil.which("schtasks" if sys.platform == "win32" else "crontab") is not None


def install(profile: str, when: When) -> None:
    if sys.platform == "win32":
        r = _run(windows_create_args(profile, when))
        if r.returncode != 0:
            raise OSError((r.stderr or r.stdout).strip() or "schtasks failed")
        return
    _write_crontab(cron_without(_read_crontab(), profile) + [cron_line(profile, when)])


def remove(profile: str) -> bool:
    if sys.platform == "win32":
        r = _run(["schtasks", "/Delete", "/F", "/TN", _task_name(profile)])
        return r.returncode == 0
    lines = _read_crontab()
    kept = cron_without(lines, profile)
    if kept == lines:
        return False
    _write_crontab(kept)
    return True


def parse_cron(lines: list[str]) -> dict[str, str]:
    """profile -> description, from crontab lines."""
    out = {}
    for ln in lines:
        if CRON_TAG not in ln:
            continue
        profile = ln.split(CRON_TAG, 1)[1].strip()
        f = ln.split()
        if len(f) >= 5 and f[1].startswith("*/"):
            out[profile] = f"every {f[1][2:]} hour(s)"
        elif len(f) >= 5:
            out[profile] = f"every day at {int(f[1]):02d}:{int(f[0]):02d}"
    return out


def parse_schtasks_csv(text: str) -> dict[str, str]:
    """profile -> next run time, from `schtasks /Query /FO CSV /NH`."""
    import csv
    import io
    out = {}
    prefix = f"\\{TASK_FOLDER}\\Sync "
    for row in csv.reader(io.StringIO(text)):
        if row and row[0].startswith(prefix):
            out[row[0][len(prefix):]] = f"next run {row[1]}" if len(row) > 1 else "scheduled"
    return out


def listing() -> dict[str, str]:
    """Scheduled profiles -> a short description."""
    try:
        if sys.platform == "win32":
            r = _run(["schtasks", "/Query", "/FO", "CSV", "/NH"])
            return parse_schtasks_csv(r.stdout) if r.returncode == 0 else {}
        return parse_cron(_read_crontab())
    except (OSError, subprocess.SubprocessError):
        return {}
