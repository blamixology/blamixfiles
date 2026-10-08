"""'Open SSH terminal here': a terminal window logged in to the site, already in the folder
you're looking at. Uses the system's OpenSSH client (built into Windows 10+, macOS, Linux),
so your keys/agent and jump hosts work the same way as in the app."""
from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys


def ssh_command(site, path: str, resolve) -> list[str]:
    cmd = ["ssh", "-t"]
    if site.effective_port != 22:
        cmd += ["-p", str(site.effective_port)]
    if site.auth == "key" and site.key_path:
        cmd += ["-i", os.path.expanduser(site.key_path)]
    hops = []
    j = resolve(site.jump_id) if getattr(site, "jump_id", "") else None
    seen = set()
    while j is not None and j.id not in seen:
        seen.add(j.id)
        hops.append(f"{j.username + '@' if j.username else ''}{j.host}"
                    + (f":{j.effective_port}" if j.effective_port != 22 else ""))
        j = resolve(j.jump_id) if j.jump_id else None
    if hops:
        cmd += ["-J", ",".join(reversed(hops))]
    cmd.append(f"{site.username}@{site.host}" if site.username else site.host)
    if path:
        cmd.append(f"cd {shlex.quote(path)} && exec \"$SHELL\" -l")
    return cmd


def open_terminal(site, path: str, resolve) -> str:
    """Launch a terminal window. Returns '' or an error message."""
    if shutil.which("ssh") is None:
        return "No ssh client found (Windows: Settings → Optional features → OpenSSH Client)."
    cmd = ssh_command(site, path, resolve)
    try:
        if sys.platform == "win32":
            wt = shutil.which("wt")
            if wt:
                subprocess.Popen([wt, "new-tab", "--title", site.label, *cmd])
            else:
                subprocess.Popen(["cmd", "/c", "start", site.label, *cmd])
        elif sys.platform == "darwin":
            script = " ".join(shlex.quote(c) for c in cmd).replace("\\", "\\\\").replace('"', '\\"')
            subprocess.Popen(["osascript", "-e", f'tell application "Terminal" to do script "{script}"',
                              "-e", 'tell application "Terminal" to activate'])
        else:
            for term in ("x-terminal-emulator", "gnome-terminal", "konsole", "xfce4-terminal", "xterm"):
                exe = shutil.which(term)
                if exe:
                    extra = ["--"] if term == "gnome-terminal" else ["-e"]
                    subprocess.Popen([exe, *extra, *cmd])
                    break
            else:
                return "No terminal emulator found."
    except OSError as e:
        return str(e)
    return ""


# ---------------------------------------------------------------- Open in BlamixShell
# BlamixShell (the sister SSH client) opens a server from its command line and hands it to the window
# that is already open:  BlamixShell --connect <name|user@host:port> [--key FILE] [--jump SERVER]
# A saved BlamixShell server with that address is reused (its own login); a new address opens its New
# server form filled in. No password ever goes on the command line.

def _blamixshell_candidates() -> list[str]:
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA", "")
        roots = [os.path.join(local, "Programs"), os.environ.get("ProgramFiles", ""),
                 os.environ.get("ProgramFiles(x86)", "")]
        return [os.path.join(r, "BlamixShell", "BlamixShell.exe") for r in roots if r]
    if sys.platform == "darwin":
        return ["/Applications/BlamixShell.app/Contents/MacOS/BlamixShell",
                os.path.expanduser("~/Applications/BlamixShell.app/Contents/MacOS/BlamixShell")]
    return [os.path.expanduser("~/Applications/BlamixShell/BlamixShell"), "/opt/BlamixShell/BlamixShell"]


def find_blamixshell(configured: str = "") -> list[str] | None:
    """The command that starts BlamixShell's window, or None when it isn't installed.
    The packaged app takes --connect directly; the pip-installed `blamixshell` needs `gui` first."""
    if configured and os.path.exists(configured):
        app = configured
        if app.endswith(".app"):
            app = os.path.join(app, "Contents", "MacOS", "BlamixShell")
        stem = os.path.splitext(os.path.basename(app))[0]
        return [app, "gui"] if stem == "blamixshell" else [app]     # lower case = the pip command line
    for c in _blamixshell_candidates():
        if os.path.isfile(c):
            return [c]
    for name in ("BlamixShell",):                 # packaged app on PATH
        exe = shutil.which(name)
        if exe and os.path.basename(exe).startswith("BlamixShell"):
            return [exe]
    exe = shutil.which("blamixshell")             # pip / pipx install: the CLI, its GUI is a sub-command
    if exe:
        return [exe, "gui"]
    return None


def blamixshell_target(site, resolve) -> list[str]:
    """--connect / --key / --jump for a site (addresses, so BlamixShell can match its saved servers)."""
    def addr(s) -> str:
        host = f"[{s.host}]" if ":" in s.host else s.host
        port = f":{s.effective_port}" if s.effective_port != 22 else ""
        return (f"{s.username}@" if s.username else "") + host + port
    args = ["--connect", addr(site)]
    if site.auth == "key" and site.key_path:
        args += ["--key", os.path.expanduser(site.key_path)]
    jump = resolve(site.jump_id) if getattr(site, "jump_id", "") else None
    if jump is not None:
        args += ["--jump", addr(jump)]
    return args


def open_in_blamixshell(site, resolve, configured: str = "") -> str:
    """Start (or hand off to) BlamixShell for this site. Returns '' or an error message."""
    cmd = find_blamixshell(configured)
    if cmd is None:
        return "BlamixShell isn't installed (https://github.com/blamixology/blamixshell)."
    try:
        kw = {}
        if sys.platform == "win32":
            kw["creationflags"] = getattr(subprocess, "DETACHED_PROCESS", 0x8) | \
                getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200)
        else:
            kw["start_new_session"] = True
        subprocess.Popen([*cmd, *blamixshell_target(site, resolve)], close_fds=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kw)
    except OSError as e:
        return f"Could not start BlamixShell: {e}"
    return ""
