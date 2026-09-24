"""The timeline: thumbnails, segments, waveform and playhead.

By default the timeline shows the *edited* result: cut parts are collapsed
to a thin marker and sped-up parts are shortened by their speed factor.  The
"original" mode shows the source at a linear scale including cut parts.
"""

from __future__ import annotations

import math
import os
from collections import OrderedDict
from typing import List, Optional, Tuple

import numpy as np
from PySide6.QtCore import QLineF, QPoint, QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import (QBrush, QColor, QFont, QImage, QPainter, QPen, QPixmap, QPolygonF)
from PySide6.QtWidgets import (QHBoxLayout, QLabel, QScrollBar, QSizePolicy, QSlider,
                               QToolButton, QVBoxLayout, QWidget)

from ..analysis.thumbnails import thumbnail_path
from ..core.timeline import Action, EditMap, Timeline
from . import icons, theme
from .fmt import fmt_speed, fmt_time, kind_label
from .i18n import tr
from .preview import EDITED, SOURCE, tool_button

RULER_H = 22
MIN_PPS = 0.005
MAX_PPS = 600.0
EDGE_HIT = 5


_STEPS = [(0.1, 0.05), (0.2, 0.1), (0.5, 0.1), (1, 0.2), (2, 0.5), (5, 1), (10, 2), (15, 5),
          (30, 5), (60, 10), (120, 30), (300, 60), (600, 120), (900, 300), (1800, 300),
          (3600, 600), (7200, 1800)]


def _nice_step(pps: float, min_px: float = 80.0) -> Tuple[float, float]:
    """Major and minor tick spacing (seconds) for the ruler."""
    for major, minor in _STEPS:
        if major * pps >= min_px:
            return float(major), float(minor)
    return 7200.0, 1800.0


class TimelineView(QWidget):
    seekRequested = Signal(float)                  # source seconds
    playFromRequested = Signal(float)
    contextMenuRequested = Signal(QPoint, int)     # global position, segment index
    viewChanged = Signal()                         # zoom / scroll changed

    def __init__(self, controller, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.ctrl = controller
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.ClickFocus)
        self.setMinimumHeight(150)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.mode = EDITED
        self.pps = 20.0
        self.offset = 0.0
        self.playhead = 0.0
        self.follow = True
        self._map: Optional[EditMap] = None
        self._disp_starts = np.zeros(0)
        self._disp_ends = np.zeros(0)
        self._thumbs: Optional[dict] = None
        self._thumb_cache: "OrderedDict[int, QPixmap]" = OrderedDict()
        self._thumb_aspect = 16 / 9
        self._drag: Optional[dict] = None
        self._hover_seg = -1
        self._fit_pending = True

    # ------------------------------------------------------------ data
    def sizeHint(self) -> QSize:
        return QSize(1000, 190)

    def refresh(self) -> None:
        tl = self.ctrl.timeline
        self._map = self.ctrl.edit_map()
        if tl is None or self._map is None:
            self._disp_starts = np.zeros(0)
            self._disp_ends = np.zeros(0)
        elif self.mode == EDITED:
            starts, ends = [], []
            by_index = {p.index: p for p in self._map.pieces}
            for i, seg in enumerate(tl):
                p = by_index.get(i)
                if p is None:
                    x = self._map.src_to_out(seg.start)
                    starts.append(x)
                    ends.append(x)
                else:
                    starts.append(p.out_start)
                    ends.append(p.out_end)
            self._disp_starts = np.asarray(starts)
            self._disp_ends = np.asarray(ends)
        else:
            self._disp_starts = np.asarray([s.start for s in tl])
            self._disp_ends = np.asarray([s.end for s in tl])
        self._clamp_offset()
        self.update()
        self.viewChanged.emit()

    def set_thumbnails(self, info: Optional[dict]) -> None:
        self._thumbs = info
        self._thumb_cache.clear()
        self.update()

    def set_media_aspect(self, aspect: float) -> None:
        self._thumb_aspect = aspect if aspect > 0.2 else 16 / 9

    def set_mode(self, mode: str) -> None:
        if mode == self.mode:
            return
        center_src = self.to_src(self.offset + self.width() / 2 / self.pps)
        self.mode = mode
        self.refresh()
        self.offset = self.to_disp(center_src) - self.width() / 2 / self.pps
        self._clamp_offset()
        self.update()
        self.viewChanged.emit()

    def set_playhead(self, src_t: float) -> None:
        if abs(src_t - self.playhead) < 1e-6:
            return
        old_x = self.x_of(self.to_disp(self.playhead))
        self.playhead = src_t
        x = self.x_of(self.to_disp(src_t))
        if self.follow and self._drag is None and (x < 0 or x > self.width() - 20):
            self.offset = self.to_disp(src_t) - 0.1 * self.width() / self.pps
            self._clamp_offset()
            self.viewChanged.emit()
            self.update()
            return
        self.update(QRectF(old_x - 8, 0, 16, self.height()).toRect())
        self.update(QRectF(x - 8, 0, 16, self.height()).toRect())

    # ------------------------------------------------------------ mapping
    def display_duration(self) -> float:
        if self.mode == EDITED:
            return self._map.out_duration if self._map else 0.0
        tl = self.ctrl.timeline
        return tl.duration if tl else 0.0

    def to_disp(self, src_t: float) -> float:
        if self.mode == EDITED and self._map is not None:
            return self._map.src_to_out(src_t)
        return src_t

    def to_src(self, disp_t: float) -> float:
        if self.mode == EDITED and self._map is not None:
            return self._map.out_to_src(disp_t)
        return disp_t

    def x_of(self, disp_t: float) -> float:
        return (disp_t - self.offset) * self.pps

    def disp_at(self, x: float) -> float:
        return self.offset + x / self.pps

    # ------------------------------------------------------------ zoom/scroll
    def fit_pps(self) -> float:
        dur = self.display_duration()
        return max(MIN_PPS, (self.width() - 20) / dur) if dur > 0 else 20.0

    def zoom_to_fit(self) -> None:
        self.pps = min(MAX_PPS, self.fit_pps())
        self.offset = 0.0
        self.update()
        self.viewChanged.emit()

    def zoom(self, factor: float, anchor_x: Optional[float] = None) -> None:
        if anchor_x is None:
            ph = self.x_of(self.to_disp(self.playhead))
            anchor_x = ph if 0 <= ph <= self.width() else self.width() / 2
        anchor_t = self.disp_at(anchor_x)
        self.pps = min(MAX_PPS, max(min(self.fit_pps(), 20.0), self.pps * factor))
        self.offset = anchor_t - anchor_x / self.pps
        self._clamp_offset()
        self.update()
        self.viewChanged.emit()

    def set_zoom_level(self, level: float) -> None:
        """0..1 logarithmic between "fit" and the maximum zoom."""
        lo, hi = math.log(min(self.fit_pps(), MAX_PPS)), math.log(MAX_PPS)
        pps = math.exp(lo + (hi - lo) * max(0.0, min(1.0, level)))
        self.zoom(pps / self.pps)

    def zoom_level(self) -> float:
        lo, hi = math.log(min(self.fit_pps(), MAX_PPS)), math.log(MAX_PPS)
        if hi - lo < 1e-9:
            return 0.0
        return (math.log(self.pps) - lo) / (hi - lo)

    def content_width(self) -> int:
        return int(self.display_duration() * self.pps) + 40

    def set_scroll(self, px: int) -> None:
        self.offset = px / self.pps
        self._clamp_offset()
        self.update()

    def scroll_px(self) -> int:
        return int(self.offset * self.pps)

    def _clamp_offset(self) -> None:
        max_off = max(0.0, self.display_duration() - (self.width() - 40) / self.pps)
        self.offset = min(max(0.0, self.offset), max_off)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self._fit_pending and self.display_duration() > 0:
            self._fit_pending = False
            self.zoom_to_fit()
        self._clamp_offset()
        self.viewChanged.emit()

    def request_fit(self) -> None:
        self._fit_pending = True
        if self.width() > 50 and self.display_duration() > 0:
            self._fit_pending = False
            self.zoom_to_fit()

    # ------------------------------------------------------------ layout
    def _tracks(self) -> Tuple[QRectF, QRectF, QRectF, QRectF]:
        w, h = float(self.width()), float(self.height())
        avail = h - RULER_H - 6
        seg_h = max(26.0, min(40.0, avail * 0.26))
        thumb_h = max(30.0, min(90.0, avail * 0.36))
        wave_h = max(24.0, avail - seg_h - thumb_h)
        y = RULER_H + 2
        thumbs = QRectF(0, y, w, thumb_h)
        y += thumb_h + 2
        segs = QRectF(0, y, w, seg_h)
        y += seg_h + 2
        wave = QRectF(0, y, w, wave_h)
        ruler = QRectF(0, 0, w, RULER_H)
        return ruler, thumbs, segs, wave

    def _visible_range(self) -> Tuple[int, int]:
        if len(self._disp_starts) == 0:
            return 0, -1
        t0 = self.offset
        t1 = self.offset + self.width() / self.pps
        i0 = int(np.searchsorted(self._disp_ends, t0, side="left"))
        i1 = int(np.searchsorted(self._disp_starts, t1, side="right"))
        return max(0, i0 - 1), min(len(self._disp_starts) - 1, i1)

    # ------------------------------------------------------------ painting
    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(theme.BG0))
        tl = self.ctrl.timeline
        ruler, thumbs, segs, wave = self._tracks()
        if tl is None or self.display_duration() <= 0:
            p.setPen(QColor(theme.TEXT_FAINT))
            p.drawText(self.rect(), Qt.AlignCenter,
                       tr("Import a lecture video to start (drag & drop works too)."))
            p.end()
            return
        self._paint_ruler(p, ruler)
        self._paint_thumbs(p, thumbs)
        self._paint_segments(p, segs, tl)
        self._paint_wave(p, wave, tl)
        end_x = self.x_of(self.display_duration())
        if end_x < self.width():
            p.fillRect(QRectF(end_x, RULER_H, self.width() - end_x, self.height()),
                       theme.color(theme.BG1, 255))
        self._paint_playhead(p)
        p.end()

    def _paint_ruler(self, p: QPainter, r: QRectF) -> None:
        p.fillRect(r, QColor(theme.BG1))
        p.setPen(QColor(theme.BORDER))
        p.drawLine(QLineF(0, r.bottom(), r.right(), r.bottom()))
        major, minor = _nice_step(self.pps)
        ratio = max(1, int(round(major / minor)))
        k = int(math.floor(self.offset / minor))
        t1 = self.offset + self.width() / self.pps
        f = QFont(self.font())
        f.setPointSizeF(max(7.5, f.pointSizeF() - 1.5))
        p.setFont(f)
        for _ in range(5000):
            t = k * minor
            if t > t1:
                break
            x = self.x_of(t)
            is_major = k % ratio == 0
            p.setPen(QColor(theme.TEXT_DIM if is_major else theme.TEXT_FAINT))
            p.drawLine(QLineF(x, r.bottom() - (8 if is_major else 4), x, r.bottom()))
            if is_major and t >= 0:
                p.drawText(QPointF(x + 3, r.top() + 12), fmt_time(t, 1 if major < 1 else 0))
            k += 1

    def _thumb_pixmap(self, index: int, height: int) -> Optional[QPixmap]:
        key = index * 1000 + height
        pm = self._thumb_cache.get(key)
        if pm is not None:
            self._thumb_cache.move_to_end(key)
            return pm
        info = self._thumbs
        if not info:
            return None
        path = thumbnail_path(info["dir"], index)
        if not os.path.isfile(path):
            return None
        img = QImage(path)
        if img.isNull():
            return None
        pm = QPixmap.fromImage(img.scaledToHeight(height, Qt.SmoothTransformation))
        self._thumb_cache[key] = pm
        while len(self._thumb_cache) > 800:
            self._thumb_cache.popitem(last=False)
        return pm

    def _paint_thumbs(self, p: QPainter, r: QRectF) -> None:
        p.fillRect(r, QColor(theme.BG1))
        info = self._thumbs
        h = int(r.height())
        tile_w = max(16.0, h * self._thumb_aspect)
        start_px = self.offset * self.pps
        k0 = int(start_px // tile_w)
        k1 = int((start_px + self.width()) // tile_w) + 1
        dur = self.display_duration()
        if not info or not info.get("count"):
            p.setPen(QColor(theme.TEXT_FAINT))
            p.drawText(r.adjusted(8, 0, 0, 0), Qt.AlignVCenter | Qt.AlignLeft,
                       tr("Generating thumbnails..."))
            return
        interval = float(info.get("interval", 1.0)) or 1.0
        count = int(info.get("count", 0))
        for k in range(k0, k1 + 1):
            x = k * tile_w - start_px
            center = (k * tile_w + tile_w / 2) / self.pps
            if center > dur:
                break
            src = self.to_src(center)
            idx = min(count - 1, max(0, int(src / interval + 0.5)))
            pm = self._thumb_pixmap(idx, h)
            if pm is not None:
                p.drawPixmap(QPointF(x, r.top()), pm, QRectF(0, 0, min(pm.width(), tile_w), h))
            p.setPen(QColor(theme.BG0))
            p.drawLine(QLineF(x, r.top(), x, r.bottom()))

    def _seg_color(self, seg) -> QColor:
        if seg.action == Action.CUT:
            return QColor(theme.CUT)
        if seg.action == Action.SPEED:
            return QColor(theme.SPEED)
        return QColor(theme.KEEP)

    def _paint_segments(self, p: QPainter, r: QRectF, tl: Timeline) -> None:
        p.fillRect(r, QColor(theme.BG1))
        i0, i1 = self._visible_range()
        selected = set(self.ctrl.selected_ids())
        settings = self.ctrl.settings
        f = QFont(self.font())
        f.setPointSizeF(max(7.5, f.pointSizeF() - 1))
        p.setFont(f)
        fm = p.fontMetrics()
        cut_markers: List[Tuple[float, int]] = []
        for i in range(i0, i1 + 1):
            seg = tl[i]
            x0 = self.x_of(self._disp_starts[i])
            x1 = self.x_of(self._disp_ends[i])
            if self.mode == EDITED and seg.action == Action.CUT:
                cut_markers.append((x0, i))
                continue
            rect = QRectF(x0, r.top() + 1, max(1.0, x1 - x0), r.height() - 2)
            col = self._seg_color(seg)
            if i == self._hover_seg:
                col = col.lighter(118)
            if seg.action == Action.CUT:
                p.fillRect(rect, theme.color(theme.CUT, 70))
                p.fillRect(rect, QBrush(theme.color(theme.CUT, 150), Qt.BDiagPattern))
            else:
                col.setAlpha(205)
                p.fillRect(rect, col)
            if seg.review and not seg.reviewed:
                p.fillRect(QRectF(rect.left(), rect.top(), rect.width(), 5), QColor(theme.REVIEW))
            elif seg.review and seg.reviewed:
                p.fillRect(QRectF(rect.left(), rect.top(), rect.width(), 2),
                           theme.color(theme.REVIEW, 140))
            if seg.manual:
                tri = QPolygonF([QPointF(rect.left(), rect.top()),
                                 QPointF(rect.left() + 7, rect.top()),
                                 QPointF(rect.left(), rect.top() + 7)])
                if rect.width() > 8:
                    p.setPen(Qt.NoPen)
                    p.setBrush(QColor("#ffffff"))
                    p.drawPolygon(tri)
            # label
            if rect.width() > 26:
                speed = seg.speed_value(settings)
                sp = "✂" if seg.action == Action.CUT else fmt_speed(speed)
                label = f"{kind_label(seg.kind)} {sp}" if seg.reason != "unanalyzed" \
                    else f"{tr('Not analysed yet')} {sp}"
                if seg.review and not seg.reviewed:
                    label = "? " + label
                if fm.horizontalAdvance(label) > rect.width() - 8:
                    label = sp
                if fm.horizontalAdvance(label) <= rect.width() - 6:
                    p.setPen(QColor("#101114") if seg.action != Action.CUT else QColor(theme.TEXT))
                    p.drawText(rect.adjusted(5, 3, -3, 0), Qt.AlignLeft | Qt.AlignVCenter, label)
            if seg.protected and rect.width() > 16:
                p.drawPixmap(QPointF(rect.right() - 15, rect.top() + 6),
                             icons.icon("protect", "#101114").pixmap(12, 12))
            # separator
            p.setPen(QColor(theme.BG0))
            p.drawLine(QLineF(x0, r.top(), x0, r.bottom()))
            if seg.id in selected:
                p.setPen(QPen(QColor("#ffffff"), 2))
                p.setBrush(Qt.NoBrush)
                p.drawRect(rect.adjusted(1, 1, -1, -1))
        # cut markers (edited view)
        for x, i in cut_markers:
            seg = tl[i]
            sel = seg.id in selected
            col = QColor("#ffffff") if sel else QColor(theme.CUT)
            p.setPen(QPen(col, 2))
            p.drawLine(QLineF(x, r.top(), x, r.bottom()))
            p.setPen(Qt.NoPen)
            p.setBrush(col)
            p.drawPolygon(QPolygonF([QPointF(x - 5, r.top()), QPointF(x + 5, r.top()),
                                     QPointF(x, r.top() + 7)]))
            p.drawPolygon(QPolygonF([QPointF(x - 5, r.bottom()), QPointF(x + 5, r.bottom()),
                                     QPointF(x, r.bottom() - 7)]))

    def _paint_wave(self, p: QPainter, r: QRectF, tl: Timeline) -> None:
        p.fillRect(r, QColor(theme.BG0))
        res = self.ctrl.project.analysis if self.ctrl.project else None
        mid = r.center().y()
        p.setPen(QColor(theme.BORDER))
        p.drawLine(QLineF(0, mid, r.right(), mid))
        if res is None or len(res.wave_peaks) == 0:
            if res is None:
                p.setPen(QColor(theme.TEXT_FAINT))
                p.drawText(r.adjusted(8, 0, 0, 0), Qt.AlignVCenter | Qt.AlignLeft,
                           tr("Audio waveform appears after the automatic edit."))
            return
        peaks = res.wave_peaks
        rate = float(res.wave_rate)
        w = self.width()
        end_x = min(w, int(self.x_of(self.display_duration())) + 1)
        if end_x <= 0:
            return
        xs = np.arange(0, end_x + 1, dtype=np.float64)
        disp = self.offset + xs / self.pps
        if self.mode == EDITED and self._map is not None and self._map.pieces:
            src = self._map.out_to_src_array(disp)
            pidx = self._map.piece_index_array(disp[:-1])
            seg_idx = np.asarray([pp.index for pp in self._map.pieces])[pidx]
        else:
            src = disp
            starts = np.asarray([s.start for s in tl])
            seg_idx = np.clip(np.searchsorted(starts, disp[:-1], side="right") - 1, 0, len(tl) - 1)
        bins = np.clip((src * rate).astype(np.int64), 0, len(peaks) - 1)
        bins = np.maximum.accumulate(bins)
        vals = np.maximum.reduceat(peaks, bins[:-1]).astype(np.float32) / 255.0
        half = r.height() / 2 - 2
        heights = vals * half
        actions = np.asarray([0 if s.action == Action.KEEP else 1 if s.action == Action.SPEED else 2
                              for s in tl])[seg_idx]
        muted = np.asarray([s.volume_value(self.ctrl.settings) <= 0.001 for s in tl])[seg_idx]
        colors = {0: theme.color(theme.WAVE, 220), 1: theme.color(theme.SPEED, 200),
                  2: theme.color(theme.CUT, 150)}
        for code, col in colors.items():
            for mute_flag in (False, True):
                sel = np.flatnonzero((actions == code) & (muted == mute_flag))
                if len(sel) == 0:
                    continue
                c = QColor(col)
                if mute_flag:
                    c.setAlpha(60)
                p.setPen(QPen(c, 1))
                lines = [QLineF(float(x) + 0.5, mid - float(hh), float(x) + 0.5, mid + float(hh))
                         for x, hh in zip(sel, heights[sel])]
                p.drawLines(lines)

    def _paint_playhead(self, p: QPainter) -> None:
        x = self.x_of(self.to_disp(self.playhead))
        if x < -10 or x > self.width() + 10:
            return
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(QPen(QColor(theme.PLAYHEAD), 1.5))
        p.drawLine(QLineF(x, 2, x, self.height()))
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(theme.PLAYHEAD))
        p.drawPolygon(QPolygonF([QPointF(x - 6, 2), QPointF(x + 6, 2), QPointF(x + 6, 10),
                                 QPointF(x, 16), QPointF(x - 6, 10)]))
        p.setRenderHint(QPainter.Antialiasing, False)

    # ------------------------------------------------------------ hit testing
    def _segment_at_x(self, x: float) -> int:
        if len(self._disp_starts) == 0:
            return -1
        t = self.disp_at(x)
        i = int(np.searchsorted(self._disp_starts, t, side="right")) - 1
        tl = self.ctrl.timeline
        # skip zero-width cut segments in edited mode
        while self.mode == EDITED and 0 <= i < len(tl) and tl[i].action == Action.CUT and \
                self._disp_ends[i] <= t and i + 1 < len(tl):
            i += 1
        return min(max(i, 0), len(self._disp_starts) - 1)

    def _cut_marker_at(self, x: float) -> int:
        if self.mode != EDITED:
            return -1
        tl = self.ctrl.timeline
        i0, i1 = self._visible_range()
        best, best_d = -1, EDGE_HIT + 1.0
        for i in range(i0, i1 + 1):
            if tl[i].action == Action.CUT:
                d = abs(self.x_of(self._disp_starts[i]) - x)
                if d < best_d:
                    best, best_d = i, d
        return best

    def _boundary_at(self, x: float) -> int:
        """Index b of the boundary between segments b-1 and b near x (or -1)."""
        tl = self.ctrl.timeline
        if tl is None or len(tl) < 2:
            return -1
        i0, i1 = self._visible_range()
        best, best_d = -1, EDGE_HIT + 1.0
        for b in range(max(1, i0), min(len(tl) - 1, i1 + 1) + 1):
            if b >= len(tl):
                break
            if self.mode == EDITED and (tl[b].action == Action.CUT or tl[b - 1].action == Action.CUT):
                continue  # handled by cut markers
            bx = self.x_of(self._disp_starts[b])
            d = abs(bx - x)
            if d < best_d:
                best, best_d = b, d
        return best

    def _in_segment_track(self, y: float) -> bool:
        _, _, segs, _ = self._tracks()
        return segs.top() <= y <= segs.bottom()

    # ------------------------------------------------------------ mouse
    def mousePressEvent(self, e) -> None:
        tl = self.ctrl.timeline
        if tl is None or self.display_duration() <= 0:
            return
        x, y = e.position().x(), e.position().y()
        if e.button() == Qt.RightButton:
            if self._in_segment_track(y):
                i = self._cut_marker_at(x)
                if i < 0:
                    i = self._segment_at_x(x)
                if i >= 0 and tl[i].id not in self.ctrl.selected_ids():
                    self.ctrl.select_index(i)
                self.contextMenuRequested.emit(e.globalPosition().toPoint(), i)
            return
        if e.button() != Qt.LeftButton:
            return
        if self._in_segment_track(y):
            cut = self._cut_marker_at(x)
            b = self._boundary_at(x) if cut < 0 else -1
            if cut >= 0 and not (e.modifiers() & (Qt.ControlModifier | Qt.ShiftModifier)):
                self.ctrl.select_index(cut)
                self._drag = {"kind": "cut", "index": cut, "x0": x, "dir": 0}
                return
            if b >= 0 and not (e.modifiers() & (Qt.ControlModifier | Qt.ShiftModifier)):
                self._start_edge_drag(b, x)
                return
            i = self._segment_at_x(x)
            self.ctrl.select_index(i, extend=bool(e.modifiers() & Qt.ShiftModifier),
                                   toggle=bool(e.modifiers() & Qt.ControlModifier))
            return
        # ruler / thumbnails / waveform: scrub
        self._drag = {"kind": "scrub"}
        self.seekRequested.emit(self.to_src(self.disp_at(x)))

    def _start_edge_drag(self, b: int, x: float, scale_index: Optional[int] = None) -> None:
        tl = self.ctrl.timeline
        if scale_index is None:
            scale_index = b - 1
        speed = 1.0
        if self.mode == EDITED:
            speed = tl[scale_index].speed_value(self.ctrl.settings)
            if math.isinf(speed):
                speed = 1.0
        self.ctrl.begin_boundary_drag()
        self._drag = {"kind": "edge", "b": b, "x0": x, "t0": tl[b].start, "speed": speed}

    def mouseMoveEvent(self, e) -> None:
        x, y = e.position().x(), e.position().y()
        d = self._drag
        tl = self.ctrl.timeline
        if d is None:
            self._update_hover(x, y)
            return
        if d["kind"] == "scrub":
            self.seekRequested.emit(self.to_src(self.disp_at(max(0.0, x))))
            self._autoscroll(x)
        elif d["kind"] == "cut":
            dx = x - d["x0"]
            if abs(dx) >= 3 and tl is not None:
                c = d["index"]
                # pulling the marker apart reveals removed material
                if dx < 0 and c + 1 < len(tl):
                    self._start_edge_drag(c + 1, d["x0"], scale_index=c + 1)
                elif dx > 0 and c > 0:
                    self._start_edge_drag(c, d["x0"], scale_index=c - 1)
                else:
                    return
                self.mouseMoveEvent(e)
        elif d["kind"] == "edge":
            dt = (x - d["x0"]) / self.pps * d["speed"]
            t = d["t0"] + dt
            # snap to the playhead
            ph_x = self.x_of(self.to_disp(self.playhead))
            if abs(ph_x - x) < 6:
                t = self.playhead
            self.ctrl.move_boundary(d["b"], t)
            self.setCursor(Qt.SplitHCursor)
            self._autoscroll(x)

    def _autoscroll(self, x: float) -> None:
        if x > self.width() - 10:
            self.offset += 20 / self.pps
        elif x < 10:
            self.offset -= 20 / self.pps
        else:
            return
        self._clamp_offset()
        self.viewChanged.emit()
        self.update()

    def mouseReleaseEvent(self, e) -> None:
        d = self._drag
        self._drag = None
        if d is not None and d["kind"] == "edge":
            self.ctrl.end_boundary_drag()
        self._update_hover(e.position().x(), e.position().y())

    def mouseDoubleClickEvent(self, e) -> None:
        tl = self.ctrl.timeline
        if tl is None or not self._in_segment_track(e.position().y()):
            return
        i = self._segment_at_x(e.position().x())
        if i >= 0:
            self.playFromRequested.emit(tl[i].start)

    def _update_hover(self, x: float, y: float) -> None:
        tl = self.ctrl.timeline
        hover = -1
        cursor = Qt.ArrowCursor
        tip = ""
        if tl is not None and self._in_segment_track(y) and self.display_duration() > 0:
            cut = self._cut_marker_at(x)
            if cut >= 0:
                cursor = Qt.SplitHCursor
                hover = cut
            elif self._boundary_at(x) >= 0:
                cursor = Qt.SplitHCursor
            else:
                hover = self._segment_at_x(x)
            if hover >= 0:
                seg = tl[hover]
                sp = seg.speed_value(self.ctrl.settings)
                tip = (f"{kind_label(seg.kind)} · "
                       f"{tr('Deleted') if seg.action == Action.CUT else fmt_speed(sp)}\n"
                       f"{fmt_time(seg.start, 2)} – {fmt_time(seg.end, 2)} "
                       f"({seg.duration:.1f}s)")
        elif y <= RULER_H:
            cursor = Qt.PointingHandCursor
        self.setCursor(cursor)
        self.setToolTip(tip)
        if hover != self._hover_seg:
            self._hover_seg = hover
            self.update()

    def leaveEvent(self, _e) -> None:
        if self._hover_seg != -1:
            self._hover_seg = -1
            self.update()

    def wheelEvent(self, e) -> None:
        delta = e.angleDelta()
        if e.modifiers() & Qt.ControlModifier:
            steps = delta.y() / 120.0
            self.zoom(1.25 ** steps, e.position().x())
            return
        dx = delta.x() if abs(delta.x()) > abs(delta.y()) else delta.y()
        self.offset -= dx / 120.0 * 60.0 / self.pps
        self._clamp_offset()
        self.update()
        self.viewChanged.emit()


class TimelinePanel(QWidget):
    """Timeline with its tool bar and scroll bar."""

    def __init__(self, controller, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.ctrl = controller
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        bar = QWidget()
        bar.setObjectName("timelineBar")
        bar.setAttribute(Qt.WA_StyledBackground)
        bl = QHBoxLayout(bar)
        bl.setContentsMargins(6, 2, 6, 2)
        bl.setSpacing(2)
        self.split_btn = tool_button("split", "")
        self.keep_btn = tool_button("keep", "")
        self.speed_btn = tool_button("speed", "")
        self.delete_btn = tool_button("delete", "")
        self.restore_btn = tool_button("restore", "")
        self.protect_btn = tool_button("protect", "")
        for b in (self.split_btn, self.keep_btn, self.speed_btn, self.delete_btn,
                  self.restore_btn, self.protect_btn):
            bl.addWidget(b)
        bl.addSpacing(12)
        self.legend = QLabel()
        self.legend.setObjectName("dim")
        bl.addWidget(self.legend)
        bl.addStretch(1)
        self.mode_edited = QToolButton()
        self.mode_source = QToolButton()
        for b in (self.mode_edited, self.mode_source):
            b.setCheckable(True)
            b.setAutoExclusive(True)
            b.setFocusPolicy(Qt.NoFocus)
            bl.addWidget(b)
        self.mode_edited.setChecked(True)
        bl.addSpacing(10)
        self.zoom_out_btn = tool_button("zoom_out", "")
        self.zoom_slider = QSlider(Qt.Horizontal)
        self.zoom_slider.setRange(0, 1000)
        self.zoom_slider.setFixedWidth(120)
        self.zoom_slider.setFocusPolicy(Qt.NoFocus)
        self.zoom_in_btn = tool_button("zoom_in", "")
        self.fit_btn = tool_button("fit", "")
        for w in (self.zoom_out_btn, self.zoom_slider, self.zoom_in_btn, self.fit_btn):
            bl.addWidget(w)
        lay.addWidget(bar)
        self.view = TimelineView(controller)
        lay.addWidget(self.view, 1)
        self.scroll = QScrollBar(Qt.Horizontal)
        lay.addWidget(self.scroll)
        self._syncing = False
        self.view.viewChanged.connect(self._sync_scroll)
        self.scroll.valueChanged.connect(self._on_scroll)
        self.zoom_slider.valueChanged.connect(self._on_zoom_slider)
        self.zoom_in_btn.clicked.connect(lambda: self.view.zoom(1.5))
        self.zoom_out_btn.clicked.connect(lambda: self.view.zoom(1 / 1.5))
        self.fit_btn.clicked.connect(self.view.zoom_to_fit)
        self.mode_edited.toggled.connect(
            lambda on: on and self.view.set_mode(EDITED))
        self.mode_source.toggled.connect(
            lambda on: on and self.view.set_mode(SOURCE))
        self.retranslate()

    def retranslate(self) -> None:
        self.split_btn.setToolTip(tr("Split at playhead (S)"))
        self.keep_btn.setToolTip(tr("Keep at normal speed (1)"))
        self.speed_btn.setToolTip(tr("Speed up as board writing (2)"))
        self.delete_btn.setToolTip(tr("Delete and close the gap (Delete)"))
        self.restore_btn.setToolTip(tr("Restore deleted segment (R)"))
        self.protect_btn.setToolTip(tr("Always keep this segment (P)"))
        self.zoom_in_btn.setToolTip(tr("Zoom in (+ / Ctrl+wheel)"))
        self.zoom_out_btn.setToolTip(tr("Zoom out (-)"))
        self.fit_btn.setToolTip(tr("Fit whole timeline (0)"))
        self.mode_edited.setText(tr("Edited length"))
        self.mode_edited.setToolTip(tr("Show the timeline after cuts and speed changes"))
        self.mode_source.setText(tr("Original length"))
        self.mode_source.setToolTip(tr("Show the timeline at the original length"))
        self.legend.setText(
            f"<span style='color:{theme.KEEP}'>■</span> {tr('Normal speed')}&nbsp;&nbsp;"
            f"<span style='color:{theme.SPEED}'>■</span> {tr('Speed up')}&nbsp;&nbsp;"
            f"<span style='color:{theme.CUT}'>■</span> {tr('Delete')}&nbsp;&nbsp;"
            f"<span style='color:{theme.REVIEW}'>■</span> {tr('To check')}")

    def _sync_scroll(self) -> None:
        self._syncing = True
        total = self.view.content_width()
        page = max(1, self.view.width())
        self.scroll.setRange(0, max(0, total - page))
        self.scroll.setPageStep(page)
        self.scroll.setSingleStep(max(1, page // 20))
        self.scroll.setValue(self.view.scroll_px())
        self.zoom_slider.setValue(int(self.view.zoom_level() * 1000))
        self._syncing = False

    def _on_scroll(self, value: int) -> None:
        if not self._syncing:
            self.view.set_scroll(value)

    def _on_zoom_slider(self, value: int) -> None:
        if not self._syncing:
            self.view.set_zoom_level(value / 1000.0)
