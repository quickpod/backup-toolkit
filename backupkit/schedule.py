r"""Register/unregister a Windows Scheduled Task for a backup job.

Scheduling is **Windows-only** -- it shells out to ``schtasks.exe``.  The rest
of Backup Toolkit (the engine, CLI and GUI runs) works on every platform; only
the automatic scheduling described here needs Windows.  On other platforms the
functions build the command and return a report with ``supported=False`` rather
than raising, so the command can be inspected/tested anywhere.

The interval spec is a short string:

    ``"30m"`` / ``"90m"``   -- every N minutes
    ``"2h"``                -- every N hours
    ``"daily"`` / ``"24h"`` -- once a day
    ``"hourly"``            -- every hour
"""

from __future__ import annotations

import os
import subprocess
import sys

from .errors import BackupKitError

TASK_PREFIX = "BackupToolkit"


def task_name(job_name):
    """Scheduled-task name for a job (namespaced under ``BackupToolkit``)."""
    return f"{TASK_PREFIX}-{job_name}"


def _parse_interval(spec):
    """Return ``(schedule_kind, modifier)`` for schtasks from an interval spec.

    ``schedule_kind`` is one of ``MINUTE``/``HOURLY``/``DAILY``; ``modifier`` is
    the ``/MO`` value (``None`` when not applicable).
    """
    if not spec:
        raise BackupKitError("an interval is required (e.g. 30m, 2h, daily)")
    s = str(spec).strip().lower()
    if s in ("daily", "day", "24h"):
        return "DAILY", None
    if s in ("hourly", "hour"):
        return "HOURLY", None
    unit = s[-1]
    num = s[:-1]
    if unit == "m" and num.isdigit() and int(num) > 0:
        return "MINUTE", int(num)
    if unit == "h" and num.isdigit() and int(num) > 0:
        return "HOURLY", int(num)
    if s.isdigit() and int(s) > 0:  # bare number = minutes
        return "MINUTE", int(s)
    raise BackupKitError(f"could not parse interval {spec!r}")


def _run_command(job, interval, python_exe=None, store_path=None):
    """The command schtasks should run for this job (a list of args)."""
    exe = python_exe or sys.executable
    # When frozen into BackupToolkit.exe, run the exe directly with CLI args;
    # otherwise invoke the module through the interpreter.
    if getattr(sys, "frozen", False):
        prefix = [exe]
    else:
        prefix = [exe, "-m", "backupkit"]
    store = ["--store", store_path] if store_path else []
    return prefix + store + ["run", job["name"]]


def _quote(cmd):
    parts = []
    for c in cmd:
        parts.append(f'"{c}"' if " " in c else c)
    return " ".join(parts)


def build_schtasks_create(job, interval, python_exe=None, store_path=None):
    """Return the full ``schtasks /Create`` argv for registering *job*."""
    kind, modifier = _parse_interval(interval)
    tr = _quote(_run_command(job, interval, python_exe, store_path))
    cmd = ["schtasks", "/Create", "/TN", task_name(job["name"]),
           "/TR", tr, "/SC", kind, "/F"]
    if modifier is not None:
        cmd += ["/MO", str(modifier)]
    return cmd


def build_schtasks_delete(job):
    """Return the ``schtasks /Delete`` argv for a job's task."""
    return ["schtasks", "/Delete", "/TN", task_name(job["name"]), "/F"]


def _report(command, supported, ok=None, message=""):
    return {
        "task": None,
        "command": command,
        "command_str": _quote(command),
        "supported": supported,
        "ok": ok,
        "message": message,
    }


def register(job, interval, python_exe=None, store_path=None):
    """Create/replace the Scheduled Task for *job*.

    On Windows, runs ``schtasks`` and returns ``ok``/``message`` from it.  On
    other platforms, no-ops and returns ``supported=False`` with the command it
    *would* have run.
    """
    cmd = build_schtasks_create(job, interval, python_exe, store_path)
    rep = _report(cmd, supported=(os.name == "nt"))
    rep["task"] = task_name(job["name"])
    if os.name != "nt":
        rep["message"] = ("Scheduling is Windows-only; run this command on "
                          "Windows to register the task.")
        return rep
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
        rep["ok"] = proc.returncode == 0
        rep["message"] = (proc.stdout or proc.stderr or "").strip()
        if not rep["ok"]:
            raise BackupKitError(
                f"schtasks failed ({proc.returncode}): {rep['message']}")
    except FileNotFoundError as exc:
        raise BackupKitError(f"schtasks not available: {exc}")
    return rep


def unregister(job):
    """Delete a job's Scheduled Task (Windows); no-op elsewhere."""
    cmd = build_schtasks_delete(job)
    rep = _report(cmd, supported=(os.name == "nt"))
    rep["task"] = task_name(job["name"])
    if os.name != "nt":
        rep["message"] = "Scheduling is Windows-only; nothing to remove here."
        return rep
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
        rep["ok"] = proc.returncode == 0
        rep["message"] = (proc.stdout or proc.stderr or "").strip()
        if not rep["ok"]:
            raise BackupKitError(
                f"schtasks delete failed ({proc.returncode}): {rep['message']}")
    except FileNotFoundError as exc:
        raise BackupKitError(f"schtasks not available: {exc}")
    return rep
