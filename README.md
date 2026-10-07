# BlamixFiles

[![tests](https://github.com/blamixology/blamixfiles/actions/workflows/tests.yml/badge.svg?branch=main)](https://github.com/blamixology/blamixfiles/actions/workflows/tests.yml)
[![build](https://github.com/blamixology/blamixfiles/actions/workflows/release.yml/badge.svg)](https://github.com/blamixology/blamixfiles/actions/workflows/release.yml)
[![release](https://img.shields.io/github/v/release/blamixology/blamixfiles)](https://github.com/blamixology/blamixfiles/releases/latest)
[![downloads](https://img.shields.io/github/downloads/blamixology/blamixfiles/total)](https://github.com/blamixology/blamixfiles/releases)
[![license](https://img.shields.io/github/license/blamixology/blamixfiles)](LICENSE)
[![python](https://img.shields.io/badge/python-3.10%2B-blue)](pyproject.toml)
[![platforms](https://img.shields.io/badge/platforms-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey)](https://github.com/blamixology/blamixfiles/releases/latest)

A free, open-source file transfer client: a modern FileZilla alternative.
SFTP, SCP, FTP/FTPS, WebDAV, S3, SMB, Google Drive, Dropbox and OneDrive in one portable app, a **built-in editor** that saves straight to
the server, and an **encrypted password vault**. Windows, macOS and Linux, plus a CLI.

> Sister app to [BlamixShell](https://github.com/blamixology/blamixshell) (SSH client): same vault format,
> same themes, same look.

## Why another FTP client?

| | FileZilla | BlamixFiles |
|---|---|---|
| Saved passwords | base64 in `sitemanager.xml` unless you set a master password | Always encrypted (AES-256-GCM, scrypt) |
| Installer | has shipped bundled offers | Clean portable zip, no bundles, no telemetry |
| Editing a remote file | download → external editor → confirm re-upload | Opens in a tab with syntax highlighting; **Ctrl+S** saves to the server |
| Folder sync | highlights differences; no one-click sync | Compare → preview every change → apply; saved sync profiles |
| S3, WebDAV | paid (FileZilla Pro) | included |
| Scripting | no scripted transfers | `blamixfiles get/put/ls/mv/rm/mkdir/sync` with saved sites, `--json` output |

Also: saves from the editor go to a temp file that is then renamed over the original (no
half-written configs if the connection drops), keep permissions, line endings and encoding,
and warn you if someone else changed the file meanwhile. On 2FA SSH servers you enter the
code once; transfers reuse that login.

## Features (so far)

- **Protocols:** SFTP (password, keys, SSH agent, keyboard-interactive/2FA), SCP (for SSH servers without SFTP),
  FTP, FTPS explicit and implicit, WebDAV/WebDAVS (Nextcloud, ownCloud, NAS), S3-compatible storage
  (AWS, Backblaze B2, Cloudflare R2, Wasabi, Hetzner, MinIO, …), SMB (Windows shares, NAS boxes),
  Google Drive, Dropbox and OneDrive (sign in with the browser): all free, no "Pro" tier
- **Search a server (Ctrl+Shift+F):** by name (wildcards), size and age, in a folder and everything below it;
  open the result's folder or download the hits. Right-click a folder → **Calculate size**
- **Bulk rename:** select several files → *Rename several…*: find/replace (or regex), change case, number
  them (`photo-{n:03}{ext}`), with a preview that flags clashes before anything is renamed
- **Compare two files:** right-click → *Compare with the other side* shows the differences between the
  file here and the one on the server (or between two servers)
- **Server to server:** right-click → *Copy to another server…* (or to another folder on the same one)
- **Queue order:** right-click a waiting transfer → move it to the top, up, down or to the bottom
- **Dual-pane browser:** local on the left, server on the right, each with a folder tree; drag & drop
  between panes, from Explorer/Finder, and out to Explorer/Finder/the desktop; filter, sort, rename,
  delete, new folder, permissions (chmod), copy path, folder bookmarks, tabs reopen where you left off
- **Jump hosts:** reach servers behind a bastion (chains work); "Open SSH terminal here" opens a shell
  in the folder you're looking at
- **Checksums:** optionally verify every transfer (SHA-256/MD5 on the server via `sha256sum`, FTP `HASH`,
  or the S3 ETag); sync can compare files by content
- **Scheduled syncs:** Sync → *Schedule profile* runs a saved profile daily or every few hours with the app
  closed (Windows Task Scheduler / cron), and *After sync…* runs a command or calls a webhook when it's done
- **SSH keys:** right-click an SSH site → *Set up key login…* makes an Ed25519 key, puts it on the server
  (like `ssh-copy-id`) and switches the site to it
- **Diagnostics:** Help → *Diagnostics…* collects versions, settings (no passwords) and recent transfers for a
  problem report; after a crash the app offers the crash report next time. Nothing is ever sent by itself
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
- **Speed limits and a log:** a limit for all transfers (queue toolbar) and one per server (site dialog),
  and a plain-text transfer log (View → Transfer log…, `blamixfiles log`)
- **Themes:** Midnight, Graphite, Nord, Solarized Dark, Light, High contrast, or follow the system; they
  switch live (View → Theme)
- **Keychain:** unlock the vault with Windows Credential Manager, macOS Keychain or the Linux Secret Service
  (File → Unlock with …)
- **Updates:** a quiet daily check on GitHub Releases and one-click install (Windows installer and portable,
  Linux and macOS when the app sits in a folder you can write to); "Install update from file…" with a
  SHA-256 check for computers without internet; admins can switch the check off (`policy.ini`)
- **Keyboard and screen readers:** every control has a name, **F6** switches between the two lists,
  **Ctrl+K** reaches every action
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
blamixfiles mkdir mysite:/var/www/new/deeper                    # with parents
blamixfiles mv   mysite:/var/www/a.txt /var/www/archive/a.txt   # rename or move on the same server
blamixfiles rm   -r --yes mysite:/var/www/old                   # folders need -r; scripts need --yes
blamixfiles log  -n 20                                          # the latest finished transfers
blamixfiles find mysite:/var/www --name "*.log" --larger 10M    # search (also --newer 7d, --older 30d)
blamixfiles du   mysite:/var/www                                # folder size
blamixfiles keygen && blamixfiles copy-id mysite                # make an SSH key, install it on the server
blamixfiles schedule "Deploy web" --daily 02:30                 # also --every 6, --off, --list
blamixfiles login "My Drive"                                    # sign in to a Google Drive/Dropbox/OneDrive site
blamixfiles diagnostics                                         # for a problem report
blamixfiles get  mysite:/var/log/nginx ./logs
blamixfiles put  ./dist sftp://deploy@example.com/var/www --if-exists newer
blamixfiles sync ./dist mysite:/var/www --mirror --dry-run     # preview, then drop --dry-run
blamixfiles sync "Deploy web" --yes                             # a profile saved in the app
blamixfiles sync ./dist mysite:/var/www --json                  # plan and result as one JSON document
blamixfiles watch ./site mysite:/var/www --ignore "*.map"       # upload every change, Ctrl+C to stop
blamixfiles import filezilla
blamixfiles import winscp                                       # WinSCP.ini or the Windows registry
blamixfiles import blamixshell                                  # asks for the BlamixShell master password
```

Saved sites come from the vault (`BLAMIXFILES_VAULT_PASSWORD`, the OS keychain if you enabled it in the app, or a prompt).
Exit codes: `0` ok, `1` some transfers failed, `2` connection/login problem, `3` usage error.

## Cloud drives (Google Drive, Dropbox, OneDrive)

Add a site, pick the provider and press **Sign in…**: the provider's page opens in your browser, and the
app gets a token it keeps in the encrypted vault (refreshed automatically). Each provider needs an app
registration: release builds have one built in when the repository has them as secrets
(`BLAMIXFILES_GDRIVE_CLIENT_ID` / `_SECRET`, `BLAMIXFILES_DROPBOX_CLIENT_ID`, `BLAMIXFILES_ONEDRIVE_CLIENT_ID`);
otherwise enter your own in the site (Google Cloud console: OAuth client *Desktop app*; Dropbox app console:
redirect `http://127.0.0.1:53682/`; Microsoft Entra: public client, redirect `http://localhost`).
Deleting moves to the provider's trash / recycle bin. Google Docs, Sheets and Slides are listed but can't
be downloaded as files (export them in Drive).

**SMB:** the share is part of the start folder (`/Public`, `/Media/Films`); user names can be
`DOMAIN\user` or `user@domain`.

## Terminal UI

For servers with no desktop (works over SSH): `pip install "blamixfiles[tui]"`, then

```
blamixfiles tui
```

Unlock the vault, pick a site (`/` filters, `u` opens an address, `n`/`e`/`d` add, edit, delete a site), then
browse this computer on the left and the server on the right. `Tab` switches pane, `Space` marks files,
`F5`/`c` copies to the other pane (with the same queue as the app: parallel, resume, "file exists" questions),
`F7`/`m` new folder, `F2`/`r` rename, `Del`/`d` delete, `v` view a text file, `e`/`F4` edit it in `$EDITOR`
(uploaded again on save, with a check that nobody changed it meanwhile), `h` hidden files, `x` cancel
transfers, `t` retry failed, `Esc` back to the sites. SSH two-factor prompts are asked on screen. Themes are
the app's (`Ctrl+P` → "Change theme").

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
