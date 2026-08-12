"""Tests for the job store (config) and schedule command building."""

import os

import pytest

from backupkit import BackupKitError, schedule
from backupkit.config import JobStore, normalize_job


def test_normalize_defaults_and_validation():
    job = normalize_job({"name": "j", "sources": "one/dir"})
    assert job["sources"] == ["one/dir"]
    assert job["mode"] == "incremental"
    assert job["enabled"] is True
    assert job["keep"] == 0

    with pytest.raises(BackupKitError):
        normalize_job({"sources": []})  # no name
    with pytest.raises(BackupKitError):
        normalize_job({"name": "j", "mode": "bogus"})


def test_store_crud_roundtrip(tmp_path):
    path = str(tmp_path / "jobs.json")
    store = JobStore(path)
    assert store.list_jobs() == []

    store.add_job({"name": "docs", "sources": [str(tmp_path / "s")],
                   "destination": str(tmp_path / "d"), "mode": "versioned",
                   "keep": 3})
    assert os.path.isfile(path)
    assert [j["name"] for j in store.list_jobs()] == ["docs"]

    # persisted independently: a fresh store sees it
    assert JobStore(path).get_job("docs")["keep"] == 3

    # duplicate add refused
    with pytest.raises(BackupKitError):
        store.add_job({"name": "docs", "sources": ["x"]})

    store.update_job("docs", keep=9, enabled=False)
    assert store.get_job("docs")["keep"] == 9
    assert store.get_job("docs")["enabled"] is False

    store.update_job("docs", name="documents")  # rename
    assert not store.has_job("docs")
    assert store.has_job("documents")

    store.remove_job("documents")
    assert store.list_jobs() == []
    with pytest.raises(BackupKitError):
        store.get_job("documents")


def test_corrupt_store_is_not_fatal(tmp_path):
    path = str(tmp_path / "jobs.json")
    with open(path, "w") as fh:
        fh.write("{not valid json")
    assert JobStore(path).list_jobs() == []


def test_schedule_command_building_cross_platform():
    job = normalize_job({"name": "docs", "sources": ["s"], "destination": "d"})

    create = schedule.build_schtasks_create(job, "30m")
    assert "schtasks" == create[0]
    assert "/Create" in create
    assert "/SC" in create and "MINUTE" in create
    assert "/MO" in create and "30" in create
    assert "BackupToolkit-docs" in create

    daily = schedule.build_schtasks_create(job, "daily")
    assert "DAILY" in daily and "/MO" not in daily

    delete = schedule.build_schtasks_delete(job)
    assert delete[:2] == ["schtasks", "/Delete"]

    with pytest.raises(BackupKitError):
        schedule.build_schtasks_create(job, "not-an-interval")


def test_cron_schedule_expressions():
    assert schedule.cron_schedule("30m") == "*/30 * * * *"
    assert schedule.cron_schedule("90m") == "0 * * * *"  # >=60min falls back hourly
    assert schedule.cron_schedule("2h") == "0 */2 * * *"
    assert schedule.cron_schedule("hourly") == "0 * * * *"
    assert schedule.cron_schedule("daily") == "0 3 * * *"
    assert schedule.cron_schedule("24h") == "0 3 * * *"
    with pytest.raises(BackupKitError):
        schedule.cron_schedule("nope")


def test_build_crontab_line_shape():
    job = normalize_job({"name": "docs", "sources": ["s"], "destination": "d"})
    line = schedule.build_crontab_line(job, "30m")
    assert line.startswith("*/30 * * * * ")
    assert line.endswith("# BackupToolkit-docs")
    assert "run" in line and "docs" in line
    # No Windows-isms leaked into the POSIX command line.
    assert "schtasks" not in line
    assert "\\" not in line


@pytest.mark.skipif(os.name == "nt", reason="cron backend is used off Windows")
def test_register_uses_cron_off_windows(monkeypatch):
    """On AIQuick/Linux, register auto-detects and installs a cron entry.

    crontab I/O is monkeypatched so the host's real crontab is never touched.
    """
    job = normalize_job({"name": "docs", "sources": ["s"], "destination": "d"})
    fake = {"text": "# an unrelated user job\n0 5 * * * echo hi\n"}

    def _read():
        return fake["text"]

    def _write(text):
        fake["text"] = text

    monkeypatch.setattr(schedule, "_read_crontab", _read)
    monkeypatch.setattr(schedule, "_write_crontab", _write)

    rep = schedule.register(job, "1h")
    assert rep["supported"] is True
    assert rep["backend"] == "cron"
    assert rep["ok"] is True
    # The user's unrelated line survives; our marked line is added.
    assert "echo hi" in fake["text"]
    assert "# BackupToolkit-docs" in fake["text"]

    # Re-registering replaces (does not duplicate) our line.
    schedule.register(job, "30m")
    assert fake["text"].count("# BackupToolkit-docs") == 1
    assert "*/30 * * * *" in fake["text"]

    # Unregister removes only our line.
    rep2 = schedule.unregister(job)
    assert rep2["backend"] == "cron"
    assert rep2["ok"] is True
    assert "# BackupToolkit-docs" not in fake["text"]
    assert "echo hi" in fake["text"]


@pytest.mark.skipif(os.name != "nt", reason="Scheduled Task backend is Windows-only")
def test_register_uses_schtasks_on_windows(monkeypatch):
    job = normalize_job({"name": "docs", "sources": ["s"], "destination": "d"})

    class _Proc:
        returncode = 0
        stdout = "SUCCESS"
        stderr = ""

    monkeypatch.setattr(schedule.subprocess, "run", lambda *a, **k: _Proc())
    rep = schedule.register(job, "1h")
    assert rep["backend"] == "schtasks"
    assert rep["command"][0] == "schtasks"
    assert rep["ok"] is True
