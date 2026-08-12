"""Error types for backupkit."""


class BackupKitError(Exception):
    """Raised for any recoverable failure in a backupkit operation.

    All public functions raise this (and only this) on failure so callers
    -- including the CLI and the GUI -- have a single exception to catch.
    Unexpected/OS errors that happen per-file are collected into the run
    report's ``errors`` list instead, so one bad file never aborts a backup.
    """
