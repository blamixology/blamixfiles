# BlamixFiles

A free, open-source file transfer client: a modern FileZilla alternative.
SFTP, FTP and FTPS in one portable app, a **built-in editor** that saves straight to
the server, and an **encrypted password vault**. Windows, macOS and Linux, plus a CLI.

> 🚧 **Work in progress.** Early development, not released yet. Sister app to
> [BlamixShell](https://github.com/blamixology/blamixshell) (SSH client).

## Why another FTP client?

| | FileZilla | BlamixFiles |
|---|---|---|
| Saved passwords | base64 in `sitemanager.xml` unless you set a master password | Always encrypted (AES-256-GCM, scrypt) |
| Installer | has shipped bundled offers | Clean portable zip, no bundles, no telemetry |
| Editing a remote file | download → external editor → confirm re-upload | Opens in a tab with syntax highlighting; **Ctrl+S** saves to the server |
| Scripting | no scripted transfers | `blamixfiles get/put/ls` with saved sites |

Also: saves from the editor go to a temp file that is then renamed over the original (no
half-written configs if the connection drops), keep permissions, line endings and encoding,
and warn you if someone else changed the file meanwhile. On 2FA SSH servers you enter the
code once; transfers reuse that login.

## Features (so far)

- **Protocols:** SFTP (password, keys, SSH agent, keyboard-interactive/2FA), FTP, FTPS explicit and implicit
- **Dual-pane browser:** drag & drop between panes and from Explorer/Finder, filter, sort, rename, delete, new folder, permissions (chmod), copy path
- **Transfer queue:** parallel transfers per site, folders, resume, retry on dropped connections (never on wrong passwords), cancel/retry, pause, "if the file exists" policy (ask, overwrite, if newer, resume, skip), timestamps preserved
- **Built-in editor:** 500+ languages (Pygments), nginx/Apache/systemd/.env detection, line numbers, find/replace (regex), go to line, toggle comment, auto-indent; large files open read-only
- **Site manager:** groups, colors, **production flag** (red tab + extra confirmation before deleting)
- **Import from FileZilla** (File → Import, or `blamixfiles import filezilla`)
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
blamixfiles import filezilla
```

Saved sites come from the vault (`BLAMIXFILES_VAULT_PASSWORD` or a prompt).
Exit codes: `0` ok, `1` some transfers failed, `2` connection/login problem, `3` usage error.

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
| `./dev.sh release v0.1.0` | `check`, bump the version, write CHANGELOG from the commits, commit, tag, push (`--dry-run` to preview) |
| `./dev.sh clean` | remove build output and caches |

The tests start real SFTP (paramiko) and FTP/FTPS (pyftpdlib) servers in-process, so no
daemons are needed, and include an end-to-end test that drives the actual window offscreen.
`build.bat` and `run.bat` remain for people using cmd instead of Git Bash.

## How this is built

> 🤖 **DevOps-designed, AI-written.** I'm a DevOps engineer, not a software developer. The code in
> this repository is written by AI (Claude). My part is the product: what to build and why, based on
> day-to-day infrastructure work; the requirements, workflows and UI; testing on real machines,
> reporting what breaks, and deciding what ships.

Security reviews are very welcome. The parts that matter most are the vault encryption
(`blamixfiles/vault.py`), host-key checking (`blamixfiles/core/ssh.py`) and certificate
handling (`blamixfiles/core/backends/ftp.py`).

## Support

BlamixFiles is free and will stay free: no ads, no "Pro" tier. If it saves you time,
you can [buy me a coffee ☕](https://ko-fi.com/blamixology).

## License

MIT
