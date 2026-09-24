"""Minimal translation layer.

User visible strings are written in English in the source and looked up
with :func:`tr`.  Translations live in ``translations_<lang>.py`` modules as
plain dictionaries, which keeps contributing a language simple.
"""

from __future__ import annotations

import importlib
import locale
import os
from typing import Dict

SUPPORTED = {"en": "English", "ja": "日本語"}

_current = "en"
_table: Dict[str, str] = {}


def detect_language() -> str:
    env = os.environ.get("CUTSENSEI_LANG")
    if env:
        return env.split("_")[0].lower()
    try:
        from PySide6.QtCore import QLocale

        name = QLocale.system().name()
    except Exception:  # pragma: no cover
        name = (locale.getlocale()[0] or "en")
    return (name or "en").split("_")[0].lower()


def set_language(lang: str) -> None:
    global _current, _table
    lang = (lang or "en").lower()
    if lang == "auto":
        lang = detect_language()
    if lang not in SUPPORTED:
        lang = "en"
    _current = lang
    _table = {}
    if lang != "en":
        try:
            mod = importlib.import_module(f"cutsensei.ui.translations_{lang}")
            _table = dict(getattr(mod, "STRINGS", {}))
        except ImportError:
            _table = {}


def language() -> str:
    return _current


def tr(text: str) -> str:
    return _table.get(text, text)


def trf(text: str, **kwargs) -> str:
    return tr(text).format(**kwargs)
