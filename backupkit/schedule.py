r"""Register/unregister a recurring backup job with the OS scheduler.

The backend is chosen from the running OS:

* **Windows** -- a Scheduled Task via ``schtasks.exe``.
* **Linux / macOS** -- a per-user ``cron`` entry (``crontab``), tagged with a
  ``# BackupToolkit-<job>`` marker so it can be replaced/removed idempotently.

The rest of Backup Toolkit (the engine, CLI and GUI runs) is platform-neutral;
only the automatic scheduling wired up here is OS-specific.  Both backends
expose *pure* command builders (``build_schtasks_create`` / ``build_crontab_line``)
so the exact command can be inspected and unit-tested on any host without
touching the real scheduler.  ``register``/``unregister`` return a report dict
whose ``backend`` field says which scheduler was used and whose ``supported``
field is now ``True`` on every mainstream OS.

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


# ---------------------------------------------------------------------------
# POSIX (Linux / macOS) backend -- cron
# ---------------------------------------------------------------------------
# Each managed crontab line ends with this marker so we can find, replace and
# remove exactly our own entries without disturbing the user's other cron jobs.
def cron_marker(job):
    """The trailing ``# BackupToolkit-<job>`` comment that tags our cron line."""
    return f"# {task_name(job['name'])}"


def cron_schedule(interval):
    """Translate an interval spec into a 5-field cron schedule expression.

    ``"30m"`` -> ``*/30 * * * *``; ``"2h"`` -> ``0 */2 * * *``;
    ``"hourly"`` -> ``0 * * * *``; ``"daily"``/``"24h"`` -> ``0 3 * * *``.
    """
    kind, modifier = _parse_interval(interval)
    if kind == "MINUTE":
        step = modifier or 1
        if step >= 60:  # cron minutes are 0-59; fall back to hourly
            return "0 * * * *"
        return f"*/{step} * * * *"
    if kind == "HOURLY":
        if modifier and modifier > 1:
            return f"0 */{modifier} * * *"
        return "0 * * * *"
    # DAILY -- run in the small hours (03:00) to stay out of the way.
    return "0 3 * * *"


def build_crontab_line(job, interval, python_exe=None, store_path=None):
    """Return the full crontab line (schedule + command + marker) for *job*."""
    expr = cron_schedule(interval)
    cmd = _quote(_run_command(job, interval, python_exe, store_path))
    return f"{expr} {cmd}  {cron_marker(job)}"


def _read_crontab():
    """Return the current user's crontab text (``""`` when there is none).

    Factored out so tests can monkeypatch the crontab I/O rather than mutate a
    real user crontab.
    """
    try:
        proc = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise BackupKitError(f"crontab not available: {exc}")
    # A missing crontab exits non-zero with a "no crontab for ..." message; that
    # is not an error for us -- it just means we start from an empty crontab.
    return proc.stdout if proc.returncode == 0 else ""


def _write_crontab(text):
    """Install *text* as the current user's crontab (via ``crontab -``)."""
    try:
        proc = subprocess.run(["crontab", "-"], input=text, capture_output=True,
                              text=True)
    except FileNotFoundError as exc:
        raise BackupKitError(f"crontab not available: {exc}")
    if proc.returncode != 0:
        raise BackupKitError(
            f"crontab install failed ({proc.returncode}): "
            f"{(proc.stderr or proc.stdout or '').strip()}")


def _crontab_without_job(existing, job):
    """Return *existing* crontab lines with this job's marked line removed."""
    marker = cron_marker(job)
    return [ln for ln in existing.splitlines() if marker not in ln]


def _report(command, backend, supported=True, ok=None, message=""):
    if isinstance(command, str):
        command_list, command_str = None, command
    else:
        command_list, command_str = command, _quote(command)
    return {
        "task": None,
        "backend": backend,
        "command": command_list,
        "command_str": command_str,
        "supported": supported,
        "ok": ok,
        "message": message,
    }


def register(job, interval, python_exe=None, store_path=None):
    """Create/replace the OS schedule entry for *job*.

    Uses a Windows Scheduled Task (``schtasks``) on Windows and a per-user cron
    entry (``crontab``) on Linux/macOS.  Returns a report dict (see the module
    docstring); raises :class:`BackupKitError` if the scheduler command fails.
    """
    if os.name == "nt":
        return _register_windows(job, interval, python_exe, store_path)
    return _register_cron(job, interval, python_exe, store_path)


def unregister(job):
    """Remove a job's OS schedule entry (Scheduled Task on Windows, cron else)."""
    if os.name == "nt":
        return _unregister_windows(job)
    return _unregister_cron(job)


# --- Windows (schtasks) -----------------------------------------------------
def _register_windows(job, interval, python_exe=None, store_path=None):
    cmd = build_schtasks_create(job, interval, python_exe, store_path)
    rep = _report(cmd, backend="schtasks")
    rep["task"] = task_name(job["name"])
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


def _unregister_windows(job):
    cmd = build_schtasks_delete(job)
    rep = _report(cmd, backend="schtasks")
    rep["task"] = task_name(job["name"])
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


# --- POSIX (cron) -----------------------------------------------------------
def _register_cron(job, interval, python_exe=None, store_path=None):
    line = build_crontab_line(job, interval, python_exe, store_path)
    rep = _report(line, backend="cron")
    rep["task"] = task_name(job["name"])
    kept = _crontab_without_job(_read_crontab(), job)  # replace any existing one
    kept.append(line)
    _write_crontab("\n".join(kept) + "\n")
    rep["ok"] = True
    rep["message"] = f"Installed cron entry: {line}"
    return rep


def _unregister_cron(job):
    rep = _report(cron_marker(job), backend="cron")  # string -> command_str only
    rep["task"] = task_name(job["name"])
    existing = _read_crontab()
    kept = _crontab_without_job(existing, job)
    if len(kept) == len(existing.splitlines()):
        rep["ok"] = True
        rep["message"] = "No matching cron entry to remove."
        return rep
    _write_crontab(("\n".join(kept) + "\n") if kept else "")
    rep["ok"] = True
    rep["message"] = "Removed cron entry."
    return rep
