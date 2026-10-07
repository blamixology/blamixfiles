# Changelog

## Unreleased

## 1.0.1 - 2026-10-07

- Keychain on macOS/Linux: store the password as ASCII (macOS prints non-ASCII passwords as hex) (1bb462d)

## 1.0.0 - 2026-10-07

- 1.0: CLI mkdir/rm/mv/log and --json, per-site speed limits, transfer log, TUI 2FA/view/edit/sites/themes, Linux and macOS updates, accessibility, more tests (09075ac)

## 0.6.3 - 2026-10-06

- Remove the bright 1px line along the tab bar (Fusion frame colors follow the theme background) (013a8df)

## 0.6.2 - 2026-10-06

- Show the version in the sidebar footer (click for About) (886e171)

## 0.6.1 - 2026-10-06

- TUI: keep the cursor on refresh (fixes flaky copy test); updater script: escape the exe path (18a7222)

## 0.6.0 - 2026-10-06

- Folder tree and server list: one-piece selection/hover highlight (no seam, no accent block) (77854d4)
- Tabs: draw the tab icon ourselves so icon, label and close button share one line (7fb34a4)

## 0.5.0 - 2026-10-06

- Live theme switching (no restart), follow the OS light/dark setting while running (f12eb9f)

## 0.4.1 - 2026-10-06

- MSI: drop the desktop shortcut (Desktop on another drive broke upgrades, error 1307); create it from the app; updater retries without rollback (09bab84)

## 0.4.0 - 2026-10-06

- Terminal UI: blamixfiles tui (browse, copy, rename, delete over SSH) (fb06ab1)
- README: badges (tests, build, release, downloads, license, python, platforms) (92e89fe)

## 0.3.0 - 2026-10-05

- Updates: daily check, one-click install (MSI/portable), install from file (b6160b3)

## 0.2.0 - 2026-10-05

- Six themes; hide the server list (Ctrl+B); own tab close button (12d0bd6)
- File list: dotfiles were drawn black on dark; use the muted theme color (c2e3858)

## 0.1.0 - 2026-10-05

First release: SFTP, SCP, FTP/FTPS, WebDAV and S3 client with a dual-pane browser, a transfer queue that
survives restarts (with speed limits), a built-in editor that saves straight to the server,
compare & sync with a preview of every change, an encrypted vault, FileZilla import and a CLI.
Also: jump hosts, folder trees, drag to the desktop, checksum verification, bookmarks, a
command palette, tab restore, FTP keep-alive and a Windows installer.
Watch a local folder and upload every change (app and `blamixfiles watch`); big S3 and Nextcloud
uploads resume after a crash or restart; About dialog with "made by Blamixology".
Import from WinSCP and BlamixShell; FTP server time zone correction; edit files in another app
(VS Code, Notepad++, …) with upload on save.
Unlock the vault with the OS keychain (Windows Credential Manager, macOS Keychain, Linux Secret Service;
the CLI uses it too); six themes like BlamixShell plus "follow the system" (View → Theme); the server list can be hidden (Ctrl+B).

- dev.sh release: force UTF-8 so commit subjects with arrows don't crash on Windows (9cecdfa)
- Unlock vault with OS keychain; light and system theme; Open in BlamixShell prepared (commented out) (06ecce5)
- Import from WinSCP and BlamixShell; FTP server time zone; edit in another app (ff46754)
- Watch folder & upload changes; resumable S3 multipart and Nextcloud chunked uploads (059e729)
- About: made by Blamixology (logo + link); CI: Node 24 actions, MSI and portable apps as separate downloads (ed8e1d7)
- Jump hosts, folder trees, drag to desktop, tab restore, checksums, MSI, bookmarks, Ctrl+K (18230d0)
- SCP, WebDAV and S3 support; clearer selftest (1d7d1b9)
- SCP, WebDAV and S3 support (fc8b544)
- Compare & sync with preview, sync profiles, blamixfiles sync (6e4f731)
- Saved transfer queue, speed limits, release builds for Windows/macOS/Linux (22e6112)
- BlamixFiles 0.1 dev: SFTP/FTP/FTPS client, queue, editor, vault, CLI, dev.sh (650f015)
- BlamixFiles 0.1 dev: SFTP/FTP/FTPS client, queue, editor, vault, CLI, dev.sh (6df8140)
Updates: a quiet daily check on GitHub Releases, one-click install for the MSI and portable Windows builds, and
"Install update from file…" (with SHA-256 check) for computers without internet; admins can switch the check off
(`BLAMIXFILES_UPDATE_CHECK=off` or `policy.ini`).
Terminal UI (`blamixfiles tui`): browse and copy between this computer and a server from any terminal, even over SSH.
Installer: no desktop shortcut in the MSI any more (a Desktop folder on another drive made upgrades fail with error 1307);
File → Create desktop shortcut does it from the app, and the in-app updater retries once without rollback files.
1.0: CLI `mkdir`, `rm`, `mv`, `log` and `--json` results; speed limits per site and a transfer log (View → Transfer log…);
the terminal UI got SSH two-factor prompts, view/edit files, site management and the app's themes; updates on
Linux and macOS; named controls for screen readers and F6 to switch lists; more tests (MSI upgrade through the
updater's own script, keychain commands, Linux swap script).
1.1: Google Drive, Dropbox, OneDrive and SMB; search a server and folder sizes; bulk rename; compare two files;
server-to-server copy; reorder the queue; scheduled syncs with after-sync commands and webhooks; a live list for
folder watches; SSH key setup (also `keygen` / `copy-id`); diagnostics and crash reports. New CLI commands:
`find`, `du`, `keygen`, `copy-id`, `schedule`, `login`, `diagnostics`.
