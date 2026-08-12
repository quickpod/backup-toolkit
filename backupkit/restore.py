r"""List versioned backups and restore files back to a folder.

:func:`list_versions` enumerates the timestamped versions of a *versioned* job.
:func:`restore` copies a version's files back into a chosen destination, with
overwrite protection (existing files are skipped unless ``overwrite=True``).
"""

from __future__ import annotations

import os
import shutil
from datetime import datetime

from .engine import MANIFEST_NAME
from .errors import BackupKitError


def list_versions(job):
    """Return the versions of a versioned *job*, newest first.

    Each item is ``{name, path, mtime}`` where ``name`` is the timestamp folder
    name and ``mtime`` is a :class:`datetime`.
    """
    dest = job.get("destination")
    name = job.get("name")
    if not dest or not name:
        raise BackupKitError("job needs a name and destination to list versions")
    job_root = os.path.join(dest, name)
    if not os.path.isdir(job_root):
        return []
    out = []
    for d in os.listdir(job_root):
        full = os.path.join(job_root, d)
        if not os.path.isdir(full):
            continue
        try:
            mtime = datetime.fromtimestamp(os.path.getmtime(full))
        except OSError:
            mtime = None
        out.append({"name": d, "path": full, "mtime": mtime})
    out.sort(key=lambda v: v["name"], reverse=True)
    return out


def restore(version_dir, dest, subpaths=None, overwrite=False):
    """Copy files from *version_dir* into *dest*.

    Parameters
    ----------
    version_dir : str
        A backup/version directory (its ``manifest.json`` is skipped).
    dest : str
        Where to restore into (created on demand).
    subpaths : list, optional
        Restore only these forward-slash relative paths; default is everything.
    overwrite : bool
        When False (default) existing destination files are left untouched and
        counted as ``skipped``; when True they are replaced.

    Returns a report dict ``{restored, skipped, bytes, errors}``.
    """
    if not os.path.isdir(version_dir):
        raise BackupKitError(f"version directory not found: {version_dir}")

    wanted = set(subpaths) if subpaths else None
    report = {"restored": 0, "skipped": 0, "bytes": 0, "errors": []}

    for dirpath, _dirs, filenames in os.walk(version_dir):
        for fn in filenames:
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, version_dir).replace(os.sep, "/")
            if rel == MANIFEST_NAME:
                continue
            if wanted is not None and rel not in wanted:
                continue
            target = os.path.join(dest, *rel.split("/"))
            if os.path.exists(target) and not overwrite:
                report["skipped"] += 1
                continue
            try:
                os.makedirs(os.path.dirname(target), exist_ok=True)
                shutil.copy2(full, target)
                report["restored"] += 1
                try:
                    report["bytes"] += os.path.getsize(target)
                except OSError:
                    pass
            except OSError as exc:
                report["errors"].append(f"{rel}: {exc}")
    return report
