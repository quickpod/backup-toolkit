# Backup Toolkit

A fast, **offline**, **100% open-source** backup & sync toolkit for Windows. Nothing is uploaded anywhere. Built entirely by AI with human testing and guidance, and published on [QuickOpen](https://quickopen.ai/projects/backup-toolkit).

> **100% AI-built and open source.** Apache-2.0.

## What it does

Define backup jobs that copy folders to a local disk, USB drive or network share on a schedule, keeping timestamped versions and pruning old ones by count or age. Mirror or incremental modes, include/exclude filters, and SHA256 integrity verification of every backed-up file with a restore browser. Everything runs on your machine; your data never leaves it.

## Install

Download **`BackupToolkit-Setup.exe`** from the [QuickOpen page](https://quickopen.ai/projects/backup-toolkit) or the [GitHub release](https://github.com/quickpod/backup-toolkit/releases/latest) and double-click it. It installs per-user, adds Desktop and Start Menu shortcuts, and can optionally trust the QuickOpen Root CA. Authenticode-signed by the QuickOpen Code Signing CA — verify at [quickopen.ai/trust](https://quickopen.ai/trust).

## Run from source

```sh
python backup_app.py          # GUI
python -m backupkit --help    # CLI
```

Pure Python standard library — no third-party dependencies.

## Features

- **Three backup modes**
  - **Incremental** — copy only new/changed files (by size + mtime); never deletes anything in the destination.
  - **Mirror** — make the destination match the sources, **deleting** files in the destination that are no longer in a source. (The GUI confirms first; the CLI documents it.)
  - **Versioned** — write each run to `dest/<job>/<timestamp>/`, **hard-linking** unchanged files from the previous version so snapshots cost almost no disk, then prune old versions by **keep-count** and/or **keep-days**.
- **SHA-256 manifests** — every backup writes a `manifest.json` mapping each file to its size, mtime and hash. `verify` re-hashes and reports any missing or tampered files.
- **Restore browser** — list a versioned job's snapshots and restore a whole version (or selected paths) back to any folder, with overwrite protection.
- **Include / exclude filters** — glob patterns (`*.tmp`, `cache/*`, …) choose exactly what gets backed up.
- **Multiple sources per job** — each source lands in its own subfolder of the destination, to a local disk, USB drive or UNC network share.
- **Windows scheduling** — register a job as a Scheduled Task (via `schtasks`). Scheduling is Windows-only; the engine, CLI and GUI runs work on every platform.
- **tkinter GUI** — sidebar sections for Jobs, Run, Versions/Restore, Verify and Schedule; threaded runs with live progress and cancel; light/dark themes.
- **Fully offline** — nothing is ever uploaded; all work happens on your machine.

## CLI examples

```sh
# Create a versioned job (keep the 7 newest snapshots, skip temp files)
python -m backupkit job add docs \
    --source ~/Documents --source ~/Pictures \
    --dest /mnt/usb/Backups --mode versioned --keep 7 --exclude "*.tmp"

python -m backupkit job list                 # show all jobs
python -m backupkit run docs                 # run one job
python -m backupkit run --all                # run every enabled job

python -m backupkit versions docs            # list snapshots of a versioned job
python -m backupkit verify docs              # re-hash newest backup vs its manifest
python -m backupkit verify /mnt/usb/Backups/docs/20260812-101500

# Restore the newest (or a chosen) version into a folder
python -m backupkit restore docs --dest ~/Restored
python -m backupkit restore docs --version 20260812-101500 --dest ~/Restored --overwrite

# Windows-only: register/remove a Scheduled Task (prints the command elsewhere)
python -m backupkit schedule docs --interval 60m
python -m backupkit unschedule docs

# Point at a specific jobs.json instead of the per-user default
python -m backupkit --store ./jobs.json job list
```

Jobs are stored as JSON at `%LOCALAPPDATA%\BackupToolkit\jobs.json` (Windows) or `~/.backuptoolkit/jobs.json` elsewhere.

## License

Apache-2.0 — see [LICENSE](LICENSE). A 100% AI-built project published on QuickOpen.
