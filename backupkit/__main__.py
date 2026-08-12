"""Command-line interface: ``python -m backupkit <command> ...``.

Manage backup jobs and run them anywhere.  Scheduling is Windows-only (it uses
``schtasks``); every other command works on every platform.

    backupkit job add docs --source ./Documents --dest ./bak --mode versioned --keep 7
    backupkit job list
    backupkit run docs               # or:  run --all
    backupkit verify docs            # or a backup directory
    backupkit versions docs
    backupkit restore docs --version 20260812-101500 --dest ./restored
    backupkit schedule docs --interval 60m
    backupkit unschedule docs

Use ``--store PATH`` (before or after the command) to point at a specific
jobs.json instead of the per-user default.
"""

from __future__ import annotations

import argparse
import sys

from . import schedule
from .config import JobStore
from .engine import run_backup
from .errors import BackupKitError
from .restore import list_versions, restore
from .verify import verify_backup


def _store(a):
    return JobStore(getattr(a, "store", None) or None)


def _fmt_bytes(n):
    size = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024.0 or unit == "TB":
            return f"{int(size)}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024.0
    return f"{size:.1f}TB"


def _print_report(rep):
    parts = [f"copied={rep['copied']}"]
    if rep.get("linked"):
        parts.append(f"linked={rep['linked']}")
    parts += [f"skipped={rep['skipped']}", f"deleted={rep['deleted']}",
              f"bytes={_fmt_bytes(rep['bytes'])}"]
    if rep.get("cancelled"):
        parts.append("CANCELLED")
    print(f"[{rep['job']}] {rep['mode']}: " + ", ".join(parts))
    if rep.get("version_dir"):
        print(f"    version: {rep['version_dir']}")
    if rep.get("manifest"):
        print(f"    manifest: {rep['manifest']}")
    for err in rep.get("errors", []):
        print(f"    ! {err}", file=sys.stderr)


# --- job sub-commands -------------------------------------------------------

def _job_kwargs(a):
    kw = {}
    if a.source is not None:
        kw["sources"] = a.source
    if a.dest is not None:
        kw["destination"] = a.dest
    if a.mode is not None:
        kw["mode"] = a.mode
    if a.include is not None:
        kw["includes"] = a.include
    if a.exclude is not None:
        kw["excludes"] = a.exclude
    if a.keep is not None:
        kw["keep"] = a.keep
    if a.keep_days is not None:
        kw["keep_days"] = a.keep_days
    if a.schedule is not None:
        kw["schedule"] = a.schedule
    if a.enabled is not None:
        kw["enabled"] = a.enabled
    return kw


def cmd_job_add(a):
    store = _store(a)
    job = {"name": a.name, **_job_kwargs(a)}
    job.setdefault("sources", [])
    store.add_job(job)
    print(f"Added job {a.name!r}.")


def cmd_job_edit(a):
    store = _store(a)
    changes = _job_kwargs(a)
    if a.rename:
        changes["name"] = a.rename
    if not changes:
        raise BackupKitError("nothing to change (pass at least one option)")
    store.update_job(a.name, **changes)
    print(f"Updated job {a.name!r}.")


def cmd_job_remove(a):
    _store(a).remove_job(a.name)
    print(f"Removed job {a.name!r}.")


def cmd_job_list(a):
    jobs = _store(a).list_jobs()
    if not jobs:
        print("(no jobs)")
        return
    for j in jobs:
        flag = "" if j["enabled"] else "  [disabled]"
        print(f"{j['name']}  [{j['mode']}]{flag}")
        print(f"    sources: {', '.join(j['sources']) or '(none)'}")
        print(f"    dest:    {j['destination'] or '(none)'}")
        if j["includes"]:
            print(f"    include: {', '.join(j['includes'])}")
        if j["excludes"]:
            print(f"    exclude: {', '.join(j['excludes'])}")
        if j["mode"] == "versioned":
            keep = j["keep"] or "unlimited"
            days = f", {j['keep_days']}d" if j["keep_days"] else ""
            print(f"    keep:    {keep}{days}")
        if j["schedule"]:
            print(f"    schedule: {j['schedule']}")


# --- run / verify / versions / restore --------------------------------------

def cmd_run(a):
    store = _store(a)
    if a.all:
        jobs = [j for j in store.list_jobs() if j["enabled"]]
        if not jobs:
            print("(no enabled jobs to run)")
            return
    else:
        if not a.name:
            raise BackupKitError("give a job name or use --all")
        jobs = [store.get_job(a.name)]
    failed = False
    for job in jobs:
        rep = run_backup(job)
        _print_report(rep)
        if rep.get("errors"):
            failed = True
    if failed:
        raise BackupKitError("one or more files could not be backed up "
                             "(see messages above)")


def cmd_verify(a):
    # target may be a job name or a directory/manifest path
    target = a.target
    store = _store(a)
    if store.has_job(target):
        job = store.get_job(target)
        if job["mode"] == "versioned":
            versions = list_versions(job)
            if not versions:
                raise BackupKitError(f"job {target!r} has no versions yet")
            target = versions[0]["path"]
        else:
            target = job["destination"]
    results = verify_backup(target)
    print(f"Verified {results['total']} file(s) in {results['base_dir']}:")
    print(f"    ok={len(results['ok'])}  missing={len(results['missing'])}  "
          f"mismatched={len(results['mismatched'])}  "
          f"errors={len(results['errors'])}")
    for rel in results["missing"]:
        print(f"    MISSING    {rel}")
    for rel in results["mismatched"]:
        print(f"    MISMATCH   {rel}")
    for err in results["errors"]:
        print(f"    ERROR      {err}")
    if not results["passed"]:
        raise BackupKitError("verification found problems")
    print("    OK — every file matches the manifest.")


def cmd_versions(a):
    job = _store(a).get_job(a.name)
    versions = list_versions(job)
    if not versions:
        print(f"(job {a.name!r} has no versions)")
        return
    for v in versions:
        when = v["mtime"].isoformat(timespec="seconds") if v["mtime"] else "?"
        print(f"{v['name']}   {when}   {v['path']}")


def cmd_restore(a):
    job = _store(a).get_job(a.name)
    versions = {v["name"]: v for v in list_versions(job)}
    if not versions:
        raise BackupKitError(f"job {a.name!r} has no versions to restore")
    if a.version:
        v = versions.get(a.version)
        if v is None:
            raise BackupKitError(f"no version {a.version!r} for job {a.name!r}")
        version_dir = v["path"]
    else:
        version_dir = sorted(versions.values(),
                             key=lambda x: x["name"])[-1]["path"]
    rep = restore(version_dir, a.dest, subpaths=a.subpath or None,
                  overwrite=a.overwrite)
    print(f"Restored {rep['restored']} file(s) to {a.dest} "
          f"({_fmt_bytes(rep['bytes'])}), skipped {rep['skipped']}.")
    for err in rep["errors"]:
        print(f"    ! {err}", file=sys.stderr)
    if rep["skipped"] and not a.overwrite:
        print("    (existing files were left in place; pass --overwrite to replace)")


def cmd_schedule(a):
    job = _store(a).get_job(a.name)
    rep = schedule.register(job, a.interval, store_path=getattr(a, "store", None))
    # remember the spec on the job for reference
    try:
        _store(a).update_job(a.name, schedule=a.interval)
    except BackupKitError:
        pass
    if rep["supported"]:
        print(f"Scheduled task {rep['task']!r}: {rep['message'] or 'created'}")
    else:
        print(f"[{rep['task']}] {rep['message']}")
        print(f"    command: {rep['command_str']}")


def cmd_unschedule(a):
    job = _store(a).get_job(a.name)
    rep = schedule.unregister(job)
    try:
        _store(a).update_job(a.name, schedule=None)
    except BackupKitError:
        pass
    if rep["supported"]:
        print(f"Removed task {rep['task']!r}: {rep['message'] or 'deleted'}")
    else:
        print(f"[{rep['task']}] {rep['message']}")


# --- parser -----------------------------------------------------------------

def _add_job_fields(p, for_add):
    p.add_argument("--source", action="append",
                   help="source folder (repeat for several)")
    p.add_argument("--dest", help="destination folder / drive / UNC path")
    p.add_argument("--mode", choices=("mirror", "incremental", "versioned"),
                   default=("incremental" if for_add else None),
                   help="backup mode")
    p.add_argument("--include", action="append",
                   help="glob to include (repeat); only matches are backed up")
    p.add_argument("--exclude", action="append",
                   help="glob to exclude (repeat)")
    p.add_argument("--keep", type=int,
                   help="versioned: keep newest N versions (0 = unlimited)")
    p.add_argument("--keep-days", type=int, dest="keep_days",
                   help="versioned: also drop versions older than N days")
    p.add_argument("--schedule", help="opaque schedule spec, stored only")
    grp = p.add_mutually_exclusive_group()
    grp.add_argument("--enabled", dest="enabled", action="store_const",
                     const=True, default=None, help="enable the job")
    grp.add_argument("--disabled", dest="enabled", action="store_const",
                     const=False, help="disable the job")


def build_parser():
    parser = argparse.ArgumentParser(
        prog="backupkit",
        description="Offline backup & sync toolkit (100%% open source).")
    parser.add_argument("--store", help="path to a specific jobs.json")
    sub = parser.add_subparsers(dest="command", required=True)

    # job ...
    jp = sub.add_parser("job", help="manage backup jobs")
    jsub = jp.add_subparsers(dest="job_command", required=True)

    add = jsub.add_parser("add", help="add a new job")
    add.add_argument("name")
    _add_job_fields(add, for_add=True)
    add.set_defaults(func=cmd_job_add)

    edit = jsub.add_parser("edit", help="edit an existing job")
    edit.add_argument("name")
    edit.add_argument("--rename", help="new name for the job")
    _add_job_fields(edit, for_add=False)
    edit.set_defaults(func=cmd_job_edit)

    rm = jsub.add_parser("remove", help="remove a job")
    rm.add_argument("name")
    rm.set_defaults(func=cmd_job_remove)

    ls = jsub.add_parser("list", help="list all jobs")
    ls.set_defaults(func=cmd_job_list)

    # run
    run = sub.add_parser("run", help="run a job (or --all enabled jobs)")
    run.add_argument("name", nargs="?")
    run.add_argument("--all", action="store_true", help="run all enabled jobs")
    run.set_defaults(func=cmd_run)

    # verify
    ver = sub.add_parser("verify", help="verify a job or a backup directory")
    ver.add_argument("target", help="job name, backup dir, or manifest.json")
    ver.set_defaults(func=cmd_verify)

    # versions
    vs = sub.add_parser("versions", help="list versions of a versioned job")
    vs.add_argument("name")
    vs.set_defaults(func=cmd_versions)

    # restore
    rs = sub.add_parser("restore", help="restore a version to a folder")
    rs.add_argument("name")
    rs.add_argument("--version", help="version timestamp (default: newest)")
    rs.add_argument("--dest", required=True, help="folder to restore into")
    rs.add_argument("--subpath", action="append",
                    help="restore only this relative path (repeat)")
    rs.add_argument("--overwrite", action="store_true",
                    help="replace existing files (default: skip them)")
    rs.set_defaults(func=cmd_restore)

    # schedule / unschedule
    sc = sub.add_parser("schedule", help="register a Windows scheduled task")
    sc.add_argument("name")
    sc.add_argument("--interval", required=True,
                    help="e.g. 30m, 2h, hourly, daily")
    sc.set_defaults(func=cmd_schedule)

    us = sub.add_parser("unschedule", help="remove a job's scheduled task")
    us.add_argument("name")
    us.set_defaults(func=cmd_unschedule)

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        args.func(args)
    except BackupKitError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
