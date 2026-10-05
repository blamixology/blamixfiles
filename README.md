# BlamixFiles

[![tests](https://github.com/blamixology/blamixfiles/actions/workflows/tests.yml/badge.svg?branch=main)](https://github.com/blamixology/blamixfiles/actions/workflows/tests.yml)
[![build](https://github.com/blamixology/blamixfiles/actions/workflows/release.yml/badge.svg)](https://github.com/blamixology/blamixfiles/actions/workflows/release.yml)
[![release](https://img.shields.io/github/v/release/blamixology/blamixfiles)](https://github.com/blamixology/blamixfiles/releases/latest)
[![downloads](https://img.shields.io/github/downloads/blamixology/blamixfiles/total)](https://github.com/blamixology/blamixfiles/releases)
[![license](https://img.shields.io/github/license/blamixology/blamixfiles)](LICENSE)
[![python](https://img.shields.io/badge/python-3.10%2B-blue)](pyproject.toml)
[![platforms](https://img.shields.io/badge/platforms-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey)](https://github.com/blamixology/blamixfiles/releases/latest)

A free, open-source file transfer client: a modern FileZilla alternative.
SFTP, SCP, FTP/FTPS, WebDAV and S3 in one portable app, a **built-in editor** that saves straight to
the server, and an **encrypted password vault**. Windows, macOS and Linux, plus a CLI.

> 🚧 **Work in progress.** Early development, not released yet. Sister app to
> [BlamixShell](https://github.com/blamixology/blamixshell) (SSH client).

## Why another FTP client?

| | FileZilla | BlamixFiles |
|---|---|---|
| Saved passwords | base64 in `sitemanager.xml` unless you set a master password | Always encrypted (AES-256-GCM, scrypt) |
| Installer | has shipped bundled offers | Clean portable zip, no bundles, no telemetry |
| Editing a remote file | download → external editor → confirm re-upload | Opens in a tab with syntax highlighting; **Ctrl+S** saves to the server |
| Folder sync | highlights differences; no one-click sync | Compare → preview every change → apply; saved sync profiles |
| S3, WebDAV | paid (FileZilla Pro) | included |
| Scripting | no scripted transfers | `blamixfiles get/put/ls/sync` with saved sites |

Also: saves from the editor go to a temp file that is then renamed over the original (no
half-written configs if the connection drops), keep permissions, line endings and encoding,
and warn you if someone else changed the file meanwhile. On 2FA SSH servers you enter the
code once; transfers reuse that login.

## Features (so far)

- **Protocols:** SFTP (password, keys, SSH agent, keyboard-interactive/2FA), SCP (for SSH servers without SFTP),
  FTP, FTPS explicit and implicit, WebDAV/WebDAVS (Nextcloud, ownCloud, NAS), S3-compatible storage
  (AWS, Backblaze B2, Cloudflare R2, Wasabi, Hetzner, MinIO, …): all free, no "Pro" tier
- **Dual-pane browser:** local on the left, server on the right, each with a folder tree; drag & drop
  between panes, from Explorer/Finder, and out to Explorer/Finder/the desktop; filter, sort, rename,
  delete, new folder, permissions (chmod), copy path, folder bookmarks, tabs reopen where you left off
- **Jump hosts:** reach servers behind a bastion (chains work); "Open SSH terminal here" opens a shell
  in the folder you're looking at
- **Checksums:** optionally verify every transfer (SHA-256/MD5 on the server via `sha256sum`, FTP `HASH`,
  or the S3 ETag); sync can compare files by content
- **Watch a folder (Ctrl+Shift+W):** save a file locally and it's on the server a second later; new
  folders are created, `.git`/`node_modules`/editor temp files are skipped, half-written files wait
  until they're complete, and local deletes are never pushed. Also `blamixfiles watch ./site web:/var/www`
- **Big files resume:** S3 multipart and Nextcloud chunked uploads pick up where they stopped, even
  after a crash or restart (parts already on the server are checked against your file first)
- **Command palette (Ctrl+K):** jump to any site, bookmark, sync profile or action
- **Transfer queue:** parallel transfers per site, folders, resume, retry on dropped connections (never on wrong passwords), cancel/retry, pause, "if the file exists" policy (ask, overwrite, if newer, resume, skip), timestamps preserved, speed limits; unfinished transfers are offered again after a restart or crash
- **Compare & sync:** pick two folders, see every change before it happens (upload, download,
  create, delete, conflicts), untick what you don't want, apply. One-way or both ways; mirror
  mode deletes extras only when you ask for it. Save it as a profile and run it again from the
  Sync menu or `blamixfiles sync "Deploy web"`
- **Built-in editor:** 500+ languages (Pygments), nginx/Apache/systemd/.env detection, line numbers, find/replace (regex), go to line, toggle comment, auto-indent; large files open read-only
- **Site manager:** groups, colors, **production flag** (red tab + extra confirmation before deleting)
- **Edit in another app (Shift+F4):** prefer VS Code or Notepad++? The file opens there, and every save
  is uploaded back (asks first; checks nobody changed the server copy in the meantime)
- **Import** from FileZilla, WinSCP (WinSCP.ini or the registry, saved passwords included) and
  BlamixShell (File → Import, or `blamixfiles import filezilla|winscp|blamixshell`)
- **Old FTP servers:** file times from servers that only speak `LIST` are corrected for the server's
  time zone (detected automatically, or set per site), so sync doesn't re-copy unchanged files
- **Portable:** data (vault, known_hosts, settings) lives in `data/` next to the app

## Run from source

```
git clone https://github.com/blamixology/blamixfiles
cd blamixfiles
./dev.sh run        # Git Bash on Windows, macOS, Linux  (or run.bat from cmd)
```

Needs Python 3.10+. The first run creates `.venv` and installs the requirements.

## Command line

```
blamixfiles sites
blamixfiles ls   mysite:/var/www
blamixfiles get  mysite:/var/log/nginx ./logs
blamixfiles put  ./dist sftp://deploy@example.com/var/www --if-exists newer
blamixfiles sync ./dist mysite:/var/www --mirror --dry-run     # preview, then drop --dry-run
blamixfiles sync "Deploy web" --yes                             # a profile saved in the app
blamixfiles watch ./site mysite:/var/www --ignore "*.map"       # upload every change, Ctrl+C to stop
blamixfiles import filezilla
blamixfiles import winscp                                       # WinSCP.ini or the Windows registry
blamixfiles import blamixshell                                  # asks for the BlamixShell master password
```

Saved sites come from the vault (`BLAMIXFILES_VAULT_PASSWORD`, the OS keychain if you enabled it in the app, or a prompt).
Exit codes: `0` ok, `1` some transfers failed, `2` connection/login problem, `3` usage error.

## Terminal UI

For servers with no desktop (works over SSH): `pip install "blamixfiles[tui]"`, then

```
blamixfiles tui
```

Unlock the vault, pick a site (`/` filters, `u` opens an address), then browse this computer on the left and
the server on the right. `Tab` switches pane, `Space` marks files, `F5`/`c` copies to the other pane (with the
same queue as the app: parallel, resume, "file exists" questions), `F7`/`m` new folder, `F2`/`r` rename,
`Del`/`d` delete, `h` hidden files, `x` cancel transfers, `t` retry failed, `Esc` back to the sites.
Sites are added and edited in the app.

## Development

Everything goes through `dev.sh` (Git Bash on Windows, or any bash on macOS/Linux):

| Command | What it does |
|---|---|
| `./dev.sh setup` | create `.venv`, install the app, test and build tools |
| `./dev.sh run` | start the app from source |
| `./dev.sh cli ls mysite:/` | run the command-line tool |
| `./dev.sh test [-k name]` | run the tests |
| `./dev.sh check` | lint + tests + app selftest (what CI runs) |
| `./dev.sh ship` | `check`, then `git pull --rebase` and push |
| `./dev.sh build` | portable app in `dist/BlamixFiles` + zip, checked with a selftest |
| `./dev.sh msi --test` | Windows installer (all users or just you), installed/upgraded/removed as a test |
| `./dev.sh release v0.1.0` | `check`, bump the version, write CHANGELOG from the commits, commit, tag, push (`--dry-run` to preview) |
| `./dev.sh clean` | remove build output and caches |

The tests start real SFTP (paramiko), FTP/FTPS (pyftpdlib), WebDAV (wsgidav) and S3 (moto)
servers in-process, so no daemons are needed (SCP runs against a throwaway OpenSSH `sshd` when
one can be started), and include an end-to-end test that drives the actual window offscreen.
`build.bat` and `run.bat` remain for people using cmd instead of Git Bash.

## How this is built

> 🤖 **DevOps-designed, AI-written.** I'm a DevOps engineer, not a software developer. The code in
> this repository is written by AI (Claude). My part is the product: what to build and why, based on
> day-to-day infrastructure work; the requirements, workflows and UI; testing on real machines,
> reporting what breaks, and deciding what ships.

Security reviews are very welcome. The parts that matter most are the vault encryption
(`blamixfiles/vault.py`), host-key checking (`blamixfiles/core/ssh.py`) and certificate
handling (`blamixfiles/core/backends/ftp.py`).

## Made by

<img src="blamixfiles/assets/blamixology.png" alt="Blamixology Tech" height="36">

BlamixFiles is built and maintained by [Blamixology](https://blamixology.ro/en): custom software, automation and AI solutions.

## Support

BlamixFiles is free and will stay free: no ads, no "Pro" tier. If it saves you time,
you can [buy me a coffee ☕](https://ko-fi.com/blamixology).

## License

MIT
