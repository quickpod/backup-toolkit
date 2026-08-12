"""Engine tests: incremental, mirror, versioned, filters, manifests."""

import os
from datetime import datetime

from conftest import read_bytes, write

from backupkit import run_backup, verify_backup
from backupkit.engine import MANIFEST_NAME


def _dest_file(dest, *parts):
    # sources land under <dest>/<source-basename>/...  (basename is "source")
    return os.path.join(str(dest), "source", *parts)


def test_incremental_copies_all_then_only_changed(job_factory, dest, src):
    job = job_factory(mode="incremental")

    rep1 = run_backup(job)
    assert rep1["copied"] == 4  # a, b, sub/c, notes.log
    assert rep1["deleted"] == 0
    assert read_bytes(_dest_file(dest, "a.txt")) == b"alpha"
    assert os.path.isfile(_dest_file(dest, "sub", "c.txt"))

    # second run with no changes: everything skipped
    rep2 = run_backup(job)
    assert rep2["copied"] == 0
    assert rep2["skipped"] == 4

    # modify exactly one file -> only that one is copied
    write(str(src / "b.txt"), "BRAVO-CHANGED")
    rep3 = run_backup(job)
    assert rep3["copied"] == 1
    assert rep3["skipped"] == 3
    assert read_bytes(_dest_file(dest, "b.txt")) == b"BRAVO-CHANGED"


def test_incremental_never_deletes(job_factory, dest):
    job = job_factory(mode="incremental")
    run_backup(job)
    # an extra file placed in the destination survives an incremental run
    extra = _dest_file(dest, "leftover.txt")
    write(extra, "keep me")
    rep = run_backup(job)
    assert rep["deleted"] == 0
    assert os.path.isfile(extra)


def test_mirror_makes_identical_and_deletes_extras(job_factory, dest, src):
    job = job_factory(mode="mirror")
    run_backup(job)

    # plant an extra file that is NOT in the source
    extra = _dest_file(dest, "sub", "orphan.txt")
    write(extra, "delete me")

    rep = run_backup(job)
    assert rep["deleted"] == 1
    assert not os.path.exists(extra)

    # destination now mirrors the source exactly (ignoring manifest.json)
    src_files = set()
    for dp, _d, fs in os.walk(str(src)):
        for f in fs:
            rel = os.path.relpath(os.path.join(dp, f), str(src))
            src_files.add(rel.replace(os.sep, "/"))
    mirror_root = os.path.join(str(dest), "source")
    dst_files = set()
    for dp, _d, fs in os.walk(mirror_root):
        for f in fs:
            rel = os.path.relpath(os.path.join(dp, f), mirror_root)
            dst_files.add(rel.replace(os.sep, "/"))
    assert dst_files == src_files


def test_versioned_creates_timestamped_folder_and_prunes(job_factory, dest, src):
    job = job_factory(mode="versioned", keep=2)
    times = [datetime(2026, 1, 1, 12, 0, 0),
             datetime(2026, 1, 2, 12, 0, 0),
             datetime(2026, 1, 3, 12, 0, 0)]
    reports = []
    for i, now in enumerate(times):
        # vary the length so the change is detected even within one clock second
        write(str(src / "a.txt"), "alpha" + "!" * (i + 1))
        reports.append(run_backup(job, now=now))

    job_root = os.path.join(str(dest), job["name"])
    versions = sorted(os.listdir(job_root))
    # keep=2 -> only the two newest timestamp folders remain
    assert versions == ["20260102-120000", "20260103-120000"]

    newest = os.path.join(job_root, "20260103-120000")
    assert os.path.isfile(os.path.join(newest, MANIFEST_NAME))
    assert read_bytes(os.path.join(newest, "source", "a.txt")) == b"alpha!!!"


def test_versioned_hardlinks_unchanged_files(job_factory, dest, src):
    job = job_factory(mode="versioned", keep=5)
    r1 = run_backup(job, now=datetime(2026, 1, 1, 0, 0, 0))
    # no changes at all -> second version should hard-link every file
    r2 = run_backup(job, now=datetime(2026, 1, 2, 0, 0, 0))
    assert r1["copied"] == 4
    assert r2["linked"] == 4
    assert r2["copied"] == 0

    v1 = os.path.join(str(dest), job["name"], "20260101-000000", "source", "a.txt")
    v2 = os.path.join(str(dest), job["name"], "20260102-000000", "source", "a.txt")
    # a hard link means the two paths share one inode
    assert os.path.samefile(v1, v2)


def test_includes_and_excludes(job_factory, dest, src):
    # only *.txt, but drop anything under sub/
    job = job_factory(mode="incremental", includes=["*.txt"], excludes=["sub/*"])
    rep = run_backup(job)
    assert os.path.isfile(_dest_file(dest, "a.txt"))
    assert os.path.isfile(_dest_file(dest, "b.txt"))
    assert not os.path.exists(_dest_file(dest, "notes.log"))   # excluded by include filter
    assert not os.path.exists(_dest_file(dest, "sub", "c.txt"))  # excluded
    assert rep["copied"] == 2


def test_multiple_sources_land_in_separate_subfolders(tmp_path):
    s1 = tmp_path / "photos"
    s2 = tmp_path / "docs"
    write(str(s1 / "p.txt"), "pic")
    write(str(s2 / "d.txt"), "doc")
    d = tmp_path / "out"
    job = {"name": "multi", "sources": [str(s1), str(s2)],
           "destination": str(d), "mode": "incremental"}
    rep = run_backup(job)
    assert rep["copied"] == 2
    assert os.path.isfile(os.path.join(str(d), "photos", "p.txt"))
    assert os.path.isfile(os.path.join(str(d), "docs", "d.txt"))
