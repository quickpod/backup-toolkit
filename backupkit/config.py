r"""Persistent backup-job store for Backup Toolkit.

Jobs are kept as JSON.  On Windows the file lives at
``%LOCALAPPDATA%\BackupToolkit\jobs.json``; elsewhere it falls back to
``~/.backuptoolkit/jobs.json``.  The store path is parameterizable so tests can
point it at a temp directory and never touch the user's real jobs.

A *job* is a plain ``dict`` with these keys::

    name         str   -- unique job name (case-sensitive)
    sources      list  -- folders to back up
    destination  str   -- target folder / USB drive / UNC path
    mode         str   -- 'mirror' | 'incremental' | 'versioned'
    includes     list  -- glob patterns; if non-empty, ONLY matches are backed up
    excludes     list  -- glob patterns; matches are skipped
    keep         int   -- versioned: keep newest N versions (0 = unlimited)
    keep_days    int   -- versioned: also drop versions older than N days (0 = off)
    enabled      bool  -- whether scheduled/`run --all` picks it up
    schedule     str   -- opaque schedule spec (e.g. "60m", "daily"); stored only

Use :func:`normalize_job` to fill defaults / validate, and :class:`JobStore`
(or the module-level CRUD helpers) to persist.
"""

from __future__ import annotations

import json
import os

from .errors import BackupKitError

APP_DIRNAME = "BackupToolkit"
STORE_NAME = "jobs.json"

MODES = ("mirror", "incremental", "versioned")

_JOB_DEFAULTS = {
    "sources": list,
    "destination": "",
    "mode": "incremental",
    "includes": list,
    "excludes": list,
    "keep": 0,
    "keep_days": 0,
    "enabled": True,
    "schedule": None,
}


def default_store_dir():
    r"""Directory that holds ``jobs.json`` (created on demand).

    ``%LOCALAPPDATA%\BackupToolkit`` on Windows, ``~/.backuptoolkit`` otherwise.
    """
    local = os.environ.get("LOCALAPPDATA")
    if local and os.name == "nt":
        return os.path.join(local, APP_DIRNAME)
    return os.path.join(os.path.expanduser("~"), "." + APP_DIRNAME.lower())


def default_store_path():
    return os.path.join(default_store_dir(), STORE_NAME)


def _as_str_list(value):
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return [str(v) for v in value if str(v).strip()]


def _as_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def normalize_job(job):
    """Return a validated copy of *job* with every key present and typed.

    Raises :class:`BackupKitError` for a missing name or an unknown mode.
    """
    if not isinstance(job, dict):
        raise BackupKitError("a job must be a dictionary")
    name = str(job.get("name", "")).strip()
    if not name:
        raise BackupKitError("a job needs a non-empty 'name'")
    out = {"name": name}
    for key, default in _JOB_DEFAULTS.items():
        val = job.get(key, default() if callable(default) else default)
        if key in ("sources", "includes", "excludes"):
            out[key] = _as_str_list(val)
        elif key in ("keep", "keep_days"):
            out[key] = max(0, _as_int(val, 0))
        elif key == "enabled":
            out[key] = bool(val)
        elif key == "mode":
            mode = str(val or "incremental").lower()
            if mode not in MODES:
                raise BackupKitError(
                    f"unknown mode {mode!r} (expected one of {', '.join(MODES)})")
            out[key] = mode
        elif key == "schedule":
            out[key] = None if val in (None, "") else str(val)
        else:
            out[key] = str(val or "")
    return out


def require_runnable(job):
    """Normalize *job* and assert it has what a backup run needs."""
    job = normalize_job(job)
    if not job["sources"]:
        raise BackupKitError(f"job {job['name']!r} has no source folders")
    if not job["destination"]:
        raise BackupKitError(f"job {job['name']!r} has no destination")
    return job


class JobStore:
    """CRUD access to the jobs JSON file.

    ``path=None`` uses the per-user default location; pass an explicit path in
    tests.  Reads are defensive (a corrupt file yields an empty store rather
    than crashing); writes are atomic via a temp file + ``os.replace``.
    """

    def __init__(self, path=None):
        self.path = path or default_store_path()

    # -- low level ------------------------------------------------------
    def load(self):
        """Return ``{name: job}`` for every stored job (never raises)."""
        jobs = {}
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            return jobs
        except (OSError, ValueError):
            return jobs
        raw = data.get("jobs", data) if isinstance(data, dict) else data
        if isinstance(raw, dict):
            raw = list(raw.values())
        if not isinstance(raw, list):
            return jobs
        for item in raw:
            try:
                job = normalize_job(item)
            except BackupKitError:
                continue
            jobs[job["name"]] = job
        return jobs

    def save(self, jobs):
        """Persist an iterable/dict of jobs atomically."""
        if isinstance(jobs, dict):
            jobs = list(jobs.values())
        clean = [normalize_job(j) for j in jobs]
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"schema": 1, "jobs": clean}, fh, indent=2)
        os.replace(tmp, self.path)

    # -- CRUD -----------------------------------------------------------
    def list_jobs(self):
        """All jobs, sorted by name."""
        return [self._jobs()[n] for n in sorted(self._jobs())]

    def get_job(self, name):
        job = self._jobs().get(name)
        if job is None:
            raise BackupKitError(f"no job named {name!r}")
        return job

    def has_job(self, name):
        return name in self._jobs()

    def add_job(self, job):
        """Add a new job; refuses to clobber an existing name."""
        job = normalize_job(job)
        jobs = self._jobs()
        if job["name"] in jobs:
            raise BackupKitError(f"a job named {job['name']!r} already exists")
        jobs[job["name"]] = job
        self.save(jobs)
        return job

    def update_job(_self, _name, **changes):
        """Merge *changes* into an existing job and persist it.

        Odd parameter names (``_self``/``_name``) let callers pass ``name=`` in
        *changes* to rename a job without a keyword collision.
        """
        jobs = _self._jobs()
        if _name not in jobs:
            raise BackupKitError(f"no job named {_name!r}")
        merged = dict(jobs[_name])
        merged.update(changes)
        job = normalize_job(merged)
        # allow renaming via changes["name"]
        if job["name"] != _name:
            if job["name"] in jobs:
                raise BackupKitError(
                    f"a job named {job['name']!r} already exists")
            del jobs[_name]
        jobs[job["name"]] = job
        _self.save(jobs)
        return job

    def remove_job(self, name):
        jobs = self._jobs()
        if name not in jobs:
            raise BackupKitError(f"no job named {name!r}")
        del jobs[name]
        self.save(jobs)

    # -- helpers --------------------------------------------------------
    def _jobs(self):
        # Always read fresh so concurrent CLI/GUI edits stay consistent.
        return self.load()


# Module-level convenience wrappers over the default store ------------------

def _default_store():
    return JobStore()


def list_jobs():
    return _default_store().list_jobs()


def get_job(name):
    return _default_store().get_job(name)


def add_job(job):
    return _default_store().add_job(job)


def update_job(name, **changes):
    return _default_store().update_job(name, **changes)


def remove_job(name):
    return _default_store().remove_job(name)
