r"""Integrity verification against a backup's ``manifest.json``.

:func:`verify_backup` re-hashes every file listed in a manifest and reports the
ones that are missing or whose contents no longer match.  Accepts either a
backup directory (it finds ``manifest.json`` inside), a path to a manifest file,
or an already-loaded manifest dict (with a sibling ``base_dir``).
"""

from __future__ import annotations

import json
import os

from .engine import MANIFEST_NAME, sha256_file
from .errors import BackupKitError


def _load_manifest(target):
    """Return ``(manifest_dict, base_dir)`` from a dir / file / dict."""
    if isinstance(target, dict):
        base = target.get("base_dir") or os.getcwd()
        return target, base
    if not isinstance(target, str):
        raise BackupKitError("verify target must be a path or manifest dict")
    if os.path.isdir(target):
        path = os.path.join(target, MANIFEST_NAME)
    else:
        path = target
    if not os.path.isfile(path):
        raise BackupKitError(f"no manifest found at {path}")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        raise BackupKitError(f"cannot read manifest {path}: {exc}")
    return data, os.path.dirname(os.path.abspath(path))


def verify_backup(manifest_or_dir):
    """Verify a backup, returning a results dict::

        {ok, missing, mismatched, errors, total, base_dir}

    ``ok``/``missing``/``mismatched`` are lists of relative paths; ``errors`` is
    a list of ``"path: message"`` strings for files that could not be read.
    """
    data, base = _load_manifest(manifest_or_dir)
    files = data.get("files", data) if isinstance(data, dict) else {}
    if not isinstance(files, dict):
        raise BackupKitError("manifest has no 'files' mapping")

    results = {
        "ok": [],
        "missing": [],
        "mismatched": [],
        "errors": [],
        "total": len(files),
        "base_dir": base,
    }
    for rel, meta in sorted(files.items()):
        full = os.path.join(base, *rel.split("/"))
        if not os.path.exists(full):
            results["missing"].append(rel)
            continue
        try:
            digest = sha256_file(full)
        except OSError as exc:
            results["errors"].append(f"{rel}: {exc}")
            continue
        expected = (meta or {}).get("sha256")
        if expected and digest != expected:
            results["mismatched"].append(rel)
        else:
            results["ok"].append(rel)
    results["passed"] = not (results["missing"] or results["mismatched"]
                             or results["errors"])
    return results
