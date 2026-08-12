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


def test_register_is_noop_off_windows():
    job = normalize_job({"name": "docs", "sources": ["s"], "destination": "d"})
    rep = schedule.register(job, "1h")
    if os.name != "nt":
        assert rep["supported"] is False
        assert rep["command"][0] == "schtasks"
        assert "Windows-only" in rep["message"]
