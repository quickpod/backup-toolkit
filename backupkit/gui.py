#!/usr/bin/env python3
r"""Backup Toolkit -- a pure-stdlib tkinter GUI on top of the ``backupkit`` API.

A single main window: a left sidebar (Jobs, Run, Versions / Restore, Verify,
Schedule) and a main panel that swaps to the selected section.  Every operation
calls the tested core library (never re-implements backup logic) and long runs
happen on a background thread so the UI stays responsive; results are marshalled
back with ``self.after`` and shown in an inline result bar -- a summary plus an
"Open folder" button on success, or the ``BackupKitError`` message (never a raw
traceback) on failure.

Design goals baked in here:
  * pure standard-library tkinter/ttk -- NO third-party GUI deps.  Dark mode is
    a ttk-style + palette swap.
  * Importing this module does nothing.  Only :func:`main` builds a root window,
    and it degrades gracefully (prints a message, returns 0) with no display.
  * Frozen-exe safe: bundled assets are resolved via ``sys._MEIPASS`` / the exe
    directory when ``sys.frozen`` is set -- never ``__file__``.
  * Destructive by consent: a mirror run (which deletes files in the
    destination) always asks for confirmation first.

100% AI-built, open source, published on QuickOpen (quickopen.ai).
"""

from __future__ import annotations

import os
import sys
import threading

# NOTE: tkinter is imported lazily inside main()/build_app so that merely
# importing this module (e.g. during packaging or on a headless CI box) never
# fails.

APP_NAME = "Backup Toolkit"
APP_VERSION = "1.0.0"
WINDOW_TITLE = "Backup Toolkit — by QuickOpen (quickopen.ai)"
PROJECT_URL = "https://quickopen.ai"

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

# ---- colour palettes (mirror the QuickOpen palette) -------------------------
PALETTES = {
    "light": {
        "bg": "#f5f7fa", "surface": "#ffffff", "text": "#141820",
        "muted": "#5b6472", "primary": "#2f5fe0", "primary_hi": "#2450c8",
        "entry": "#ffffff", "border": "#d5dae2", "sel": "#2f5fe0",
        "sel_fg": "#ffffff", "trough": "#e2e7ef", "ok": "#1f7a3d",
        "err": "#c0392b",
    },
    "dark": {
        "bg": "#0f1115", "surface": "#1a1e24", "text": "#f1f3f7",
        "muted": "#9aa4b2", "primary": "#5b86f7", "primary_hi": "#7098ff",
        "entry": "#1a1e24", "border": "#2a2f38", "sel": "#5b86f7",
        "sel_fg": "#0f1115", "trough": "#2a2f38", "ok": "#5bd68a",
        "err": "#ff6b5e",
    },
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
# The app (built lazily; tkinter imported only inside build_app/main)
# ---------------------------------------------------------------------------
def build_app():
    """Construct and return the App class bound to a live tkinter import."""
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox

    from . import guiconfig, schedule
    from .config import JobStore, MODES
    from .engine import run_backup
    from .errors import BackupKitError
    from .restore import list_versions, restore
    from .verify import verify_backup

    FONT = "Segoe UI"

    class App(tk.Tk):
        def __init__(self):
            super().__init__()
            self.title(WINDOW_TITLE)
            self.geometry("1040x680")
            self.minsize(900, 560)

            self.store = JobStore()
            self.theme = guiconfig.get_theme()
            self._busy = False
            self._cancel_flag = False
            self._tracked = []        # (tk_widget, role) for manual re-theming
            self._img_refs = []
            self._panels = {}
            self._current = None
            self._last_output_dir = None

            self._set_icon()
            self._build_menu()
            self._build_layout()
            self._apply_theme()
            self.protocol("WM_DELETE_WINDOW", self._on_close)
            self.after(50, lambda: self._select_section("jobs"))

        # ---- assets / icon
        def _set_icon(self):
            try:
                ico = asset_path("backup-toolkit.ico")
                if ico:
                    self.iconbitmap(ico)
                    return
            except Exception:
                pass
            try:
                png = asset_path("backup-toolkit.png")
                if png:
                    img = tk.PhotoImage(file=png)
                    self._img_refs.append(img)
                    self.iconphoto(True, img)
            except Exception:
                pass  # icon is cosmetic; never block launch

        # ---- theming
        def track(self, widget, role):
            self._tracked.append((widget, role))

        def _pal(self):
            return PALETTES[self.theme]

        def _apply_theme(self):
            p = self._pal()
            style = ttk.Style(self)
            try:
                style.theme_use("clam")
            except Exception:
                pass
            self.configure(bg=p["bg"])
            style.configure(".", background=p["bg"], foreground=p["text"],
                            fieldbackground=p["entry"], bordercolor=p["border"],
                            font=(FONT, 10))
            style.configure("TFrame", background=p["bg"])
            style.configure("Sidebar.TFrame", background=p["surface"])
            style.configure("Card.TFrame", background=p["surface"])
            style.configure("TLabel", background=p["bg"], foreground=p["text"])
            style.configure("Muted.TLabel", background=p["bg"], foreground=p["muted"])
            style.configure("Header.TLabel", background=p["bg"], foreground=p["text"],
                            font=(FONT, 15, "bold"))
            style.configure("Sub.TLabel", background=p["bg"], foreground=p["muted"],
                            font=(FONT, 10))
            style.configure("Brand.TLabel", background=p["surface"],
                            foreground=p["text"], font=(FONT, 12, "bold"))
            style.configure("Ok.TLabel", background=p["bg"], foreground=p["ok"])
            style.configure("Err.TLabel", background=p["bg"], foreground=p["err"])
            style.configure("Status.TLabel", background=p["surface"],
                            foreground=p["muted"])
            style.configure("TButton", background=p["surface"], foreground=p["text"],
                            bordercolor=p["border"], focuscolor=p["surface"],
                            padding=(10, 5))
            style.map("TButton",
                      background=[("active", p["trough"]), ("disabled", p["bg"])],
                      foreground=[("disabled", p["muted"])])
            style.configure("Accent.TButton", background=p["primary"],
                            foreground="#ffffff", padding=(12, 6))
            style.map("Accent.TButton",
                      background=[("active", p["primary_hi"]),
                                  ("disabled", p["border"])],
                      foreground=[("disabled", p["muted"])])
            style.configure("Nav.TButton", background=p["surface"],
                            foreground=p["text"], anchor="w", padding=(14, 9),
                            font=(FONT, 11))
            style.map("Nav.TButton",
                      background=[("active", p["trough"])])
            style.configure("NavActive.TButton", background=p["primary"],
                            foreground="#ffffff", anchor="w", padding=(14, 9),
                            font=(FONT, 11, "bold"))
            style.map("NavActive.TButton",
                      background=[("active", p["primary_hi"])])
            style.configure("Toggle.TButton", background=p["surface"],
                            foreground=p["text"], padding=(8, 4))
            for name in ("TEntry", "TSpinbox"):
                style.configure(name, fieldbackground=p["entry"], foreground=p["text"],
                                insertcolor=p["text"], bordercolor=p["border"])
            style.configure("TCombobox", fieldbackground=p["entry"],
                            foreground=p["text"], background=p["surface"],
                            arrowcolor=p["text"])
            style.map("TCombobox", fieldbackground=[("readonly", p["entry"])],
                      foreground=[("readonly", p["text"])])
            style.configure("TCheckbutton", background=p["bg"], foreground=p["text"])
            style.map("TCheckbutton", background=[("active", p["bg"])])
            style.configure("TRadiobutton", background=p["bg"], foreground=p["text"])
            style.map("TRadiobutton", background=[("active", p["bg"])])
            style.configure("TLabelframe", background=p["bg"], foreground=p["text"],
                            bordercolor=p["border"])
            style.configure("TLabelframe.Label", background=p["bg"],
                            foreground=p["muted"])
            style.configure("Treeview", background=p["surface"],
                            fieldbackground=p["surface"], foreground=p["text"],
                            bordercolor=p["border"], rowheight=24)
            style.map("Treeview", background=[("selected", p["primary"])],
                      foreground=[("selected", p["sel_fg"])])
            style.configure("TProgressbar", background=p["primary"],
                            troughcolor=p["trough"], bordercolor=p["border"])
            style.configure("Horizontal.TProgressbar", background=p["primary"],
                            troughcolor=p["trough"])
            style.configure("TScrollbar", background=p["surface"],
                            troughcolor=p["bg"], bordercolor=p["border"],
                            arrowcolor=p["text"])
            style.configure("TSeparator", background=p["border"])

            for widget, role in list(self._tracked):
                try:
                    if role == "listbox":
                        widget.configure(bg=p["surface"], fg=p["text"],
                                         selectbackground=p["primary"],
                                         selectforeground=p["sel_fg"],
                                         highlightthickness=1,
                                         highlightbackground=p["border"],
                                         borderwidth=0)
                    elif role == "text":
                        widget.configure(bg=p["surface"], fg=p["text"],
                                         insertbackground=p["text"],
                                         selectbackground=p["primary"],
                                         selectforeground=p["sel_fg"],
                                         highlightthickness=1,
                                         highlightbackground=p["border"],
                                         borderwidth=0)
                except Exception:
                    pass
            # recolour nav buttons
            self._refresh_nav_styles()

        def toggle_theme(self):
            self.theme = "dark" if self.theme == "light" else "light"
            guiconfig.set_theme(self.theme)
            self._apply_theme()
            self._theme_btn.configure(
                text="☀ Light mode" if self.theme == "dark" else "🌙 Dark mode")

        # ---- menu
        def _build_menu(self):
            bar = tk.Menu(self)
            filem = tk.Menu(bar, tearoff=0)
            filem.add_command(label="Refresh jobs", command=self._reload_jobs)
            filem.add_separator()
            filem.add_command(label="Exit", command=self._on_close)
            bar.add_cascade(label="File", menu=filem)

            viewm = tk.Menu(bar, tearoff=0)
            viewm.add_command(label="Toggle dark mode", command=self.toggle_theme)
            bar.add_cascade(label="View", menu=viewm)

            helpm = tk.Menu(bar, tearoff=0)
            helpm.add_command(label="About", command=self._about)
            helpm.add_command(label="Open project page (quickopen.ai)",
                              command=lambda: open_with_default_app(PROJECT_URL))
            bar.add_cascade(label="Help", menu=helpm)
            self.configure(menu=bar)

        # ---- layout
        def _build_layout(self):
            top = ttk.Frame(self, style="Sidebar.TFrame", padding=(12, 8))
            top.pack(fill="x", side="top")
            ttk.Label(top, text="Backup Toolkit", style="Brand.TLabel").pack(side="left")
            ttk.Label(top, style="Status.TLabel",
                      text="  offline · open source · by QuickOpen").pack(side="left")
            self._theme_btn = ttk.Button(
                top, style="Toggle.TButton", command=self.toggle_theme,
                text="☀ Light mode" if self.theme == "dark" else "🌙 Dark mode")
            self._theme_btn.pack(side="right")

            body = ttk.Frame(self, style="TFrame")
            body.pack(fill="both", expand=True)

            side = ttk.Frame(body, style="Sidebar.TFrame", width=210)
            side.pack(side="left", fill="y")
            side.pack_propagate(False)
            self._nav_btns = {}
            for sid, label in SECTIONS:
                b = ttk.Button(side, text=label, style="Nav.TButton",
                               command=lambda s=sid: self._select_section(s))
                b.pack(fill="x", padx=6, pady=(6 if sid == "jobs" else 2, 0))
                self._nav_btns[sid] = b

            main = ttk.Frame(body, style="TFrame", padding=(16, 12))
            main.pack(side="left", fill="both", expand=True)
            head = ttk.Frame(main, style="TFrame")
            head.pack(fill="x")
            self.title_lbl = ttk.Label(head, text="", style="Header.TLabel")
            self.title_lbl.pack(anchor="w")
            self.desc_lbl = ttk.Label(head, text="", style="Sub.TLabel",
                                      wraplength=700, justify="left")
            self.desc_lbl.pack(anchor="w", pady=(2, 8))
            ttk.Separator(main).pack(fill="x")
            self.container = ttk.Frame(main, style="TFrame")
            self.container.pack(fill="both", expand=True, pady=(10, 8))

            # result / status bar
            bar = ttk.Frame(self, style="Sidebar.TFrame", padding=(12, 6))
            bar.pack(fill="x", side="bottom")
            self.status_lbl = ttk.Label(bar, text="Ready", style="Status.TLabel",
                                        width=14, anchor="w")
            self.status_lbl.pack(side="left")
            self.progress = ttk.Progressbar(bar, mode="determinate", length=160)
            self.openfolder_btn = ttk.Button(bar, text="Open folder",
                                             command=self._open_last_folder)
            self.result_lbl = ttk.Label(bar, text="", style="Status.TLabel",
                                        anchor="w", wraplength=560, justify="left")
            self.result_lbl.pack(side="left", fill="x", expand=True, padx=8)

        def _refresh_nav_styles(self):
            for sid, btn in getattr(self, "_nav_btns", {}).items():
                btn.configure(style="NavActive.TButton" if sid == self._current
                              else "Nav.TButton")

        # ---- section switching
        def _select_section(self, sid):
            if self._busy:
                return
            self._current = sid
            for child in self.container.winfo_children():
                child.destroy()
            self.title_lbl.configure(text=dict(SECTIONS)[sid])
            self.desc_lbl.configure(text=SECTION_DESC.get(sid, ""))
            self._clear_result()
            builder = getattr(self, "_section_" + sid, None)
            frame = ttk.Frame(self.container, style="TFrame")
            frame.pack(fill="both", expand=True)
            if builder:
                builder(frame)
            self._apply_theme()

        def _reload_jobs(self):
            if self._current == "jobs":
                self._select_section("jobs")
            self._set_status("Ready")

        def _job_names(self):
            try:
                return [j["name"] for j in self.store.list_jobs()]
            except Exception:
                return []

        # ================= Jobs section =================
        def _section_jobs(self, parent):
            left = ttk.Frame(parent, style="TFrame")
            left.pack(side="left", fill="y", padx=(0, 12))
            ttk.Label(left, text="Jobs", style="Sub.TLabel").pack(anchor="w")
            self.jobs_list = tk.Listbox(left, height=16, width=24,
                                        activestyle="none", exportselection=False)
            self.jobs_list.pack(fill="y", expand=True, pady=(4, 6))
            self.track(self.jobs_list, "listbox")
            self.jobs_list.bind("<<ListboxSelect>>", lambda e: self._load_selected_job())
            row = ttk.Frame(left, style="TFrame")
            row.pack(fill="x")
            ttk.Button(row, text="New", command=self._new_job).pack(side="left")
            ttk.Button(row, text="Delete", command=self._delete_job).pack(side="left", padx=4)

            self._reload_job_listbox()

            form = ttk.Frame(parent, style="TFrame")
            form.pack(side="left", fill="both", expand=True)
            self._job_form = {}

            def field(label, widget):
                r = ttk.Frame(form, style="TFrame")
                r.pack(fill="x", pady=3)
                ttk.Label(r, text=label, width=13, anchor="w").pack(side="left")
                widget(r)

            self._jf_name = tk.StringVar()
            field("Name", lambda r: ttk.Entry(r, textvariable=self._jf_name).pack(
                side="left", fill="x", expand=True))

            # sources listbox with add/remove
            sr = ttk.Frame(form, style="TFrame")
            sr.pack(fill="x", pady=3)
            ttk.Label(sr, text="Sources", width=13, anchor="nw").pack(side="left")
            sbox = ttk.Frame(sr, style="TFrame")
            sbox.pack(side="left", fill="x", expand=True)
            self._jf_sources = tk.Listbox(sbox, height=4, activestyle="none",
                                          exportselection=False)
            self._jf_sources.pack(fill="x")
            self.track(self._jf_sources, "listbox")
            sbtn = ttk.Frame(sbox, style="TFrame")
            sbtn.pack(fill="x", pady=(3, 0))
            ttk.Button(sbtn, text="Add folder…", command=self._add_source).pack(side="left")
            ttk.Button(sbtn, text="Remove", command=self._remove_source).pack(side="left", padx=4)

            self._jf_dest = tk.StringVar()
            dr = ttk.Frame(form, style="TFrame")
            dr.pack(fill="x", pady=3)
            ttk.Label(dr, text="Destination", width=13, anchor="w").pack(side="left")
            ttk.Entry(dr, textvariable=self._jf_dest).pack(side="left", fill="x", expand=True, padx=(0, 6))
            ttk.Button(dr, text="Browse…", command=self._browse_dest).pack(side="left")

            mr = ttk.Frame(form, style="TFrame")
            mr.pack(fill="x", pady=3)
            ttk.Label(mr, text="Mode", width=13, anchor="nw").pack(side="left")
            mcol = ttk.Frame(mr, style="TFrame")
            mcol.pack(side="left", fill="x", expand=True)
            self._jf_mode = tk.StringVar(value="incremental")
            for value, text in MODE_LABELS:
                ttk.Radiobutton(mcol, text=text, value=value,
                                variable=self._jf_mode).pack(anchor="w")

            self._jf_includes = tk.StringVar()
            field("Include globs", lambda r: ttk.Entry(r, textvariable=self._jf_includes).pack(
                side="left", fill="x", expand=True))
            self._jf_excludes = tk.StringVar()
            field("Exclude globs", lambda r: ttk.Entry(r, textvariable=self._jf_excludes).pack(
                side="left", fill="x", expand=True))
            ttk.Label(form, style="Muted.TLabel", text="   (comma-separated, e.g. *.tmp, cache/*)").pack(anchor="w")

            kr = ttk.Frame(form, style="TFrame")
            kr.pack(fill="x", pady=3)
            ttk.Label(kr, text="Keep (versioned)", width=13, anchor="w").pack(side="left")
            self._jf_keep = tk.StringVar(value="0")
            ttk.Spinbox(kr, from_=0, to=999, width=6, textvariable=self._jf_keep).pack(side="left")
            ttk.Label(kr, text="versions,   or days:").pack(side="left", padx=6)
            self._jf_keepdays = tk.StringVar(value="0")
            ttk.Spinbox(kr, from_=0, to=3650, width=6, textvariable=self._jf_keepdays).pack(side="left")

            self._jf_enabled = tk.BooleanVar(value=True)
            ttk.Checkbutton(form, text="Enabled (included in “run all” and schedules)",
                            variable=self._jf_enabled).pack(anchor="w", pady=(4, 0))

            br = ttk.Frame(form, style="TFrame")
            br.pack(fill="x", pady=(10, 0))
            self._jf_save = ttk.Button(br, text="Save job", style="Accent.TButton",
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
            top = ttk.Frame(parent, style="TFrame")
            top.pack(fill="x")
            ttk.Label(top, text="Job:", width=8, anchor="w").pack(side="left")
            self._run_job = tk.StringVar()
            names = self._job_names()
            self._run_combo = ttk.Combobox(top, textvariable=self._run_job,
                                           values=names, state="readonly", width=32)
            if names:
                self._run_combo.current(0)
            self._run_combo.pack(side="left")
            self._run_btn = ttk.Button(top, text="Run backup", style="Accent.TButton",
                                       command=self._do_run)
            self._run_btn.pack(side="left", padx=8)
            self._cancel_btn = ttk.Button(top, text="Cancel", command=self._cancel_run,
                                          state="disabled")
            self._cancel_btn.pack(side="left")

            logframe = ttk.Frame(parent, style="TFrame")
            logframe.pack(fill="both", expand=True, pady=(10, 0))
            self._run_log = tk.Text(logframe, height=18, wrap="none", borderwidth=0)
            sb = ttk.Scrollbar(logframe, orient="vertical", command=self._run_log.yview)
            self._run_log.configure(yscrollcommand=sb.set, state="disabled")
            sb.pack(side="right", fill="y")
            self._run_log.pack(side="left", fill="both", expand=True)
            self.track(self._run_log, "text")

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
            self.progress.pack(side="right", padx=6)
            self.progress.configure(value=0, maximum=100)

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
                self.progress.configure(maximum=total, value=idx)
            action = ev.get("action")
            if action in ("copy", "link", "delete"):
                self._log(f"  {action:<6} {ev.get('path','')}")

        def _run_done(self, rep):
            self.progress.pack_forget()
            self._cancel_btn.configure(state="disabled")
            self._log("")
            summary = (f"copied {rep['copied']}, linked {rep.get('linked',0)}, "
                       f"skipped {rep['skipped']}, deleted {rep['deleted']}, "
                       f"{human_size(rep['bytes'])}")
            if rep.get("cancelled"):
                self._log("Cancelled. " + summary)
                self._set_status("cancelled", kind="err")
                self.result_lbl.configure(text="Run cancelled.",
                                          foreground=self._pal()["err"])
                return
            self._log("Done: " + summary)
            if rep.get("version_dir"):
                self._log("Version: " + rep["version_dir"])
            for err in rep.get("errors", []):
                self._log("  ! " + err)
            out = rep.get("version_dir") or rep.get("dest")
            if rep.get("errors"):
                self.report_success(summary + f" — {len(rep['errors'])} error(s)", [out])
            else:
                self.report_success(summary, [out])

        # ================= Versions / Restore section =================
        def _section_versions(self, parent):
            top = ttk.Frame(parent, style="TFrame")
            top.pack(fill="x")
            ttk.Label(top, text="Job:", width=8, anchor="w").pack(side="left")
            self._ver_job = tk.StringVar()
            names = [j["name"] for j in self.store.list_jobs()
                     if j["mode"] == "versioned"]
            self._ver_combo = ttk.Combobox(top, textvariable=self._ver_job,
                                           values=names, state="readonly", width=32)
            if names:
                self._ver_combo.current(0)
            self._ver_combo.pack(side="left")
            ttk.Button(top, text="List versions",
                       command=self._list_versions).pack(side="left", padx=8)
            if not names:
                ttk.Label(parent, style="Muted.TLabel",
                          text="No versioned jobs yet — set a job's mode to "
                               "“versioned” to keep timestamped snapshots.").pack(
                    anchor="w", pady=8)

            self._ver_list = tk.Listbox(parent, height=12, activestyle="none",
                                        exportselection=False)
            self._ver_list.pack(fill="both", expand=True, pady=(10, 6))
            self.track(self._ver_list, "listbox")

            row = ttk.Frame(parent, style="TFrame")
            row.pack(fill="x")
            ttk.Button(row, text="Restore selected…", style="Accent.TButton",
                       command=self._restore_selected).pack(side="left")
            self._ver_overwrite = tk.BooleanVar(value=False)
            ttk.Checkbutton(row, text="Overwrite existing files",
                            variable=self._ver_overwrite).pack(side="left", padx=10)
            self._versions_cache = []

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
            top = ttk.Frame(parent, style="TFrame")
            top.pack(fill="x")
            ttk.Label(top, text="Job:", width=8, anchor="w").pack(side="left")
            self._vf_job = tk.StringVar()
            names = self._job_names()
            self._vf_combo = ttk.Combobox(top, textvariable=self._vf_job,
                                          values=names, state="readonly", width=32)
            if names:
                self._vf_combo.current(0)
            self._vf_combo.pack(side="left")
            ttk.Button(top, text="Verify job", style="Accent.TButton",
                       command=self._verify_job).pack(side="left", padx=8)
            ttk.Button(top, text="Verify a folder…",
                       command=self._verify_folder).pack(side="left")

            self._vf_out = tk.Text(parent, height=18, wrap="none", borderwidth=0,
                                   state="disabled")
            self._vf_out.pack(fill="both", expand=True, pady=(10, 0))
            self.track(self._vf_out, "text")

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
                self.result_lbl.configure(
                    text=f"✕ {len(res['missing'])} missing / "
                         f"{len(res['mismatched'])} mismatched",
                    foreground=self._pal()["err"])
                self._set_status("problems", kind="err")

        # ================= Schedule section =================
        def _section_schedule(self, parent):
            note = ("Scheduling registers a Windows Scheduled Task (via schtasks) "
                    "so a job runs automatically. This is Windows-only; on other "
                    "platforms the command is shown but not registered. Backups "
                    "themselves run everywhere.")
            ttk.Label(parent, style="Muted.TLabel", wraplength=680, justify="left",
                      text=note).pack(anchor="w", pady=(0, 10))
            top = ttk.Frame(parent, style="TFrame")
            top.pack(fill="x")
            ttk.Label(top, text="Job:", width=8, anchor="w").pack(side="left")
            self._sc_job = tk.StringVar()
            names = self._job_names()
            combo = ttk.Combobox(top, textvariable=self._sc_job, values=names,
                                 state="readonly", width=32)
            if names:
                combo.current(0)
            combo.pack(side="left")

            ir = ttk.Frame(parent, style="TFrame")
            ir.pack(fill="x", pady=8)
            ttk.Label(ir, text="Interval:", width=8, anchor="w").pack(side="left")
            self._sc_interval = tk.StringVar(value="60m")
            ttk.Combobox(ir, textvariable=self._sc_interval,
                         values=["30m", "60m", "2h", "hourly", "daily"],
                         width=12).pack(side="left")
            ttk.Button(ir, text="Register task", style="Accent.TButton",
                       command=self._do_schedule).pack(side="left", padx=8)
            ttk.Button(ir, text="Remove task",
                       command=self._do_unschedule).pack(side="left")

            self._sc_out = tk.Text(parent, height=8, wrap="word", borderwidth=0,
                                   state="disabled")
            self._sc_out.pack(fill="both", expand=True, pady=(10, 0))
            self.track(self._sc_out, "text")

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
                    self._set_status("error", kind="err")
                    self._show_error(err)
                    return
                self._set_status("done", kind="ok")
                try:
                    on_ok(res)
                except Exception as ex:
                    self._show_error(f"Post-processing error: {ex}")

            threading.Thread(target=run, daemon=True).start()

        # ---- result bar helpers
        def _set_status(self, text, kind="idle"):
            p = self._pal()
            color = {"working": p["primary"], "ok": p["ok"], "err": p["err"]}.get(
                kind, p["muted"])
            self.status_lbl.configure(text=text, foreground=color)

        def _clear_result(self, keep_status=False):
            self.result_lbl.configure(text="")
            self.openfolder_btn.pack_forget()
            if not keep_status:
                self._set_status("Ready")

        def _show_error(self, message):
            self.result_lbl.configure(text="✕ " + message,
                                      foreground=self._pal()["err"])
            self.openfolder_btn.pack_forget()

        def report_success(self, message, outputs=None):
            outputs = outputs or []
            first = next((o for o in outputs if o), None)
            if first:
                self._last_output_dir = (
                    first if os.path.isdir(first)
                    else os.path.dirname(os.path.abspath(first)))
                self.openfolder_btn.pack(side="right")
            self.result_lbl.configure(text="✓ " + message,
                                      foreground=self._pal()["ok"])
            self._set_status("done", kind="ok")

        def _open_last_folder(self):
            if self._last_output_dir:
                open_in_file_manager(self._last_output_dir)

        # ---- About
        def _about(self):
            win = tk.Toplevel(self)
            win.title("About Backup Toolkit")
            win.configure(bg=self._pal()["bg"])
            win.resizable(False, False)
            frm = ttk.Frame(win, style="TFrame", padding=18)
            frm.pack(fill="both", expand=True)
            ttk.Label(frm, text="Backup Toolkit", style="Header.TLabel").pack(anchor="w")
            ttk.Label(frm, text=f"Version {APP_VERSION}",
                      style="Sub.TLabel").pack(anchor="w", pady=(0, 8))
            ttk.Label(frm, style="TLabel", justify="left", wraplength=380,
                      text="A fast, fully-offline backup & sync toolkit — mirror, "
                           "incremental and versioned backups with SHA-256 "
                           "verification and restore.\n\n"
                           "100% AI-built, open source, published on QuickOpen.\n"
                           "Nothing is ever uploaded anywhere.").pack(anchor="w")
            ttk.Label(frm, style="Sub.TLabel", justify="left", wraplength=380,
                      text="Licensed under Apache-2.0. Pure Python standard "
                           "library — no third-party dependencies.").pack(
                anchor="w", pady=(8, 4))
            link = ttk.Label(frm, text="Project page: quickopen.ai",
                             style="Ok.TLabel", cursor="hand2")
            link.pack(anchor="w", pady=(4, 10))
            link.bind("<Button-1>", lambda e: open_with_default_app(PROJECT_URL))
            ttk.Button(frm, text="Close", command=win.destroy).pack(anchor="e")

        # ---- shutdown
        def _on_close(self):
            self.destroy()

    return App


def main():
    """Entry point: build the root window and run. Degrades on headless hosts.

    Importing this module does nothing; only this function creates a Tk root.
    With no display it prints a friendly note and returns 0 instead of raising.
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
