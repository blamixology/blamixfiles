# Roadmap

Legend: ✅ done · 🚧 in progress · 🗓 planned · 💡 later / maybe

## 1.0

| | Feature |
|---|---|
| ✅ | SFTP, FTP, FTPS (explicit + implicit) with host-key and certificate checks |
| ✅ | Dual-pane browser, drag & drop, file operations |
| ✅ | Transfer queue: parallel, resume, retry, overwrite policies |
| ✅ | Built-in editor with syntax highlighting, save to server with conflict check |
| ✅ | Encrypted site vault, FileZilla import |
| ✅ | CLI: `ls`, `get`, `put`, `sites`, `import` |
| ✅ | Queue survives restarts (saved to disk) |
| ✅ | Speed limits (upload/download, also `--limit` in the CLI) |
| ✅ | Portable builds for Windows, macOS and Linux from CI, `./dev.sh release` |
| ✅ | Windows MSI installer (all users or just me) |
| 🗓 | Code signing (Windows, macOS): not yet. Options: SignPath Foundation (free for OSS), Azure Artifact Signing (companies in the EU), an OV certificate on a cloud HSM; Apple Developer ID for macOS |

## Next

| | Feature |
|---|---|
| ✅ | Folder compare and sync with a preview of every change (one-way, mirror, two-way) |
| ✅ | Saved sync profiles, `blamixfiles sync <profile>` |
| ✅ | Checksum verification after transfers; sync by content |
| ✅ | S3-compatible storage (AWS, MinIO, Backblaze B2, Wasabi, Hetzner, R2) |
| ✅ | WebDAV (Nextcloud, ownCloud, NAS) |
| ✅ | SCP |
| ✅ | Nextcloud chunked uploads for very large files; resumable S3 multipart uploads |
| ✅ | Jump hosts, "Open SSH terminal here" |
| ✅ | Drag files from the remote pane to the desktop; folder trees |
| ✅ | Bookmarks, command palette, tab restore |
| ✅ | Watch a local folder and upload changes (app and `blamixfiles watch`) |
| ✅ | Import from WinSCP and BlamixShell |
| ✅ | FTP server time zone (auto-detected with MDTM, or set per site) |
| ✅ | Edit in another app (VS Code, Notepad++, …) with upload on save and conflict check |
| ✅ | Unlock the vault with the OS keychain (Windows Credential Manager, macOS Keychain, Linux Secret Service) |
| ✅ | Themes (Midnight, Graphite, Nord, Solarized Dark, Light, High contrast, or follow the system): View → Theme, switches live |
| ✅ | Updates: daily check on GitHub Releases, one-click install (MSI or portable), "Install update from file…" for offline PCs |
| ✅ | Terminal UI: `blamixfiles tui`, with SSH two-factor prompts, view/edit files, add/edit/delete sites and the app's themes |
| ✅ | Updates on Linux (tar.gz) and macOS (.app) when the app sits in a folder you can write to |
| ✅ | CLI: `mkdir`, `rm`, `mv`, `log`, and `--json` for get / put / sync results |
| ✅ | Speed limits per site; a transfer log (View → Transfer log…, `blamixfiles log`) |
| ✅ | Accessibility: named controls for screen readers, F6 between the two lists |
| ✅ | Tests: real MSI upgrade through the in-app updater's own script, keychain commands, Linux swap script |
| 🗓 | "Open in BlamixShell" (needs BlamixShell to accept a site on its command line; the code is ready, commented out: search for `TODO(BlamixShell)`) |

## 1.1

| | Feature |
|---|---|
| ✅ | Search a server by name, size and age (Ctrl+Shift+F, `blamixfiles find`); folder sizes (`blamixfiles du`) |
| ✅ | Bulk rename with a preview (find/replace, regex, case, numbering) |
| ✅ | Compare two files (here and on the server, or between servers) |
| ✅ | Server-to-server copy (through a temporary file on this computer) |
| ✅ | Reorder the transfer queue |
| ✅ | Scheduled syncs (Task Scheduler / cron, `blamixfiles schedule`) and after-sync commands / webhooks |
| ✅ | Live list of what a folder watch uploaded |
| ✅ | SSH key setup: make a key, install it on the server, switch the site to it (`keygen`, `copy-id`) |
| ✅ | Google Drive, Dropbox, OneDrive (browser sign-in) and SMB shares |
| ✅ | Diagnostics and crash reports (never sent automatically) |

## Later / maybe

| | Feature |
|---|---|
