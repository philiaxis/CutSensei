"""Location of the persistent application settings."""

from __future__ import annotations

import os

from PySide6.QtCore import QSettings

from .. import APP_NAME


def app_settings() -> QSettings:
    """``CUTSENSEI_SETTINGS=/path/file.ini`` stores settings in a file
    (portable installs, tests); otherwise the platform default is used."""
    path = os.environ.get("CUTSENSEI_SETTINGS")
    if path:
        return QSettings(path, QSettings.IniFormat)
    return QSettings(APP_NAME, APP_NAME)
