"""Formatting helpers for times and labels."""

from __future__ import annotations

import math

from ..core.timeline import Action, Kind
from .i18n import tr


def fmt_time(sec: float, decimals: int = 1) -> str:
    if sec is None or math.isnan(sec) or math.isinf(sec):
        return "--:--"
    sign = "-" if sec < 0 else ""
    sec = abs(sec)
    h = int(sec // 3600)
    m = int(sec % 3600 // 60)
    s = sec % 60
    width = 3 + decimals if decimals else 2
    s_txt = f"{s:0{width}.{decimals}f}" if decimals else f"{int(s):02d}"
    if h:
        return f"{sign}{h}:{m:02d}:{s_txt}"
    return f"{sign}{m:02d}:{s_txt}"


def fmt_timecode(sec: float, fps: float) -> str:
    fps = fps if fps > 0 else 30.0
    total = int(round(sec * fps))
    f = total % int(round(fps))
    s = total // int(round(fps))
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}:{f:02d}"


def fmt_duration(sec: float) -> str:
    sec = max(0.0, sec)
    if sec < 60:
        return tr("{s:.1f} s").format(s=sec)
    if sec < 3600:
        return tr("{m} min {s:02d} s").format(m=int(sec // 60), s=int(sec % 60))
    return tr("{h} h {m:02d} min").format(h=int(sec // 3600), m=int(sec % 3600 // 60))


def fmt_speed(speed: float) -> str:
    if math.isinf(speed):
        return "✂"
    if abs(speed - round(speed)) < 1e-6:
        return f"{int(round(speed))}×"
    return f"{speed:g}×"


def kind_label(kind: str) -> str:
    return {Kind.SPEECH: tr("Explanation"), Kind.WRITING: tr("Board writing"),
            Kind.IDLE: tr("Waiting"), Kind.UNCERTAIN: tr("Uncertain")}.get(kind, kind)


def action_label(action: str) -> str:
    return {Action.KEEP: tr("Normal speed"), Action.SPEED: tr("Speed up"),
            Action.CUT: tr("Delete")}.get(action, action)


REASONS = {
    "speech": "Speech detected",
    "writing": "Silent board writing",
    "short_writing": "Short writing (kept at 1x)",
    "idle_cut": "Waiting without speech or writing",
    "short_pause": "Short pause (kept)",
    "margin": "Margin next to a cut",
    "hold_after_writing": "Showing the finished board",
    "uncertain_speech": "Possibly quiet speech",
    "uncertain_writing": "Possibly writing",
    "uncertain_motion": "Movement without speech",
    "unanalyzed": "Not analysed yet",
}


def reason_label(reason: str) -> str:
    return tr(REASONS.get(reason, reason or "-"))
