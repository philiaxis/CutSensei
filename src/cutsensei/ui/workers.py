"""Background jobs (analysis, export) with progress dialog and cancel."""

from __future__ import annotations

import time
import traceback
from typing import Any, Callable, Optional

from PySide6.QtCore import QObject, QThread, Qt, Signal
from PySide6.QtWidgets import QProgressDialog, QWidget

from ..core.errors import Cancelled, CutSenseiError, FFmpegError
from ..core.jobs import CancelToken
from .i18n import tr


class JobThread(QThread):
    progress = Signal(float, str)
    succeeded = Signal(object)
    failed = Signal(str, str)      # message, details
    cancelled = Signal()

    def __init__(self, func: Callable[[Callable[[float, str], None], CancelToken], Any],
                 parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self.func = func
        self.token = CancelToken()

    def cancel(self) -> None:
        self.token.cancel()

    def run(self) -> None:
        try:
            result = self.func(lambda f, m="": self.progress.emit(float(f), m), self.token)
        except Cancelled:
            self.cancelled.emit()
        except FFmpegError as exc:
            self.failed.emit(str(exc), exc.stderr[-4000:])
        except CutSenseiError as exc:
            self.failed.emit(str(exc), "")
        except Exception as exc:  # unexpected - show the traceback
            self.failed.emit(str(exc) or exc.__class__.__name__, traceback.format_exc())
        else:
            if self.token.cancelled:
                self.cancelled.emit()
            else:
                self.succeeded.emit(result)


class ProgressRunner(QObject):
    """Runs a :class:`JobThread` while showing a cancellable progress dialog."""

    finished = Signal()

    def __init__(self, parent: QWidget, title: str, label: str,
                 func: Callable[[Callable[[float, str], None], CancelToken], Any],
                 on_success: Callable[[Any], None],
                 on_failure: Callable[[str, str], None],
                 on_cancel: Optional[Callable[[], None]] = None,
                 messages: Optional[dict] = None) -> None:
        super().__init__(parent)
        self.label = label
        self.messages = messages or {}
        self.start_time = time.time()
        self.dialog = QProgressDialog(label, tr("Cancel"), 0, 1000, parent)
        self.dialog.setWindowTitle(title)
        self.dialog.setWindowModality(Qt.WindowModal)
        self.dialog.setMinimumDuration(0)
        self.dialog.setMinimumWidth(420)
        self.dialog.setAutoClose(False)
        self.dialog.setAutoReset(False)
        self.dialog.setValue(0)
        self.thread = JobThread(func, self)
        self.thread.progress.connect(self._on_progress)
        self.thread.succeeded.connect(lambda r: self._done(lambda: on_success(r)))
        self.thread.failed.connect(lambda m, d: self._done(lambda: on_failure(m, d)))
        self.thread.cancelled.connect(lambda: self._done(on_cancel or (lambda: None)))
        self.dialog.canceled.connect(self._cancel)

    def start(self) -> None:
        self.start_time = time.time()
        self.thread.start()
        self.dialog.show()

    def _cancel(self) -> None:
        self.dialog.setLabelText(tr("Cancelling..."))
        self.dialog.setCancelButton(None)
        self.dialog.show()
        self.thread.cancel()

    def _on_progress(self, fraction: float, message: str) -> None:
        if self.thread.token.cancelled:
            return
        self.dialog.setValue(int(fraction * 1000))
        text = self.messages.get(message, self.label)
        elapsed = time.time() - self.start_time
        if fraction > 0.03 and elapsed > 3:
            remaining = elapsed * (1 - fraction) / fraction
            text += "\n" + tr("Remaining: about {t}").format(t=_fmt_eta(remaining))
        self.dialog.setLabelText(text)

    def _done(self, callback: Callable[[], None]) -> None:
        self.thread.wait()
        self.dialog.reset()
        self.dialog.hide()
        self.dialog.deleteLater()
        try:
            callback()
        finally:
            self.finished.emit()
            self.deleteLater()


def _fmt_eta(sec: float) -> str:
    sec = int(max(0, sec))
    if sec < 60:
        return tr("{n} s").format(n=sec)
    if sec < 3600:
        return tr("{m} min {s} s").format(m=sec // 60, s=sec % 60)
    return tr("{h} h {m} min").format(h=sec // 3600, m=sec % 3600 // 60)
