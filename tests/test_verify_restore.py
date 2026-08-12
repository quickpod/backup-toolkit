"""Tests for manifest verification and restore."""

import os
from datetime import datetime

from conftest import read_bytes, write

from backupkit import list_versions, restore, run_backup, verify_backup


def test_manifest_hashes_match(job_factory, dest):
    run_backup(job_factory(mode="incremental"))
    results = verify_backup(str(dest))
    assert results["passed"] is True
    assert len(results["ok"]) == 4
    assert results["missing"] == []
    assert results["mismatched"] == []


def test_verify_detects_tampered_file(job_factory, dest):
    run_backup(job_factory(mode="incremental"))
    # tamper with a backed-up file after the manifest was written
    tampered = os.path.join(str(dest), "source", "a.txt")
    write(tampered, "corrupted contents")
    results = verify_backup(str(dest))
    assert results["passed"] is False
    assert "source/a.txt" in results["mismatched"]


def test_verify_detects_missing_file(job_factory, dest):
    run_backup(job_factory(mode="incremental"))
    os.remove(os.path.join(str(dest), "source", "b.txt"))
    results = verify_backup(str(dest))
    assert results["passed"] is False
    assert "source/b.txt" in results["missing"]


def test_restore_reproduces_original_bytes(job_factory, dest, src, tmp_path):
    job = job_factory(mode="versioned", keep=5)
    run_backup(job, now=datetime(2026, 5, 1, 8, 0, 0))

    versions = list_versions(job)
    assert len(versions) == 1
    restore_target = tmp_path / "restored"
    rep = restore(versions[0]["path"], str(restore_target))
    assert rep["restored"] == 4

    # bytes come back identical to the originals under the <source> subfolder
    assert read_bytes(str(restore_target / "source" / "a.txt")) == b"alpha"
    assert read_bytes(str(restore_target / "source" / "sub" / "c.txt")) == b"charlie"


def test_restore_overwrite_protection(job_factory, dest, tmp_path):
    job = job_factory(mode="versioned", keep=5)
    run_backup(job, now=datetime(2026, 5, 1, 8, 0, 0))
    version_dir = list_versions(job)[0]["path"]

    target = tmp_path / "restored"
    write(str(target / "source" / "a.txt"), "existing local edit")

    # default: existing file is protected (skipped)
    rep = restore(version_dir, str(target))
    assert rep["skipped"] >= 1
    assert read_bytes(str(target / "source" / "a.txt")) == b"existing local edit"

    # overwrite=True replaces it
    rep2 = restore(version_dir, str(target), overwrite=True)
    assert rep2["restored"] >= 1
    assert read_bytes(str(target / "source" / "a.txt")) == b"alpha"


def test_restore_subpaths_only(job_factory, dest, tmp_path):
    job = job_factory(mode="versioned", keep=5)
    run_backup(job, now=datetime(2026, 5, 1, 8, 0, 0))
    version_dir = list_versions(job)[0]["path"]

    target = tmp_path / "partial"
    rep = restore(version_dir, str(target), subpaths=["source/a.txt"])
    assert rep["restored"] == 1
    assert os.path.isfile(str(target / "source" / "a.txt"))
    assert not os.path.exists(str(target / "source" / "b.txt"))
