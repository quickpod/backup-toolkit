#!/usr/bin/env python3
r"""Backup Toolkit -- an Aura (QuickOpen design system) GUI on top of the
``backupkit`` API.

A single Aura window: a left sidebar (Jobs, Run, Versions / Restore, Verify,
Schedule, About) and a main panel that swaps to the selected section.  Every
operation calls the tested core library (never re-implements backup logic) and
long runs happen on a background thread so the UI stays responsive; results
are marshalled back with ``self.after`` and shown in the Aura status bar -- a
summary plus an "Open folder" button on success, or the ``BackupKitError``
message (never a raw traceback) on failure.

Design goals baked in here:
  * built on the vendored ``backupkit/aura.py`` design system, which layers
    the quickopen.ai look (deep space + light) over CustomTkinter.  Runtime
    deps: ``customtkinter`` (+ ``darkdetect``) — declared in requirements.txt;
    the PyInstaller build adds ``--collect-all customtkinter``.
  * Importing this module does nothing.  Only :func:`main` builds a root
    window, and it degrades gracefully (prints a message, returns 0) with no
    display or with customtkinter missing.
  * Frozen-exe safe: bundled assets are resolved via ``sys._MEIPASS`` / the
    exe directory when ``sys.frozen`` is set -- never ``__file__``.
  * Destructive by consent: a mirror run (which deletes files in the
    destination) always asks for confirmation first.

100% AI-built, open source, published on QuickOpen (quickopen.ai).
"""

from __future__ import annotations

import os
import sys
import threading

# NOTE: tkinter/customtkinter are imported lazily inside main()/build_app so
# that merely importing this module (e.g. during packaging or on a headless CI
# box) never fails.

APP_NAME = "Backup Toolkit"
APP_VERSION = "1.0.0"
WINDOW_TITLE = "Backup Toolkit — by QuickOpen (quickopen.ai)"
PROJECT_URL = "https://quickopen.ai"
ACCENT = "#17914b"      # publish/specs/backup-toolkit.json UI accent

MODE_LABELS = [
    ("incremental", "Incremental — copy new/changed files, never delete"),
    ("mirror", "Mirror — make destination match sources (DELETES extras)"),
    ("versioned", "Versioned — timestamped snapshots, hard-linked & pruned"),
]

SECTIONS = [
    ("jobs", "Jobs"),
    ("run", "Run"),
    ("versions", "Versions / Restore"),
    ("verify", "Verify"),
    ("schedule", "Schedule"),
]

SECTION_DESC = {
    "jobs": "Create and edit backup jobs: sources, destination, mode, filters "
            "and version retention.",
    "run": "Pick a job and run it. Progress and a summary appear below; mirror "
           "runs ask before deleting.",
    "versions": "Browse the timestamped versions of a versioned job and restore "
                "one to a folder.",
    "verify": "Re-hash a backup and compare against its manifest to catch bit-rot "
              "or tampering.",
    "schedule": "Register a Windows Scheduled Task so a job runs automatically "
                "(Windows only).",
}


# ---------------------------------------------------------------------------
# Asset / frozen handling
# ---------------------------------------------------------------------------
def asset_path(name):
    """Locate a bundled asset from source OR a PyInstaller one-file build."""
    roots = []
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            roots.append(meipass)
        roots.append(os.path.dirname(os.path.abspath(sys.executable)))
    else:
        here = os.path.dirname(os.path.abspath(__file__))
        roots += [here, os.path.dirname(here), os.getcwd()]
    for root in roots:
        candidate = os.path.join(root, name)
        if os.path.exists(candidate):
            return candidate
    return None


def human_size(num_bytes):
    size = float(num_bytes or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024.0 or unit == "TB":
            return f"{int(size)}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024.0
    return f"{size:.1f}TB"


def open_in_file_manager(path):
    """Best-effort 'reveal in file manager', guarded on every platform."""
    try:
        folder = path if os.path.isdir(path) else os.path.dirname(os.path.abspath(path))
        if hasattr(os, "startfile"):          # Windows
            os.startfile(folder)              # noqa: S606 - intended
        elif sys.platform == "darwin":
            import subprocess
            subprocess.Popen(["open", folder])
        else:
            import subprocess
            subprocess.Popen(["xdg-open", folder])
        return True
    except Exception:
        return False


def open_with_default_app(path):
    try:
        if hasattr(os, "startfile"):
            os.startfile(path)                # noqa: S606
        elif sys.platform == "darwin":
            import subprocess
            subprocess.Popen(["open", path])
        else:
            import subprocess
            subprocess.Popen(["xdg-open", path])
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# The app (built lazily; tkinter/customtkinter imported only inside build_app)
# ---------------------------------------------------------------------------
def build_app():
    """Construct and return the App class bound to live GUI imports."""
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox
    import customtkinter as ctk

    from . import aura, guiconfig, schedule
    from .config import JobStore, MODES  # noqa: F401 - MODES documents modes
    from .engine import run_backup
    from .errors import BackupKitError
    # NOTE: backupkit/__init__ rebinds the package attribute 'restore' to the
    # restore() FUNCTION, so 'from . import restore' would NOT get the module.
    from .restore import list_versions, restore
    from .verify import verify_backup

    class App(aura.AuraApp):
        def __init__(self):
            super().__init__(
                title=WINDOW_TITLE, app_name=APP_NAME, accent=ACCENT,
                theme=guiconfig.get_theme(),
                icon_png=asset_path("backup-toolkit.png"),
                version=APP_VERSION, tagline="offline backups",
                on_theme_change=guiconfig.set_theme,
                size=(1160, 700), min_size=(980, 580))

            self.store = JobStore()
            self._busy = False
            self._cancel_flag = False
            self._img_refs_gui = []
            self._last_output_dir = None
            self._versions_cache = []

            self._set_icon()
            self._build_menu()
            self.add_section("jobs", "Jobs", "▤", self._section_jobs)
            self.add_section("run", "Run", "⇄", self._section_run)
            self.add_section("versions", "Versions / Restore", "⛁",
                             self._section_versions)
            self.add_section("verify", "Verify", "◉", self._section_verify)
            self.add_section("schedule", "Schedule", "↻",
                             self._section_schedule)
            self.add_section("about", "About", "ℹ", self._section_about)

            # "Open folder" lives in the status bar; shown only on success
            self.openfolder_btn = aura.AuraButton(
                self.statusbar.actions, "Open folder", kind="secondary",
                height=30, command=self._open_last_folder)

            self.show("jobs")
            self.set_status("Ready")
            self.protocol("WM_DELETE_WINDOW", self._on_close)

        # ---- assets / icon
        def _set_icon(self):
            try:
                ico = asset_path("backup-toolkit.ico")
                if ico and os.name == "nt":
                    self.iconbitmap(ico)
                    return
            except Exception:
                pass
            try:
                png = asset_path("backup-toolkit.png")
                if png:
                    img = tk.PhotoImage(file=png)
                    self._img_refs_gui.append(img)
                    self.iconphoto(True, img)
            except Exception:
                pass  # icon is cosmetic; never block launch

        # ---- menu (native menus stay; theme lives in the sidebar toggle too)
        def _build_menu(self):
            bar = tk.Menu(self)
            filem = tk.Menu(bar, tearoff=0)
            filem.add_command(label="Refresh jobs", command=self._reload_jobs)
            filem.add_separator()
            filem.add_command(label="Exit", command=self._on_close)
            bar.add_cascade(label="File", menu=filem)

            viewm = tk.Menu(bar, tearoff=0)
            viewm.add_command(
                label="Toggle dark mode",
                command=lambda: self.set_theme(
                    "light" if self.theme == "dark" else "dark"))
            bar.add_cascade(label="View", menu=viewm)

            helpm = tk.Menu(bar, tearoff=0)
            helpm.add_command(label="About", command=lambda: self.show("about"))
            helpm.add_command(label="Open project page (quickopen.ai)",
                              command=lambda: open_with_default_app(PROJECT_URL))
            bar.add_cascade(label="Help", menu=helpm)
            self.configure(menu=bar)

        # ---- section switching (panels are lazy + persistent; refresh data)
        def show(self, sid):
            super().show(sid)
            self._refresh_section(sid)

        def _refresh_section(self, sid):
            if sid == "jobs" and hasattr(self, "jobs_list"):
                self._reload_job_listbox()
            elif sid == "run" and hasattr(self, "_run_combo"):
                self._set_combo(self._run_combo, self._run_job,
                                self._job_names())
            elif sid == "versions" and hasattr(self, "_ver_combo"):
                self._refresh_versioned_jobs()
            elif sid == "verify" and hasattr(self, "_vf_combo"):
                self._set_combo(self._vf_combo, self._vf_job,
                                self._job_names())
            elif sid == "schedule" and hasattr(self, "_sc_combo"):
                self._set_combo(self._sc_combo, self._sc_job,
                                self._job_names())

        @staticmethod
        def _set_combo(combo, var, names):
            combo.configure(values=names)
            if names and var.get() not in names:
                var.set(names[0])
            elif not names:
                var.set("")

        def _reload_jobs(self):
            self._refresh_section(self.active_section)
            self._set_status("Ready")

        def _job_names(self):
            try:
                return [j["name"] for j in self.store.list_jobs()]
            except Exception:
                return []

        @staticmethod
        def _desc(parent, sid):
            aura.Caption(parent, SECTION_DESC[sid], wraplength=760,
                         justify="left").pack(anchor="w", pady=(0, 10))

        # ================= Jobs section =================
        def _section_jobs(self, parent):
            self._desc(parent, "jobs")
            body = ctk.CTkFrame(parent, fg_color="transparent")
            body.pack(fill="both", expand=True)

            left = aura.Card(body, title="Jobs")
            left.pack(side="left", fill="y", padx=(0, 14))
            self.jobs_list = tk.Listbox(left.body, height=16, width=24,
                                        activestyle="none",
                                        exportselection=False)
            self.jobs_list.pack(fill="y", expand=True, pady=(0, 8))
            aura.track(self.jobs_list, "listbox")
            self.jobs_list.bind("<<ListboxSelect>>",
                                lambda e: self._load_selected_job())
            row = ctk.CTkFrame(left.body, fg_color="transparent")
            row.pack(fill="x")
            aura.AuraButton(row, "New", kind="secondary",
                            command=self._new_job).pack(side="left")
            aura.AuraButton(row, "Delete", kind="danger",
                            command=self._delete_job).pack(side="left", padx=6)

            self._reload_job_listbox()

            card = aura.Card(body, title="Job details")
            card.pack(side="left", fill="both", expand=True)
            form = card.body

            def label(r, text, anchor="w"):
                ctk.CTkLabel(r, text=text, width=110, anchor=anchor,
                             font=aura.font()).pack(
                    side="left", anchor="n" if anchor == "nw" else "center")

            def field(text, widget):
                r = ctk.CTkFrame(form, fg_color="transparent")
                r.pack(fill="x", pady=3)
                label(r, text)
                widget(r)

            self._jf_name = tk.StringVar()
            field("Name", lambda r: aura.AuraEntry(
                r, textvariable=self._jf_name).pack(
                side="left", fill="x", expand=True))

            # sources listbox with add/remove
            sr = ctk.CTkFrame(form, fg_color="transparent")
            sr.pack(fill="x", pady=3)
            label(sr, "Sources", anchor="nw")
            sbox = ctk.CTkFrame(sr, fg_color="transparent")
            sbox.pack(side="left", fill="x", expand=True)
            self._jf_sources = tk.Listbox(sbox, height=4, activestyle="none",
                                          exportselection=False)
            self._jf_sources.pack(fill="x")
            aura.track(self._jf_sources, "listbox")
            sbtn = ctk.CTkFrame(sbox, fg_color="transparent")
            sbtn.pack(fill="x", pady=(6, 0))
            aura.AuraButton(sbtn, "Add folder…", kind="secondary",
                            command=self._add_source).pack(side="left")
            aura.AuraButton(sbtn, "Remove", kind="secondary",
                            command=self._remove_source).pack(
                side="left", padx=6)

            self._jf_dest = tk.StringVar()
            dr = ctk.CTkFrame(form, fg_color="transparent")
            dr.pack(fill="x", pady=3)
            label(dr, "Destination")
            aura.AuraEntry(dr, textvariable=self._jf_dest).pack(
                side="left", fill="x", expand=True, padx=(0, 8))
            aura.AuraButton(dr, "Browse…", kind="secondary",
                            command=self._browse_dest).pack(side="left")

            mr = ctk.CTkFrame(form, fg_color="transparent")
            mr.pack(fill="x", pady=3)
            label(mr, "Mode", anchor="nw")
            mcol = ctk.CTkFrame(mr, fg_color="transparent")
            mcol.pack(side="left", fill="x", expand=True)
            self._jf_mode = tk.StringVar(value="incremental")
            for value, text in MODE_LABELS:
                ctk.CTkRadioButton(mcol, text=text, value=value,
                                   variable=self._jf_mode,
                                   font=aura.font()).pack(anchor="w", pady=2)

            self._jf_includes = tk.StringVar()
            field("Include globs", lambda r: aura.AuraEntry(
                r, textvariable=self._jf_includes).pack(
                side="left", fill="x", expand=True))
            self._jf_excludes = tk.StringVar()
            field("Exclude globs", lambda r: aura.AuraEntry(
                r, textvariable=self._jf_excludes).pack(
                side="left", fill="x", expand=True))
            aura.Caption(form,
                         "(comma-separated, e.g. *.tmp, cache/*)").pack(
                anchor="w", padx=(110, 0))

            kr = ctk.CTkFrame(form, fg_color="transparent")
            kr.pack(fill="x", pady=3)
            label(kr, "Keep (versioned)")
            self._jf_keep = tk.StringVar(value="0")
            ttk.Spinbox(kr, from_=0, to=999, width=6,
                        textvariable=self._jf_keep).pack(side="left")
            ctk.CTkLabel(kr, text="versions,   or days:",
                         font=aura.font()).pack(side="left", padx=8)
            self._jf_keepdays = tk.StringVar(value="0")
            ttk.Spinbox(kr, from_=0, to=3650, width=6,
                        textvariable=self._jf_keepdays).pack(side="left")

            self._jf_enabled = tk.BooleanVar(value=True)
            ctk.CTkCheckBox(form,
                            text="Enabled (included in “run all” and schedules)",
                            variable=self._jf_enabled,
                            font=aura.font()).pack(anchor="w", pady=(8, 0))

            br = ctk.CTkFrame(form, fg_color="transparent")
            br.pack(fill="x", pady=(12, 0))
            self._jf_save = aura.AuraButton(br, "Save job",
                                            command=self._save_job)
            self._jf_save.pack(side="left")
            self._editing = None  # name being edited, or None for new
            self._new_job()

        def _reload_job_listbox(self):
            self.jobs_list.delete(0, "end")
            for name in self._job_names():
                self.jobs_list.insert("end", name)

        def _new_job(self):
            self._editing = None
            self._jf_name.set("")
            self._jf_sources.delete(0, "end")
            self._jf_dest.set("")
            self._jf_mode.set("incremental")
            self._jf_includes.set("")
            self._jf_excludes.set("")
            self._jf_keep.set("0")
            self._jf_keepdays.set("0")
            self._jf_enabled.set(True)
            self._jf_save.configure(text="Add job")

        def _load_selected_job(self):
            sel = self.jobs_list.curselection()
            if not sel:
                return
            name = self.jobs_list.get(sel[0])
            try:
                job = self.store.get_job(name)
            except BackupKitError:
                return
            self._editing = name
            self._jf_name.set(job["name"])
            self._jf_sources.delete(0, "end")
            for s in job["sources"]:
                self._jf_sources.insert("end", s)
            self._jf_dest.set(job["destination"])
            self._jf_mode.set(job["mode"])
            self._jf_includes.set(", ".join(job["includes"]))
            self._jf_excludes.set(", ".join(job["excludes"]))
            self._jf_keep.set(str(job["keep"]))
            self._jf_keepdays.set(str(job["keep_days"]))
            self._jf_enabled.set(job["enabled"])
            self._jf_save.configure(text="Save job")

        def _add_source(self):
            d = filedialog.askdirectory(title="Choose a source folder")
            if d:
                self._jf_sources.insert("end", d)

        def _remove_source(self):
            for i in reversed(self._jf_sources.curselection()):
                self._jf_sources.delete(i)

        def _browse_dest(self):
            d = filedialog.askdirectory(title="Choose the destination folder")
            if d:
                self._jf_dest.set(d)

        @staticmethod
        def _split_globs(text):
            return [g.strip() for g in text.split(",") if g.strip()]

        def _collect_job(self):
            return {
                "name": self._jf_name.get().strip(),
                "sources": list(self._jf_sources.get(0, "end")),
                "destination": self._jf_dest.get().strip(),
                "mode": self._jf_mode.get(),
                "includes": self._split_globs(self._jf_includes.get()),
                "excludes": self._split_globs(self._jf_excludes.get()),
                "keep": int(self._jf_keep.get() or 0),
                "keep_days": int(self._jf_keepdays.get() or 0),
                "enabled": bool(self._jf_enabled.get()),
            }

        def _save_job(self):
            try:
                job = self._collect_job()
                if self._editing:
                    self.store.update_job(self._editing, **job)
                else:
                    self.store.add_job(job)
            except (BackupKitError, ValueError) as exc:
                self._show_error(str(exc))
                return
            self._editing = job["name"]
            self._reload_job_listbox()
            self._jf_save.configure(text="Save job")
            self.report_success(f"Saved job “{job['name']}”.")

        def _delete_job(self):
            sel = self.jobs_list.curselection()
            if not sel:
                return
            name = self.jobs_list.get(sel[0])
            if not messagebox.askyesno("Delete job", f"Delete job “{name}”?"):
                return
            try:
                self.store.remove_job(name)
            except BackupKitError as exc:
                self._show_error(str(exc))
                return
            self._reload_job_listbox()
            self._new_job()
            self.report_success(f"Deleted job “{name}”.")

        # ================= Run section =================
        def _section_run(self, parent):
            self._desc(parent, "run")
            top = ctk.CTkFrame(parent, fg_color="transparent")
            top.pack(fill="x")
            ctk.CTkLabel(top, text="Job:", width=60, anchor="w",
                         font=aura.font()).pack(side="left")
            self._run_job = tk.StringVar()
            names = self._job_names()
            self._run_combo = aura.AuraCombo(top, variable=self._run_job,
                                             values=names, state="readonly",
                                             width=260)
            if names:
                self._run_job.set(names[0])
            self._run_combo.pack(side="left")
            self._run_btn = aura.AuraButton(top, "Run backup",
                                            command=self._do_run)
            self._run_btn.pack(side="left", padx=10)
            self._cancel_btn = aura.AuraButton(top, "Cancel",
                                               kind="secondary",
                                               command=self._cancel_run,
                                               state="disabled")
            self._cancel_btn.pack(side="left")

            self._run_prog = aura.ProgressBar(parent)
            self._run_prog.pack(fill="x", pady=(12, 0))

            logframe = ctk.CTkFrame(parent, fg_color="transparent")
            logframe.pack(fill="both", expand=True, pady=(12, 0))
            self._run_log = tk.Text(logframe, height=18, wrap="none",
                                    borderwidth=0)
            sb = ttk.Scrollbar(logframe, orient="vertical",
                               command=self._run_log.yview)
            self._run_log.configure(yscrollcommand=sb.set, state="disabled")
            sb.pack(side="right", fill="y")
            self._run_log.pack(side="left", fill="both", expand=True)
            aura.track(self._run_log, "text")

        def _log(self, line):
            self._run_log.configure(state="normal")
            self._run_log.insert("end", line + "\n")
            self._run_log.see("end")
            self._run_log.configure(state="disabled")

        def _cancel_run(self):
            self._cancel_flag = True
            self._set_status("cancelling…", kind="working")

        def _do_run(self):
            name = self._run_job.get()
            if not name:
                self._show_error("Choose a job to run.")
                return
            try:
                job = self.store.get_job(name)
            except BackupKitError as exc:
                self._show_error(str(exc))
                return
            if job["mode"] == "mirror":
                if not messagebox.askyesno(
                        "Mirror deletes files",
                        "Mirror mode makes the destination match the sources and "
                        "will DELETE any file in the destination that is not in a "
                        "source.\n\nRun the mirror backup now?"):
                    return
            self._run_log.configure(state="normal")
            self._run_log.delete("1.0", "end")
            self._run_log.configure(state="disabled")
            self._log(f"Running “{name}” ({job['mode']})…")
            self._cancel_flag = False
            self._cancel_btn.configure(state="normal")
            self._run_prog.set(0)

            def progress(ev):
                self.after(0, lambda e=ev: self._on_progress(e))

            def cancel():
                return self._cancel_flag

            self._bg(lambda: run_backup(job, progress=progress, cancel=cancel),
                     self._run_done, button=self._run_btn, busy="Backing up…")

        def _on_progress(self, ev):
            total = ev.get("total") or 0
            idx = ev.get("index") or 0
            if total:
                # aura.ProgressBar is 0..1, never maximum/value
                self._run_prog.set(min(1.0, idx / max(1, total)))
            action = ev.get("action")
            if action in ("copy", "link", "delete"):
                self._log(f"  {action:<6} {ev.get('path','')}")

        def _run_done(self, rep):
            self._cancel_btn.configure(state="disabled")
            self._log("")
            summary = (f"copied {rep['copied']}, linked {rep.get('linked',0)}, "
                       f"skipped {rep['skipped']}, deleted {rep['deleted']}, "
                       f"{human_size(rep['bytes'])}")
            if rep.get("cancelled"):
                self._log("Cancelled. " + summary)
                self._show_error("Run cancelled.")
                return
            self._run_prog.set(1.0)
            self._log("Done: " + summary)
            if rep.get("version_dir"):
                self._log("Version: " + rep["version_dir"])
            for err in rep.get("errors", []):
                self._log("  ! " + err)
            out = rep.get("version_dir") or rep.get("dest")
            if rep.get("errors"):
                self.report_success(summary + f" — {len(rep['errors'])} error(s)",
                                    [out])
            else:
                self.report_success(summary, [out])

        # ================= Versions / Restore section =================
        def _section_versions(self, parent):
            self._desc(parent, "versions")
            top = ctk.CTkFrame(parent, fg_color="transparent")
            top.pack(fill="x")
            ctk.CTkLabel(top, text="Job:", width=60, anchor="w",
                         font=aura.font()).pack(side="left")
            self._ver_job = tk.StringVar()
            self._ver_combo = aura.AuraCombo(top, variable=self._ver_job,
                                             values=[], state="readonly",
                                             width=260)
            self._ver_combo.pack(side="left")
            aura.AuraButton(top, "List versions", kind="secondary",
                            command=self._list_versions).pack(
                side="left", padx=10)
            self._ver_hint = aura.Caption(
                parent, "", wraplength=760, justify="left")

            self._ver_list = tk.Listbox(parent, height=12, activestyle="none",
                                        exportselection=False)
            self._ver_list.pack(fill="both", expand=True, pady=(12, 8))
            aura.track(self._ver_list, "listbox")

            row = ctk.CTkFrame(parent, fg_color="transparent")
            row.pack(fill="x")
            aura.AuraButton(row, "Restore selected…",
                            command=self._restore_selected).pack(side="left")
            self._ver_overwrite = tk.BooleanVar(value=False)
            ctk.CTkCheckBox(row, text="Overwrite existing files",
                            variable=self._ver_overwrite,
                            font=aura.font()).pack(side="left", padx=12)
            self._versions_cache = []
            self._refresh_versioned_jobs()

        def _refresh_versioned_jobs(self):
            try:
                names = [j["name"] for j in self.store.list_jobs()
                         if j["mode"] == "versioned"]
            except Exception:
                names = []
            self._set_combo(self._ver_combo, self._ver_job, names)
            if names:
                self._ver_hint.pack_forget()
            else:
                self._ver_hint.configure(
                    text="No versioned jobs yet — set a job's mode to "
                         "“versioned” to keep timestamped snapshots.")
                self._ver_hint.pack(anchor="w", pady=(8, 0),
                                    after=self._ver_combo.master)

        def _list_versions(self):
            name = self._ver_job.get()
            if not name:
                self._show_error("Choose a versioned job.")
                return
            try:
                job = self.store.get_job(name)
                versions = list_versions(job)
            except BackupKitError as exc:
                self._show_error(str(exc))
                return
            self._versions_cache = versions
            self._ver_list.delete(0, "end")
            for v in versions:
                when = v["mtime"].strftime("%Y-%m-%d %H:%M:%S") if v["mtime"] else "?"
                self._ver_list.insert("end", f"{v['name']}    ({when})")
            if not versions:
                self.report_success(f"Job “{name}” has no versions yet.")
            else:
                self._set_status(f"{len(versions)} version(s)")

        def _restore_selected(self):
            sel = self._ver_list.curselection()
            if not sel or not self._versions_cache:
                self._show_error("Pick a version to restore.")
                return
            version = self._versions_cache[sel[0]]
            dest = filedialog.askdirectory(title="Restore into which folder?")
            if not dest:
                return
            overwrite = bool(self._ver_overwrite.get())
            self._bg(lambda: restore(version["path"], dest, overwrite=overwrite),
                     lambda rep: self.report_success(
                         f"Restored {rep['restored']} file(s) "
                         f"({human_size(rep['bytes'])}), skipped {rep['skipped']}.",
                         [dest]),
                     busy="Restoring…")

        # ================= Verify section =================
        def _section_verify(self, parent):
            self._desc(parent, "verify")
            top = ctk.CTkFrame(parent, fg_color="transparent")
            top.pack(fill="x")
            ctk.CTkLabel(top, text="Job:", width=60, anchor="w",
                         font=aura.font()).pack(side="left")
            self._vf_job = tk.StringVar()
            names = self._job_names()
            self._vf_combo = aura.AuraCombo(top, variable=self._vf_job,
                                            values=names, state="readonly",
                                            width=260)
            if names:
                self._vf_job.set(names[0])
            self._vf_combo.pack(side="left")
            aura.AuraButton(top, "Verify job",
                            command=self._verify_job).pack(side="left", padx=10)
            aura.AuraButton(top, "Verify a folder…", kind="secondary",
                            command=self._verify_folder).pack(side="left")

            self._vf_out = tk.Text(parent, height=18, wrap="none",
                                   borderwidth=0, state="disabled")
            self._vf_out.pack(fill="both", expand=True, pady=(12, 0))
            aura.track(self._vf_out, "text")

        def _verify_target(self, target):
            self._bg(lambda: verify_backup(target), self._verify_done,
                     busy="Verifying…")

        def _verify_job(self):
            name = self._vf_job.get()
            if not name:
                self._show_error("Choose a job to verify.")
                return
            try:
                job = self.store.get_job(name)
                if job["mode"] == "versioned":
                    versions = list_versions(job)
                    if not versions:
                        self._show_error(f"Job “{name}” has no versions yet.")
                        return
                    target = versions[0]["path"]
                else:
                    target = job["destination"]
            except BackupKitError as exc:
                self._show_error(str(exc))
                return
            self._verify_target(target)

        def _verify_folder(self):
            d = filedialog.askdirectory(title="Choose a backup folder to verify")
            if d:
                self._verify_target(d)

        def _verify_done(self, res):
            self._vf_out.configure(state="normal")
            self._vf_out.delete("1.0", "end")
            self._vf_out.insert("end",
                f"Verified {res['total']} file(s) in {res['base_dir']}\n"
                f"  ok={len(res['ok'])}  missing={len(res['missing'])}  "
                f"mismatched={len(res['mismatched'])}  errors={len(res['errors'])}\n\n")
            for rel in res["missing"]:
                self._vf_out.insert("end", f"  MISSING    {rel}\n")
            for rel in res["mismatched"]:
                self._vf_out.insert("end", f"  MISMATCH   {rel}\n")
            for err in res["errors"]:
                self._vf_out.insert("end", f"  ERROR      {err}\n")
            if res["passed"]:
                self._vf_out.insert("end", "  OK — every file matches the manifest.\n")
            self._vf_out.configure(state="disabled")
            if res["passed"]:
                self.report_success("Verification passed.")
            else:
                self._show_error(f"{len(res['missing'])} missing / "
                                 f"{len(res['mismatched'])} mismatched")

        # ================= Schedule section =================
        def _section_schedule(self, parent):
            self._desc(parent, "schedule")
            note = ("Scheduling registers a Windows Scheduled Task (via schtasks) "
                    "so a job runs automatically. This is Windows-only; on other "
                    "platforms the command is shown but not registered. Backups "
                    "themselves run everywhere.")
            aura.Caption(parent, note, wraplength=700, justify="left").pack(
                anchor="w", pady=(0, 10))
            top = ctk.CTkFrame(parent, fg_color="transparent")
            top.pack(fill="x")
            ctk.CTkLabel(top, text="Job:", width=60, anchor="w",
                         font=aura.font()).pack(side="left")
            self._sc_job = tk.StringVar()
            names = self._job_names()
            self._sc_combo = aura.AuraCombo(top, variable=self._sc_job,
                                            values=names, state="readonly",
                                            width=260)
            if names:
                self._sc_job.set(names[0])
            self._sc_combo.pack(side="left")

            ir = ctk.CTkFrame(parent, fg_color="transparent")
            ir.pack(fill="x", pady=10)
            ctk.CTkLabel(ir, text="Interval:", width=60, anchor="w",
                         font=aura.font()).pack(side="left")
            self._sc_interval = tk.StringVar(value="60m")
            aura.AuraCombo(ir, variable=self._sc_interval,
                           values=["30m", "60m", "2h", "hourly", "daily"],
                           width=130).pack(side="left")
            aura.AuraButton(ir, "Register task",
                            command=self._do_schedule).pack(side="left", padx=10)
            aura.AuraButton(ir, "Remove task", kind="secondary",
                            command=self._do_unschedule).pack(side="left")

            self._sc_out = tk.Text(parent, height=8, wrap="word", borderwidth=0,
                                   state="disabled")
            self._sc_out.pack(fill="both", expand=True, pady=(10, 0))
            aura.track(self._sc_out, "text")

        def _sc_write(self, text):
            self._sc_out.configure(state="normal")
            self._sc_out.delete("1.0", "end")
            self._sc_out.insert("end", text)
            self._sc_out.configure(state="disabled")

        def _do_schedule(self):
            name = self._sc_job.get()
            if not name:
                self._show_error("Choose a job.")
                return
            try:
                job = self.store.get_job(name)
                rep = schedule.register(job, self._sc_interval.get())
                self.store.update_job(name, schedule=self._sc_interval.get())
            except BackupKitError as exc:
                self._show_error(str(exc))
                return
            if rep["supported"]:
                self._sc_write(f"Registered task {rep['task']}.\n{rep['message']}")
                self.report_success(f"Scheduled “{name}”.")
            else:
                self._sc_write(rep["message"] + "\n\nCommand that would run:\n"
                               + rep["command_str"])
                self.report_success("Command shown (Windows-only feature).")

        def _do_unschedule(self):
            name = self._sc_job.get()
            if not name:
                self._show_error("Choose a job.")
                return
            try:
                job = self.store.get_job(name)
                rep = schedule.unregister(job)
                self.store.update_job(name, schedule=None)
            except BackupKitError as exc:
                self._show_error(str(exc))
                return
            self._sc_write(rep["message"])
            self.report_success(f"Unscheduled “{name}”." if rep["supported"]
                                else "Nothing to remove here (Windows-only).")

        # ================= About section =================
        def _section_about(self, parent):
            card = aura.Card(parent, title="About Backup Toolkit")
            card.pack(fill="x")
            aura.Heading(card.body, APP_NAME).pack(anchor="w")
            aura.Caption(card.body, f"Version {APP_VERSION}").pack(
                anchor="w", pady=(0, 10))
            ctk.CTkLabel(
                card.body, font=aura.font(), justify="left", anchor="w",
                wraplength=520,
                text="A fast, fully-offline backup & sync toolkit — mirror, "
                     "incremental and versioned backups with SHA-256 "
                     "verification and restore.\n\n"
                     "100% AI-built, open source, published on QuickOpen. "
                     "Nothing is ever uploaded anywhere.").pack(anchor="w")
            aura.Caption(card.body,
                         "Licensed under Apache-2.0. Built on the Python "
                         "standard library and CustomTkinter (MIT).").pack(
                anchor="w", pady=(10, 4))
            aura.AuraButton(card.body, "Project page: quickopen.ai",
                            kind="ghost",
                            command=lambda: open_with_default_app(
                                PROJECT_URL)).pack(anchor="w", pady=(6, 0))

        # ---- background operation runner
        def _bg(self, work, on_ok, button=None, busy="Working…"):
            if self._busy:
                self._show_error("Please wait — an operation is already running.")
                return
            self._busy = True
            if button is not None:
                try:
                    button.state(["disabled"])
                except Exception:
                    pass
            self._set_status(busy, kind="working")
            self._clear_result(keep_status=True)

            def run():
                try:
                    res, err = work(), None
                except BackupKitError as ex:
                    res, err = None, str(ex)
                except Exception as ex:
                    res, err = None, f"Unexpected error: {ex}"
                self.after(0, lambda: finish(res, err))

            def finish(res, err):
                self._busy = False
                if button is not None:
                    try:
                        button.state(["!disabled"])
                    except Exception:
                        pass
                if err is not None:
                    self._show_error(err)
                    return
                self._set_status("done", kind="ok")
                try:
                    on_ok(res)
                except Exception as ex:
                    self._show_error(f"Post-processing error: {ex}")

            threading.Thread(target=run, daemon=True).start()

        # ---- status bar helpers (errors go to the Aura status bar, never
        # a popup or a raw traceback)
        def _set_status(self, text, kind="idle"):
            self.set_status(text, kind)

        def _clear_result(self, keep_status=False):
            self.openfolder_btn.pack_forget()
            if not keep_status:
                self.set_status("Ready")

        def _show_error(self, message):
            self.set_error(message)
            self.openfolder_btn.pack_forget()

        def report_success(self, message, outputs=None):
            outputs = outputs or []
            first = next((o for o in outputs if o), None)
            if first:
                self._last_output_dir = (
                    first if os.path.isdir(first)
                    else os.path.dirname(os.path.abspath(first)))
                self.openfolder_btn.pack(side="left")
            else:
                self.openfolder_btn.pack_forget()
            self.set_success(message)

        def _open_last_folder(self):
            if self._last_output_dir:
                open_in_file_manager(self._last_output_dir)

        # ---- shutdown
        def _on_close(self):
            self.destroy()

    return App


def main():
    """Entry point: build the root window and run. Degrades on headless hosts.

    Importing this module does nothing; only this function creates a Tk root.
    With no display (e.g. a server) or without customtkinter installed, it
    prints a friendly note and returns 0 instead of raising.
    """
    try:
        import tkinter as tk
    except Exception as exc:  # tkinter missing entirely
        print(f"{APP_NAME}: a graphical environment with tkinter is required "
              f"to run the GUI ({exc}).")
        return 0

    try:
        App = build_app()
        app = App()
    except ImportError as exc:
        print(f"{APP_NAME}: the GUI needs the 'customtkinter' package "
              f"({exc}). Install it with:  pip install customtkinter")
        return 0
    except tk.TclError as exc:
        print(f"{APP_NAME}: no graphical display available — cannot start the "
              f"GUI here ({exc}). This app is intended for the Windows desktop.")
        return 0
    except Exception as exc:
        print(f"{APP_NAME}: could not start the GUI ({exc}).")
        return 1

    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
