"""backupkit -- a pure-stdlib backup & sync library for Backup Toolkit.

Everything runs locally; nothing is ever uploaded.  Public API::

    from backupkit import JobStore, run_backup, verify_backup, list_versions, restore

    store = JobStore()                       # per-user jobs.json (parameterizable)
    store.add_job({"name": "docs", "sources": ["C:/Users/me/Documents"],
                   "destination": "E:/Backups", "mode": "versioned", "keep": 7})
    report = run_backup(store.get_job("docs"))
    results = verify_backup(report["version_dir"])

All operations raise :class:`BackupKitError` on invalid input; per-file OS
problems are collected into a run's ``report['errors']`` instead of aborting.
Scheduling (:mod:`backupkit.schedule`) is Windows-only; everything else is
cross-platform.  See the CLI at ``python -m backupkit --help``.
"""

from __future__ import annotations

from .errors import BackupKitError
from .config import (
    JobStore,
    normalize_job,
    require_runnable,
    default_store_path,
    add_job,
    get_job,
    list_jobs,
    remove_job,
    update_job,
    MODES,
)
from .engine import run_backup, iter_files, sha256_file, MANIFEST_NAME
from .verify import verify_backup
from .restore import list_versions, restore
from . import schedule

__version__ = "1.0.0"

__all__ = [
    "BackupKitError",
    "JobStore",
    "normalize_job",
    "require_runnable",
    "default_store_path",
    "add_job",
    "get_job",
    "list_jobs",
    "remove_job",
    "update_job",
    "MODES",
    "run_backup",
    "iter_files",
    "sha256_file",
    "MANIFEST_NAME",
    "verify_backup",
    "list_versions",
    "restore",
    "schedule",
    "__version__",
]
