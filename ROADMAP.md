# Roadmap

Legend: ✅ done · 🚧 in progress · 🗓 planned · 💡 later / maybe

## First release (0.1)

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
| ✅ | Themes (Midnight, Graphite, Nord, Solarized Dark, Light, High contrast, or follow the system): View → Theme, applies on the next start |
| ✅ | Updates: daily check on GitHub Releases, one-click install (MSI or portable), "Install update from file…" for offline PCs |
| 🗓 | "Open in BlamixShell" (needs BlamixShell to accept a site on its command line; the code is ready, commented out: search for `TODO(BlamixShell)`) |

## Later / maybe

| | Feature |
|---|---|
| 💡 | Google Drive, Dropbox, OneDrive |
| ✅ | Terminal UI: `blamixfiles tui` (browse, copy, rename, delete over SSH) |
