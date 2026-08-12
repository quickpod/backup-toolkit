r"""Tiny JSON-backed config for the Backup Toolkit GUI.

Stores just the chosen theme ("light"/"dark") and a short list of recently used
folders.  On Windows the file lives at ``%LOCALAPPDATA%\BackupToolkit\config.json``;
elsewhere it falls back to ``~/.backuptoolkit/config.json`` (the same directory
as the jobs store).  Every function is defensive -- a corrupt or unreadable
config must never stop the app from starting.
"""

from __future__ import annotations

import json
import os

from .config import default_store_dir

CONFIG_NAME = "config.json"
MAX_RECENT = 10
VALID_THEMES = ("light", "dark")


def config_path():
    return os.path.join(default_store_dir(), CONFIG_NAME)


def _defaults():
    return {"theme": "light", "recent": []}


def load():
    cfg = _defaults()
    try:
        with open(config_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            theme = data.get("theme")
            if theme in VALID_THEMES:
                cfg["theme"] = theme
            recent = data.get("recent")
            if isinstance(recent, list):
                cfg["recent"] = [p for p in recent if isinstance(p, str)][:MAX_RECENT]
    except Exception:
        pass  # missing/corrupt -> defaults; never fatal
    return cfg


def save(cfg):
    try:
        os.makedirs(default_store_dir(), exist_ok=True)
        clean = {
            "theme": cfg.get("theme") if cfg.get("theme") in VALID_THEMES else "light",
            "recent": [p for p in cfg.get("recent", []) if isinstance(p, str)][:MAX_RECENT],
        }
        tmp = config_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(clean, fh, indent=2)
        os.replace(tmp, config_path())
    except Exception:
        pass


def get_theme():
    return load().get("theme", "light")


def set_theme(theme):
    if theme not in VALID_THEMES:
        return
    cfg = load()
    cfg["theme"] = theme
    save(cfg)


def get_recent():
    return load().get("recent", [])


def add_recent(path):
    if not path:
        return
    try:
        ap = os.path.abspath(path)
    except Exception:
        ap = path
    cfg = load()
    recent = [p for p in cfg.get("recent", []) if _abs(p) != ap]
    recent.insert(0, ap)
    cfg["recent"] = recent[:MAX_RECENT]
    save(cfg)


def clear_recent():
    cfg = load()
    cfg["recent"] = []
    save(cfg)


def _abs(p):
    try:
        return os.path.abspath(p)
    except Exception:
        return p
