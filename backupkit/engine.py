r"""The backup engine: :func:`run_backup` and the file-walking helpers.

Three modes, all pure standard library:

* **mirror** -- make the destination *match* the sources.  New or changed files
  (by size + mtime) are copied in, and **any file in the destination that is not
  in the sources is DELETED.**  This is destructive by design; the GUI confirms
  before running it and the CLI documents it.
* **incremental** -- copy only new/changed files into the destination.  Nothing
  in the destination is ever deleted; old files simply accumulate.
* **versioned** -- copy the sources into ``dest/<jobname>/<timestamp>/``.  Files
  that are unchanged from the previous version are hard-linked (``os.link``) so
  repeated versions cost almost no disk; unchanged-detection falls back to a
  full copy when hard-linking is unavailable.  Old versions are then pruned by
  keep-count and/or keep-days.

Every backup writes a ``manifest.json`` mapping ``relpath -> {size, mtime,
sha256}`` at its root, which :mod:`backupkit.verify` and
:mod:`backupkit.restore` build on.

Sources are laid out under the destination by their folder name -- a source
``/data/photos`` lands at ``<root>/photos/...`` -- so several sources can share
one destination without colliding (duplicate names get a ``_2`` suffix).

``run_backup`` returns a *report* dict::

    {job, mode, copied, linked, skipped, deleted, bytes, errors,
     dest, manifest, version_dir (versioned only), cancelled}
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
import shutil
from datetime import datetime, timedelta

from . import config
from .errors import BackupKitError

MANIFEST_NAME = "manifest.json"
_CHUNK = 1024 * 1024


# ---------------------------------------------------------------------------
# path / matching helpers
# ---------------------------------------------------------------------------
def _posix(rel):
    return rel.replace(os.sep, "/")


def _posixjoin(*parts):
    return "/".join(p.strip("/") for p in parts if p not in (None, ""))


def _matches(rel, patterns):
    """True if *rel* (or its basename) matches any glob in *patterns*."""
    if not patterns:
        return False
    base = rel.rsplit("/", 1)[-1]
    for pat in patterns:
        if fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(base, pat):
            return True
    return False


def included(rel, includes, excludes):
    """Apply include/exclude glob filters to a forward-slash relative path."""
    if includes and not _matches(rel, includes):
        return False
    if excludes and _matches(rel, excludes):
        return False
    return True


def iter_files(root, includes=None, excludes=None):
    """Yield forward-slash paths of files under *root* that pass the filters.

    Directories whose name/relpath matches an exclude are pruned so we do not
    descend into them.
    """
    includes = includes or []
    excludes = excludes or []
    root = os.path.abspath(root)
    for dirpath, dirnames, filenames in os.walk(root):
        relbase = _posix(os.path.relpath(dirpath, root))
        if relbase == ".":
            relbase = ""
        # prune excluded directories in place
        if excludes:
            kept = []
            for d in dirnames:
                rel = _posixjoin(relbase, d)
                if _matches(rel, excludes):
                    continue
                kept.append(d)
            dirnames[:] = kept
        dirnames.sort()
        for fn in sorted(filenames):
            rel = _posixjoin(relbase, fn)
            if included(rel, includes, excludes):
                yield rel


def sha256_file(path):
    """SHA-256 hex digest of *path* (streamed; may raise ``OSError``)."""
    h = hashlib.sha256()
    with open(path, "rb", buffering=0) as fh:
        for chunk in iter(lambda: fh.read(_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def _unique_bases(sources):
    """Map each source folder to a unique destination subfolder name."""
    out = {}
    seen = set()
    for s in sources:
        base = os.path.basename(os.path.normpath(s)) or "root"
        # strip drive colons etc. that are illegal in a folder name
        base = base.replace(":", "").replace(os.sep, "_") or "root"
        orig, i = base, 2
        while base in seen:
            base = f"{orig}_{i}"
            i += 1
        seen.add(base)
        out[s] = base
    return out


def _plan(job, report):
    """Build ``[(abs_source_file, rel_dest_posix), ...]`` for every source."""
    pairs = []
    bases = _unique_bases(job["sources"])
    for source in job["sources"]:
        if not os.path.isdir(source):
            report["errors"].append(f"source not found: {source}")
            continue
        base = bases[source]
        for rel in iter_files(source, job["includes"], job["excludes"]):
            abs_src = os.path.join(source, *rel.split("/"))
            pairs.append((abs_src, _posixjoin(base, rel)))
    return pairs


def _same_file(a, b):
    """Cheap unchanged-check: same size and (integer) mtime."""
    try:
        sa, sb = os.stat(a), os.stat(b)
    except OSError:
        return False
    return sa.st_size == sb.st_size and int(sa.st_mtime) == int(sb.st_mtime)


def _new_report(job):
    return {
        "job": job["name"],
        "mode": job["mode"],
        "copied": 0,
        "linked": 0,
        "skipped": 0,
        "deleted": 0,
        "bytes": 0,
        "errors": [],
        "cancelled": False,
        "dest": job["destination"],
        "manifest": None,
    }


def _emit(progress, **event):
    if progress is None:
        return
    try:
        progress(event)
    except Exception:
        pass  # a broken progress callback must never break a backup


def _copy(src, dst, report):
    """Copy *src* to *dst* (dirs created, metadata preserved). Returns bool."""
    try:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
        report["copied"] += 1
        try:
            report["bytes"] += os.path.getsize(dst)
        except OSError:
            pass
        return True
    except OSError as exc:
        report["errors"].append(f"copy failed {src}: {exc}")
        return False


def _write_manifest(root, job, report, now):
    """Hash every file under *root* and write ``manifest.json`` there."""
    files = {}
    for dirpath, _dirs, filenames in os.walk(root):
        for fn in sorted(filenames):
            full = os.path.join(dirpath, fn)
            rel = _posix(os.path.relpath(full, root))
            if rel == MANIFEST_NAME:
                continue
            try:
                st = os.stat(full)
                files[rel] = {
                    "size": st.st_size,
                    "mtime": st.st_mtime,
                    "sha256": sha256_file(full),
                }
            except OSError as exc:
                report["errors"].append(f"hash failed {full}: {exc}")
    manifest = {
        "schema": 1,
        "job": job["name"],
        "mode": job["mode"],
        "generated": now.isoformat(timespec="seconds"),
        "files": files,
    }
    path = os.path.join(root, MANIFEST_NAME)
    import json
    try:
        os.makedirs(root, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, indent=2)
        os.replace(tmp, path)
        report["manifest"] = path
    except OSError as exc:
        report["errors"].append(f"manifest write failed: {exc}")
    return manifest


# ---------------------------------------------------------------------------
# modes
# ---------------------------------------------------------------------------
def _copy_pairs(pairs, dest_root, report, progress, cancel):
    """Copy new/changed pairs into *dest_root*; skip unchanged. Shared by
    mirror and incremental."""
    total = len(pairs)
    for i, (src, rel) in enumerate(pairs, 1):
        if cancel and cancel():
            report["cancelled"] = True
            return
        dst = os.path.join(dest_root, *rel.split("/"))
        if os.path.exists(dst) and _same_file(src, dst):
            report["skipped"] += 1
            _emit(progress, type="file", action="skip", path=rel,
                  index=i, total=total)
            continue
        _copy(src, dst, report)
        _emit(progress, type="file", action="copy", path=rel,
              index=i, total=total)


def _run_incremental(job, report, now, progress, cancel):
    dest = job["destination"]
    pairs = _plan(job, report)
    _copy_pairs(pairs, dest, report, progress, cancel)
    if not report["cancelled"]:
        _write_manifest(dest, job, report, now)
    return report


def _run_mirror(job, report, now, progress, cancel):
    dest = job["destination"]
    pairs = _plan(job, report)
    _copy_pairs(pairs, dest, report, progress, cancel)
    if report["cancelled"]:
        return report
    # delete anything in the destination that is not part of the plan
    expected = {rel for _src, rel in pairs}
    if os.path.isdir(dest):
        for dirpath, _dirs, filenames in os.walk(dest, topdown=False):
            for fn in filenames:
                full = os.path.join(dirpath, fn)
                rel = _posix(os.path.relpath(full, dest))
                if rel == MANIFEST_NAME or rel in expected:
                    continue
                try:
                    os.remove(full)
                    report["deleted"] += 1
                    _emit(progress, type="file", action="delete", path=rel)
                except OSError as exc:
                    report["errors"].append(f"delete failed {full}: {exc}")
            # drop now-empty directories (never the root)
            if os.path.abspath(dirpath) != os.path.abspath(dest):
                try:
                    if not os.listdir(dirpath):
                        os.rmdir(dirpath)
                except OSError:
                    pass
    _write_manifest(dest, job, report, now)
    return report


def _version_dirs(job_root):
    """Existing version directories under *job_root*, oldest first (by name)."""
    if not os.path.isdir(job_root):
        return []
    entries = [os.path.join(job_root, d) for d in os.listdir(job_root)
               if os.path.isdir(os.path.join(job_root, d))]
    return sorted(entries, key=lambda p: os.path.basename(p))


def _run_versioned(job, report, now, progress, cancel):
    dest = job["destination"]
    job_root = os.path.join(dest, job["name"])
    previous = _version_dirs(job_root)
    prev = previous[-1] if previous else None

    stamp = now.strftime("%Y%m%d-%H%M%S")
    version_dir = os.path.join(job_root, stamp)
    if os.path.exists(version_dir):  # avoid clobbering a same-second run
        version_dir = os.path.join(job_root, f"{stamp}-{now.microsecond:06d}")
    report["version_dir"] = version_dir

    pairs = _plan(job, report)
    total = len(pairs)
    for i, (src, rel) in enumerate(pairs, 1):
        if cancel and cancel():
            report["cancelled"] = True
            break
        dst = os.path.join(version_dir, *rel.split("/"))
        linked = False
        if prev is not None:
            prev_file = os.path.join(prev, *rel.split("/"))
            if os.path.exists(prev_file) and _same_file(src, prev_file):
                try:
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    os.link(prev_file, dst)
                    report["linked"] += 1
                    try:
                        report["bytes"] += os.path.getsize(dst)
                    except OSError:
                        pass
                    linked = True
                    _emit(progress, type="file", action="link", path=rel,
                          index=i, total=total)
                except OSError:
                    linked = False  # fall through to a plain copy
        if not linked:
            _copy(src, dst, report)
            _emit(progress, type="file", action="copy", path=rel,
                  index=i, total=total)

    if report["cancelled"]:
        return report
    _write_manifest(version_dir, job, report, now)
    _prune_versions(job, report, now)
    return report


def _prune_versions(job, report, now):
    """Remove old versions per keep-count then keep-days."""
    job_root = os.path.join(job["destination"], job["name"])
    versions = _version_dirs(job_root)
    doomed = []

    keep = job.get("keep", 0)
    if keep and len(versions) > keep:
        doomed.extend(versions[:len(versions) - keep])
        versions = versions[len(versions) - keep:]

    keep_days = job.get("keep_days", 0)
    if keep_days:
        cutoff = now - timedelta(days=keep_days)
        for vdir in list(versions):
            try:
                mtime = datetime.fromtimestamp(os.path.getmtime(vdir))
            except OSError:
                continue
            if mtime < cutoff:
                doomed.append(vdir)

    for vdir in doomed:
        try:
            shutil.rmtree(vdir)
            report["deleted"] += 1
        except OSError as exc:
            report["errors"].append(f"prune failed {vdir}: {exc}")


_RUNNERS = {
    "incremental": _run_incremental,
    "mirror": _run_mirror,
    "versioned": _run_versioned,
}


def run_backup(job, now=None, progress=None, cancel=None):
    """Run *job* (a dict) and return a report dict.

    Parameters
    ----------
    job : dict
        A job as produced by :mod:`backupkit.config`.
    now : datetime, optional
        Injected "current time" used for the versioned timestamp folder and the
        manifest's ``generated`` field.  Passing it keeps tests deterministic;
        defaults to ``datetime.now()``.
    progress : callable, optional
        Called as ``progress(event_dict)`` for each file (``type='file'`` with
        ``action`` in copy/skip/link/delete, plus ``index``/``total``).  Any
        exception it raises is swallowed.
    cancel : callable, optional
        Polled before each file; if it returns truthy the run stops early and
        the report's ``cancelled`` flag is set.

    Raises
    ------
    BackupKitError
        Only for an invalid job (bad mode, no sources, no destination).
        Per-file problems are collected in ``report['errors']`` instead.
    """
    job = config.require_runnable(job)
    now = now or datetime.now()
    report = _new_report(job)

    dest = job["destination"]
    try:
        os.makedirs(dest, exist_ok=True)
    except OSError as exc:
        raise BackupKitError(f"cannot create destination {dest!r}: {exc}")

    runner = _RUNNERS.get(job["mode"])
    if runner is None:  # normalize_job already guards, belt & braces
        raise BackupKitError(f"unknown mode {job['mode']!r}")
    return runner(job, report, now, progress, cancel)
