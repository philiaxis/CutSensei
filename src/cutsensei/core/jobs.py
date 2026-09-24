"""Helpers for long running jobs: cancellation and progress reporting."""

from __future__ import annotations

import threading
from typing import Callable, Optional

from .errors import Cancelled

ProgressCallback = Callable[[float, str], None]


class CancelToken:
    """Thread-safe cancellation flag passed down to long running jobs."""

    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def check(self) -> None:
        if self._event.is_set():
            raise Cancelled("cancelled")


class Progress:
    """Maps the progress of a sub-step into a range of the overall progress."""

    def __init__(self, callback: Optional[ProgressCallback], start: float = 0.0,
                 end: float = 1.0) -> None:
        self._callback = callback
        self.start = start
        self.end = end

    def __call__(self, fraction: float, message: str = "") -> None:
        if self._callback is None:
            return
        fraction = min(1.0, max(0.0, fraction))
        self._callback(self.start + (self.end - self.start) * fraction, message)

    def sub(self, start: float, end: float) -> "Progress":
        span = self.end - self.start
        return Progress(self._callback, self.start + span * start, self.start + span * end)


def no_progress(_fraction: float, _message: str = "") -> None:
    pass
