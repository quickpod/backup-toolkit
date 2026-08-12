"""Shared pytest fixtures for backupkit — everything stays inside tmp dirs."""

import os
import sys

import pytest

# make the package importable when tests run from the repo root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def write(path, content):
    """Write text *content* to *path*, creating parent dirs."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(content)
    return path


def read_bytes(path):
    with open(path, "rb") as fh:
        return fh.read()


@pytest.fixture
def src(tmp_path):
    """A populated source tree; returns its path."""
    root = tmp_path / "source"
    write(str(root / "a.txt"), "alpha")
    write(str(root / "b.txt"), "bravo")
    write(str(root / "sub" / "c.txt"), "charlie")
    write(str(root / "notes.log"), "log-data")
    return root


@pytest.fixture
def dest(tmp_path):
    return tmp_path / "dest"


@pytest.fixture
def job_factory(src, dest):
    """Return a builder that makes a job dict pointing at the tmp trees."""
    def make(name="job1", mode="incremental", **overrides):
        job = {
            "name": name,
            "sources": [str(src)],
            "destination": str(dest),
            "mode": mode,
        }
        job.update(overrides)
        return job
    return make
