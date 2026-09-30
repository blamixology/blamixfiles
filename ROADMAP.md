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
| 🗓 | Windows MSI installer, code signing |

## Next

| | Feature |
|---|---|
| ✅ | Folder compare and sync with a preview of every change (one-way, mirror, two-way) |
| ✅ | Saved sync profiles, `blamixfiles sync <profile>` |
| 🗓 | Checksum verification after transfers |
| 🗓 | S3-compatible storage (AWS, MinIO, Backblaze B2, Wasabi, Hetzner, R2) |
| 🗓 | WebDAV (Nextcloud) |
| 🗓 | SCP |
| 🗓 | Jump hosts, "Open in BlamixShell" |
| 🗓 | Drag files from the remote pane to the desktop |
| 🗓 | Bookmarks, command palette, tab restore |

## Later / maybe

| | Feature |
|---|---|
| 💡 | Google Drive, Dropbox, OneDrive |
| 💡 | Watch a local folder and upload changes |
| 💡 | Terminal UI (like BlamixShell's) |
