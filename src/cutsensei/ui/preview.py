"""Preview playback of the edited timeline.

Two ``QMediaPlayer`` instances are used.  While one plays, the other one is
paused at the start of the next kept part after a cut, so jumping over a
cut is an instant switch instead of a slow seek.  Speed changes between
adjacent parts only change the playback rate of the active player.
"""

from __future__ import annotations

from typing import List, Optional

from PySide6.QtCore import QObject, QRectF, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPainterPath
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer, QVideoFrame, QVideoSink
from PySide6.QtWidgets import (QHBoxLayout, QLabel, QSizePolicy, QSlider, QToolButton,
                               QVBoxLayout, QWidget)

from ..core.timeline import Action, EditMap, Timeline
from . import icons, theme
from .fmt import fmt_speed, fmt_time, kind_label
from .i18n import tr

EDITED = "edited"
SOURCE = "source"


class _Slot:
    def __init__(self, owner: "PreviewEngine", index: int) -> None:
        self.index = index
        self.player = QMediaPlayer(owner)
        self.sink = QVideoSink(owner)
        self.audio = QAudioOutput(owner)
        self.player.setVideoSink(self.sink)
        self.player.setAudioOutput(self.audio)
        if hasattr(self.player, "setPitchCompensation"):
            try:
                self.player.setPitchCompensation(True)
            except Exception:
                pass
        self.frame: Optional[QVideoFrame] = None
        self.frame_time = -1.0
        self.preloaded: Optional[float] = None
        self.rate = 1.0
        self.volume = -1.0


class PreviewEngine(QObject):
    frameReady = Signal(object)          # QVideoFrame of the active player
    positionChanged = Signal(float)      # source time in seconds
    playingChanged = Signal(bool)
    mediaLoaded = Signal(bool, str)

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._slots = [_Slot(self, 0), _Slot(self, 1)]
        self._active = 0
        for slot in self._slots:
            slot.sink.videoFrameChanged.connect(
                lambda frame, s=slot: self._on_frame(s, frame))
            slot.player.mediaStatusChanged.connect(
                lambda status, s=slot: self._on_status(s, status))
            slot.player.errorOccurred.connect(
                lambda _err, msg, s=slot: self._on_error(s, msg))
        self._map: Optional[EditMap] = None
        self._timeline: Optional[Timeline] = None
        self._mode = EDITED
        self._playing = False
        self._pos = 0.0
        self._duration = 0.0
        self._fps = 30.0
        self._volume = 0.8
        self._muted = False
        self._stop_at: Optional[float] = None
        self._return_to: Optional[float] = None
        self._restore_mode: Optional[str] = None
        self._loaded = False
        self._source: Optional[str] = None
        self._timer = QTimer(self)
        self._timer.setInterval(15)
        self._timer.timeout.connect(self._tick)

    # ------------------------------------------------------------ properties
    @property
    def active(self) -> _Slot:
        return self._slots[self._active]

    @property
    def standby(self) -> _Slot:
        return self._slots[1 - self._active]

    @property
    def position(self) -> float:
        return self._pos

    @property
    def playing(self) -> bool:
        return self._playing

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def fps(self) -> float:
        return self._fps

    @property
    def edit_map(self) -> Optional[EditMap]:
        return self._map

    # ------------------------------------------------------------ setup
    def load(self, path: Optional[str], duration: float, fps: float) -> None:
        self.pause()
        self._loaded = False
        self._source = path
        self._duration = duration
        self._fps = fps if fps > 0 else 30.0
        self._pos = 0.0
        for slot in self._slots:
            slot.frame = None
            slot.frame_time = -1.0
            slot.preloaded = None
            slot.player.setSource(QUrl.fromLocalFile(path) if path else QUrl())
            slot.audio.setVolume(0.0)
        self._active = 0

    def unload(self) -> None:
        self.load(None, 0.0, 30.0)

    def set_edit(self, timeline: Optional[Timeline], edit_map: Optional[EditMap]) -> None:
        self._timeline = timeline
        self._map = edit_map
        for slot in self._slots:
            slot.preloaded = None
        if self._playing and self._mode == EDITED:
            self._preload_next()

    def set_mode(self, mode: str) -> None:
        if mode == self._mode:
            return
        self._mode = mode
        self._restore_mode = None
        if mode == SOURCE:
            self._apply_rate(self.active, 1.0, 1.0)
        elif self._map is not None and self._map.piece_index_at_src(self._pos) < 0:
            self.seek(self._pos)
        if self._playing:
            self._preload_next()

    def set_volume(self, volume: float, muted: bool) -> None:
        self._volume = max(0.0, min(1.0, volume))
        self._muted = muted
        self.active.volume = -1.0
        if self._playing:
            self._apply_current()

    # ------------------------------------------------------------ transport
    def toggle(self) -> None:
        if self._playing:
            self.pause()
        else:
            self.play()

    def play(self) -> None:
        if not self._loaded:
            return
        if self._mode == EDITED and self._map is not None:
            if not self._map.pieces:
                return
            i = self._map.piece_index_at_src(self._pos)
            if i < 0:
                j = self._map.next_piece_index(self._pos)
                if j < 0:
                    j = 0
                self._seek_active(self._map.pieces[j].src_start)
            elif self._pos >= self._map.pieces[-1].src_end - 1e-3:
                self._seek_active(self._map.pieces[0].src_start)
        elif self._pos >= self._duration - 0.05:
            self._seek_active(0.0)
        self._playing = True
        self._apply_current(force=True)
        self.active.player.play()
        self._timer.start()
        self._preload_next()
        self.playingChanged.emit(True)

    def pause(self) -> None:
        was = self._playing
        self._playing = False
        self._timer.stop()
        for slot in self._slots:
            if slot.player.playbackState() == QMediaPlayer.PlayingState:
                slot.player.pause()
            slot.audio.setVolume(0.0)
            slot.volume = -1.0
        if self._restore_mode is not None:
            self._mode = self._restore_mode
            self._restore_mode = None
        self._stop_at = None
        self._return_to = None
        if was:
            self.playingChanged.emit(False)

    def seek(self, t: float) -> None:
        """Seek to a source time (snapped to kept material in edited mode)."""
        t = min(max(0.0, t), max(0.0, self._duration - 0.5 / self._fps))
        if self._mode == EDITED and self._map is not None and self._map.pieces:
            if self._map.piece_index_at_src(t) < 0:
                j = self._map.next_piece_index(t)
                t = self._map.pieces[j].src_start if j >= 0 else \
                    self._map.pieces[-1].src_end - 1.0 / self._fps
        self._seek_active(t)
        if self._playing:
            self._apply_current(force=True)
            self._preload_next()

    def seek_output(self, t_out: float) -> None:
        if self._map is not None:
            self.seek(self._map.out_to_src(t_out))

    def step(self, frames: int) -> None:
        self.pause()
        dt = frames / self._fps
        t = self._pos + dt
        if self._mode == EDITED and self._map is not None and self._map.pieces:
            if self._map.piece_index_at_src(t) < 0:
                if frames > 0:
                    j = self._map.next_piece_index(t)
                    t = self._map.pieces[j].src_start if j >= 0 else self._pos
                else:
                    # previous kept frame before the cut
                    prev = [p for p in self._map.pieces if p.src_end <= t + 1e-6]
                    t = prev[-1].src_end - 1.0 / self._fps if prev else self._pos
        self._seek_active(t)

    def play_range(self, start: float, end: float, mode: Optional[str] = None,
                   return_to: Optional[float] = None) -> None:
        """Play ``[start, end)`` (source time) and pause at the end.

        ``mode`` temporarily switches edited/original preview; ``return_to``
        moves the playhead back there afterwards (used by "play around").
        """
        if not self._loaded:
            return
        self.pause()
        restore = None
        if mode is not None and mode != self._mode:
            restore = self._mode
            self._mode = mode
        self._seek_active(start)
        self.play()
        self._stop_at = end
        self._return_to = return_to
        self._restore_mode = restore

    # ------------------------------------------------------------ internals
    def _seek_active(self, t: float) -> None:
        self._pos = t
        self.active.player.setPosition(int(round(t * 1000)))
        self.positionChanged.emit(t)

    def _piece_for(self, t: float):
        if self._map is None:
            return None
        i = self._map.piece_index_at_src(t)
        return self._map.pieces[i] if i >= 0 else None

    def _apply_rate(self, slot: _Slot, rate: float, volume: float) -> None:
        if abs(slot.rate - rate) > 1e-6:
            slot.player.setPlaybackRate(rate)
            slot.rate = rate
        vol = 0.0 if self._muted else self._volume * min(1.0, volume)
        if abs(slot.volume - vol) > 1e-6:
            slot.audio.setVolume(vol)
            slot.volume = vol

    def _apply_current(self, force: bool = False) -> None:
        slot = self.active
        if force:
            slot.volume = -1.0
        if self._mode == SOURCE or self._map is None:
            self._apply_rate(slot, 1.0, 1.0)
            return
        p = self._piece_for(self._pos)
        if p is not None:
            self._apply_rate(slot, p.speed, p.volume)

    def _next_jump_target(self, t: float) -> Optional[float]:
        if self._map is None or not self._map.pieces:
            return None
        pieces = self._map.pieces
        i = self._map.piece_index_at_src(t)
        if i < 0:
            i = self._map.next_piece_index(t)
            if i < 0:
                return None
        for k in range(i + 1, len(pieces)):
            if abs(pieces[k].src_start - pieces[k - 1].src_end) > 1e-3:
                return pieces[k].src_start
        return None

    def _preload_next(self) -> None:
        if self._mode != EDITED:
            return
        target = self._next_jump_target(self._pos)
        sb = self.standby
        if target is None:
            sb.preloaded = None
            return
        if sb.preloaded is not None and abs(sb.preloaded - target) < 1e-3:
            return
        sb.preloaded = target
        sb.frame_time = -1.0
        if sb.player.playbackState() == QMediaPlayer.PlayingState:
            sb.player.pause()
        sb.audio.setVolume(0.0)
        sb.volume = -1.0
        sb.player.pause()
        sb.player.setPosition(int(round(target * 1000)))

    def _jump(self, target: float) -> None:
        sb = self.standby
        ready = (sb.preloaded is not None and abs(sb.preloaded - target) < 1e-3
                 and sb.frame_time >= 0 and abs(sb.frame_time - target) < 0.5)
        if ready:
            old = self.active
            self._active = 1 - self._active
            self._pos = target
            self._apply_current(force=True)
            self.active.player.play()
            old.player.pause()
            old.audio.setVolume(0.0)
            old.volume = -1.0
            old.preloaded = None
            if self.active.frame is not None:
                self.frameReady.emit(self.active.frame)
        else:
            self.active.player.setPosition(int(round(target * 1000)))
            self._pos = target
            self._apply_current(force=True)
        self._preload_next()

    def _finish(self) -> None:
        self.pause()
        self.positionChanged.emit(self._pos)

    def _tick(self) -> None:
        if not self._playing:
            return
        slot = self.active
        t = slot.player.position() / 1000.0
        # position() may lag behind right after a switch
        if slot.frame_time >= 0 and abs(slot.frame_time - t) < 0.25:
            t = max(t, slot.frame_time)
        self._pos = t
        if self._stop_at is not None and t >= self._stop_at:
            target = self._return_to if self._return_to is not None else \
                min(self._stop_at, self._duration)
            self.pause()
            self._seek_active(target)
            return
        if self._mode == EDITED and self._map is not None and self._map.pieces:
            pieces = self._map.pieces
            i = self._map.piece_index_at_src(t)
            if i < 0:
                j = self._map.next_piece_index(t)
                if j < 0:
                    self._finish()
                    return
                self._jump(pieces[j].src_start)
                self.positionChanged.emit(self._pos)
                return
            p = pieces[i]
            self._apply_rate(slot, p.speed, p.volume)
            look = 0.02 * p.speed + 0.012
            if t >= p.src_end - look:
                if i + 1 >= len(pieces):
                    if t >= p.src_end - 0.03 or p.src_end >= self._duration - 0.05:
                        if p.src_end < self._duration - 0.05:
                            self._finish()
                            return
                else:
                    nxt = pieces[i + 1]
                    if abs(nxt.src_start - p.src_end) > 1e-3:
                        self._jump(nxt.src_start)
                    else:
                        self._apply_rate(slot, nxt.speed, nxt.volume)
        self.positionChanged.emit(self._pos)

    def _on_frame(self, slot: _Slot, frame: QVideoFrame) -> None:
        if not frame.isValid():
            return
        slot.frame = frame
        st = frame.startTime()
        if st >= 0:
            slot.frame_time = st / 1_000_000.0
        if slot is self.active:
            if not self._playing and slot.frame_time >= 0:
                pass
            self.frameReady.emit(frame)

    def _on_status(self, slot: _Slot, status) -> None:
        if status == QMediaPlayer.LoadedMedia and slot.index == 0 and not self._loaded:
            self._loaded = True
            slot.player.pause()
            slot.player.setPosition(0)
            self.mediaLoaded.emit(True, "")
        elif status == QMediaPlayer.LoadedMedia and slot.index == 1:
            slot.player.pause()
        elif status == QMediaPlayer.EndOfMedia and slot is self.active and self._playing:
            self._pos = self._duration
            self._finish()
        elif status == QMediaPlayer.InvalidMedia and slot.index == 0:
            self.mediaLoaded.emit(False, slot.player.errorString())

    def _on_error(self, slot: _Slot, message: str) -> None:
        if slot.index == 0 and not self._loaded:
            self.mediaLoaded.emit(False, message)


# ---------------------------------------------------------------------------
# widgets
# ---------------------------------------------------------------------------

class VideoView(QWidget):
    """Paints the current video frame with a status badge overlay."""

    clicked = Signal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setMinimumSize(320, 180)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setAttribute(Qt.WA_OpaquePaintEvent)
        self._frame: Optional[QVideoFrame] = None
        self._image: Optional[QImage] = None
        self._badge: List = []   # [(text, color)]
        self._hint = ""
        self._aspect = 16 / 9

    def sizeHint(self) -> QSize:
        return QSize(800, 450)

    def set_frame(self, frame: QVideoFrame) -> None:
        self._frame = frame
        self._image = None
        self.update()

    def clear(self) -> None:
        self._frame = None
        self._image = None
        self.update()

    def set_aspect(self, aspect: float) -> None:
        self._aspect = aspect if aspect > 0 else 16 / 9
        self.update()

    def set_badge(self, parts: List) -> None:
        if parts != self._badge:
            self._badge = parts
            self.update()

    def set_hint(self, text: str) -> None:
        self._hint = text
        self.update()

    def current_image(self) -> Optional[QImage]:
        if self._image is None and self._frame is not None and self._frame.isValid():
            img = self._frame.toImage()
            self._image = img if not img.isNull() else None
        return self._image

    def video_rect(self) -> QRectF:
        w, h = self.width(), self.height()
        img = self.current_image()
        aspect = (img.width() / img.height()) if img is not None and img.height() else self._aspect
        if w / max(1, h) > aspect:
            vw, vh = h * aspect, h
        else:
            vw, vh = w, w / aspect
        return QRectF((w - vw) / 2, (h - vh) / 2, vw, vh)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(theme.BG0))
        img = self.current_image()
        if img is not None:
            p.setRenderHint(QPainter.SmoothPixmapTransform)
            p.drawImage(self.video_rect(), img)
        elif self._hint:
            p.setPen(QColor(theme.TEXT_DIM))
            f = p.font()
            f.setPointSizeF(f.pointSizeF() * 1.1)
            p.setFont(f)
            p.drawText(self.rect().adjusted(40, 40, -40, -40), Qt.AlignCenter | Qt.TextWordWrap,
                       self._hint)
        if self._badge and img is not None:
            self._paint_badge(p)
        p.end()

    def _paint_badge(self, p: QPainter) -> None:
        f = QFont(self.font())
        f.setPointSizeF(max(9.0, f.pointSizeF()))
        f.setBold(True)
        p.setFont(f)
        fm = p.fontMetrics()
        x, y = 12.0, 12.0
        p.setRenderHint(QPainter.Antialiasing)
        for text, col in self._badge:
            w = fm.horizontalAdvance(text) + 16
            h = fm.height() + 8
            path = QPainterPath()
            path.addRoundedRect(QRectF(x, y, w, h), 4, 4)
            p.fillPath(path, theme.color(theme.BG0, 200))
            p.fillRect(QRectF(x, y, 4, h), QColor(col))
            p.setPen(QColor(theme.TEXT))
            p.drawText(QRectF(x + 4, y, w - 4, h), Qt.AlignCenter, text)
            x += w + 6


def tool_button(icon_name: str, tip: str, parent: Optional[QWidget] = None,
                checkable: bool = False, text: str = "") -> QToolButton:
    b = QToolButton(parent)
    b.setIcon(icons.icon(icon_name))
    b.setToolTip(tip)
    b.setAutoRaise(True)
    b.setFocusPolicy(Qt.NoFocus)
    b.setCheckable(checkable)
    if text:
        b.setText(text)
        b.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
    return b


class TransportBar(QWidget):
    """Play controls, current time and preview mode."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("transport")
        self.setAttribute(Qt.WA_StyledBackground)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(6, 3, 6, 3)
        lay.setSpacing(1)
        self.prev_mark = tool_button("prev_mark", "")
        self.frame_back = tool_button("frame_back", "")
        self.play = tool_button("play", "")
        self.play.setIconSize(QSize(22, 22))
        self.frame_fwd = tool_button("frame_fwd", "")
        self.next_mark = tool_button("next_mark", "")
        for b in (self.prev_mark, self.frame_back, self.play, self.frame_fwd, self.next_mark):
            lay.addWidget(b)
        lay.addSpacing(8)
        times = QVBoxLayout()
        times.setSpacing(0)
        times.setContentsMargins(0, 0, 0, 0)
        self.time_label = QLabel("00:00.00 / 00:00.0")
        self.time_label.setFont(theme.mono_font(10.5))
        self.time_label.setMinimumWidth(
            self.time_label.fontMetrics().horizontalAdvance("0:00:00.00 / 0:00:00.0") + 6)
        times.addWidget(self.time_label)
        self.src_label = QLabel("")
        self.src_label.setObjectName("dim")
        self.src_label.setFont(theme.mono_font(8.5))
        times.addWidget(self.src_label)
        lay.addLayout(times)
        lay.addStretch(1)
        self.prev_review = tool_button("prev_review", "")
        self.next_review = tool_button("next_review", "")
        self.around = tool_button("loop", "")
        lay.addWidget(self.prev_review)
        lay.addWidget(self.next_review)
        lay.addWidget(self.around)
        lay.addSpacing(8)
        self.mode_edited = QToolButton()
        self.mode_source = QToolButton()
        for b in (self.mode_edited, self.mode_source):
            b.setCheckable(True)
            b.setFocusPolicy(Qt.NoFocus)
            b.setAutoExclusive(True)
            lay.addWidget(b)
        self.mode_edited.setChecked(True)
        lay.addSpacing(8)
        self.mute = tool_button("volume", "", checkable=True)
        lay.addWidget(self.mute)
        self.volume = QSlider(Qt.Horizontal)
        self.volume.setRange(0, 100)
        self.volume.setValue(80)
        self.volume.setFixedWidth(90)
        self.volume.setFocusPolicy(Qt.NoFocus)
        lay.addWidget(self.volume)
        self.mute.toggled.connect(
            lambda on: self.mute.setIcon(icons.icon("mute" if on else "volume")))
        self.retranslate()

    def resizeEvent(self, e) -> None:
        super().resizeEvent(e)
        # drop secondary controls when space is short
        w = self.width()
        self.volume.setVisible(w >= 700)
        for b in (self.prev_review, self.next_review, self.around):
            b.setVisible(w >= 600)

    def retranslate(self) -> None:
        self.prev_mark.setToolTip(tr("Previous boundary (Up)"))
        self.frame_back.setToolTip(tr("Previous frame (Left)"))
        self.play.setToolTip(tr("Play / pause (Space)"))
        self.frame_fwd.setToolTip(tr("Next frame (Right)"))
        self.next_mark.setToolTip(tr("Next boundary (Down)"))
        self.prev_review.setToolTip(tr("Previous item to check (Shift+N)"))
        self.next_review.setToolTip(tr("Next item to check (N)"))
        self.around.setToolTip(tr("Play around the playhead (A)"))
        self.mode_edited.setText(tr("Edited"))
        self.mode_edited.setToolTip(tr("Preview with cuts and speed changes (V)"))
        self.mode_source.setText(tr("Original"))
        self.mode_source.setToolTip(tr("Preview the unedited source video (V)"))
        self.mute.setToolTip(tr("Mute"))
        self.volume.setToolTip(tr("Preview volume"))

    def set_playing(self, playing: bool) -> None:
        self.play.setIcon(icons.icon("pause" if playing else "play"))

    def set_times(self, out_t: float, out_total: float, src_t: float, show_src: bool) -> None:
        self.time_label.setText(f"{fmt_time(out_t, 2)} / {fmt_time(out_total, 1)}")
        self.src_label.setText(tr("source {t}").format(t=fmt_time(src_t, 2)) if show_src else "")


def badge_for(timeline: Optional[Timeline], edit_map: Optional[EditMap], src_t: float,
              settings) -> List:
    if timeline is None or len(timeline) == 0:
        return []
    seg = timeline.segment_at(src_t)
    if seg.reason == "unanalyzed":
        return [(tr("Not analysed yet"), theme.TEXT_DIM)]
    parts = [(kind_label(seg.kind), theme.KIND_COLORS.get(seg.kind, theme.TEXT_DIM))]
    if seg.action == Action.CUT:
        parts.append((tr("Deleted"), theme.CUT))
    else:
        speed = seg.speed_value(settings)
        parts.append((fmt_speed(speed), theme.SPEED if speed != 1.0 else theme.KEEP))
    if seg.review and not seg.reviewed:
        parts.append((tr("Check"), theme.REVIEW))
    if seg.protected:
        parts.append((tr("Protected"), theme.ACCENT))
    return parts
