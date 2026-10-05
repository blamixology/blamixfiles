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


# TODO(BlamixShell): uncomment (and adjust the flags) once BlamixShell accepts a site on its
# command line. Proposed contract, matching the vault entry BlamixFiles imports:
#     blamixshell --site "<site label or id>" [--cd "<remote folder>"]
# BlamixShell resolves the site from its own vault, so no credentials pass through here.
#
# def blamixshell_executable() -> str | None:
#     """BlamixShell on PATH, or in its usual per-OS install location."""
#     exe = shutil.which("blamixshell")
#     if exe:
#         return exe
#     candidates = []
#     if sys.platform == "win32":
#         base = os.environ.get("LOCALAPPDATA", "")
#         candidates = [os.path.join(base, "Programs", "BlamixShell", "BlamixShell.exe")]
#     elif sys.platform == "darwin":
#         candidates = ["/Applications/BlamixShell.app/Contents/MacOS/BlamixShell"]
#     return next((c for c in candidates if os.path.isfile(c)), None)
#
#
# def open_in_blamixshell(site, path: str = "") -> str:
#     """Start BlamixShell on this site. Returns '' or an error message."""
#     exe = blamixshell_executable()
#     if exe is None:
#         return "BlamixShell isn't installed (or not on your PATH)."
#     cmd = [exe, "--site", site.label]
#     if path:
#         cmd += ["--cd", path]
#     try:
#         subprocess.Popen(cmd)
#     except OSError as e:
#         return str(e)
#     return ""
