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

    from PySide6.QtCore import QSettings, Qt
    from PySide6.QtWidgets import QApplication, QMessageBox

    from . import APP_NAME, __version__
    from .core import ffmpeg
    from .ui import i18n

    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName(APP_NAME)
    app.setApplicationVersion(__version__)
    app.setDesktopFileName("cutsensei")

    qs = QSettings(APP_NAME, APP_NAME)
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
    for arg in argv[1:]:
        if arg.startswith("-"):
            continue
        if arg.endswith(".cutsensei"):
            win.open_project(arg)
        else:
            win.import_video(arg)
        break
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
