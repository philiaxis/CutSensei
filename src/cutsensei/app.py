"""Desktop application entry point (``cutsensei``)."""

from __future__ import annotations

import os
import sys
from typing import List, Optional


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    # Qt Multimedia: the FFmpeg backend is the most capable on every platform
    os.environ.setdefault("QT_MEDIA_BACKEND", "ffmpeg")
    os.environ.setdefault("QT_LOGGING_RULES", "qt.multimedia.symbolsresolver=false")

    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QMessageBox

    from . import APP_NAME, __version__
    from .core import ffmpeg
    from .ui import i18n

    from PySide6.QtCore import QEvent

    class CutSenseiApp(QApplication):
        """Handles macOS "open document" events (Finder double-click, Dock)."""

        def __init__(self, args: List[str]) -> None:
            super().__init__(args)
            self.main_window = None
            self.pending: List[str] = []

        def event(self, e) -> bool:  # noqa: N802 - Qt naming
            if e.type() == QEvent.FileOpen and e.file():
                if self.main_window is None:
                    self.pending.append(e.file())
                else:
                    _open_path(self.main_window, e.file())
                return True
            return super().event(e)

    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = CutSenseiApp(argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName(APP_NAME)
    app.setApplicationVersion(__version__)
    app.setDesktopFileName("cutsensei")

    from .ui.settings_store import app_settings

    qs = app_settings()
    i18n.set_language(str(qs.value("language", "auto")))
    ff = str(qs.value("ffmpeg_path", "") or "")
    if ff and os.path.isfile(ff):
        ffmpeg.set_ffmpeg_path(ff)

    from .ui import icons, theme
    from .ui.dialogs import prewarm_encoder_probe
    from .ui.main_window import MainWindow

    theme.apply_theme(app)
    app.setWindowIcon(icons.app_icon())
    try:
        ffmpeg.find_ffmpeg()
    except Exception as exc:
        QMessageBox.critical(None, APP_NAME, i18n.tr(
            "FFmpeg was not found. Please install FFmpeg (see README) or set its path in the "
            "preferences.") + f"\n\n{exc}")
    else:
        prewarm_encoder_probe()

    win = MainWindow()
    win.show()
    app.main_window = win
    smoke = os.environ.get("CUTSENSEI_SMOKE_TEST")
    if smoke:
        # used by CI / packaging checks: start, render once, save a screenshot, quit
        from PySide6.QtCore import QTimer

        def finish() -> None:
            win.grab().save(smoke)
            app.quit()
        QTimer.singleShot(1500, finish)
    files = [a for a in argv[1:] if not a.startswith("-")] + app.pending
    if files:
        _open_path(win, files[0])
    return app.exec()


def _open_path(win, path: str) -> None:
    if path.lower().endswith(".cutsensei"):
        win.open_project(path)
    else:
        win.import_video(path)


if __name__ == "__main__":
    sys.exit(main())
