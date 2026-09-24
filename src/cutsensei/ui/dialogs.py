"""Dialogs: board region, export, preferences, about."""

from __future__ import annotations

import os
import sys
from typing import List, Optional

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QBrush, QColor, QImage, QPainter, QPen
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
                               QFormLayout, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
                               QPushButton, QSlider, QVBoxLayout, QWidget)

from .. import __version__
from ..core import ffmpeg
from ..core.media import MediaInfo
from ..core.settings import Encoder, ExportSettings
from ..render.exporter import FPS_PRESETS, RESOLUTION_PRESETS
from . import icons, theme
from .fmt import fmt_time
from .i18n import SUPPORTED, tr


def grab_frame(media: MediaInfo, t: float, height: int = 720) -> Optional[QImage]:
    t = max(0.0, min(t, media.duration - 0.05))
    args = [ffmpeg.find_ffmpeg(), "-hide_banner", "-loglevel", "error", "-ss", f"{t:.3f}",
            "-i", media.path, "-frames:v", "1", "-vf", f"scale=-2:{min(height, media.height or height)}",
            "-f", "image2pipe", "-vcodec", "png", "pipe:1"]
    try:
        proc = ffmpeg.run(args, timeout=30, check=False)
    except Exception:
        return None
    img = QImage.fromData(proc.stdout, "PNG")
    return None if img.isNull() else img


# ============================================================================
# board region
# ============================================================================

class RegionCanvas(QWidget):
    changed = Signal()

    HANDLE = 8

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setMinimumSize(560, 315)
        self.image: Optional[QImage] = None
        self.regions: List[List[float]] = []
        self.excluded: List[List[float]] = []   # ignored automatically (webcam picture)
        self.selected = -1
        self._drag = None

    def sizeHint(self) -> QSize:
        return QSize(900, 506)

    def set_image(self, img: Optional[QImage]) -> None:
        self.image = img
        self.update()

    def image_rect(self) -> QRectF:
        w, h = self.width(), self.height()
        aspect = (self.image.width() / self.image.height()) if self.image else 16 / 9
        if w / max(1, h) > aspect:
            vw, vh = h * aspect, h
        else:
            vw, vh = w, w / aspect
        return QRectF((w - vw) / 2, (h - vh) / 2, vw, vh)

    def _to_norm(self, p: QPointF) -> QPointF:
        r = self.image_rect()
        return QPointF(min(1.0, max(0.0, (p.x() - r.left()) / r.width())),
                       min(1.0, max(0.0, (p.y() - r.top()) / r.height())))

    def _rect_px(self, reg: List[float]) -> QRectF:
        r = self.image_rect()
        return QRectF(r.left() + reg[0] * r.width(), r.top() + reg[1] * r.height(),
                      reg[2] * r.width(), reg[3] * r.height())

    def _hit(self, pos: QPointF):
        for i in reversed(range(len(self.regions))):
            rp = self._rect_px(self.regions[i])
            corners = {"tl": rp.topLeft(), "tr": rp.topRight(), "bl": rp.bottomLeft(),
                       "br": rp.bottomRight()}
            for name, c in corners.items():
                if abs(c.x() - pos.x()) <= self.HANDLE and abs(c.y() - pos.y()) <= self.HANDLE:
                    return i, name
            if rp.contains(pos):
                return i, "move"
        return -1, None

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(theme.BG0))
        r = self.image_rect()
        if self.image is not None:
            p.drawImage(r, self.image)
        else:
            p.setPen(QColor(theme.TEXT_DIM))
            p.drawText(self.rect(), Qt.AlignCenter, tr("Loading frame..."))
        if self.regions:
            # dim everything outside the regions
            p.save()
            outside = QRectF(r)
            from PySide6.QtGui import QPainterPath

            path = QPainterPath()
            path.addRect(outside)
            for reg in self.regions:
                inner = QPainterPath()
                inner.addRect(self._rect_px(reg))
                path = path.subtracted(inner)
            p.fillPath(path, theme.color("#000000", 120))
            p.restore()
        for reg in self.excluded:
            rp = self._rect_px(reg)
            p.fillRect(rp, QBrush(theme.color(theme.CUT, 150), Qt.BDiagPattern))
            p.setPen(QPen(QColor(theme.CUT), 1, Qt.DotLine))
            p.drawRect(rp)
            p.setPen(QColor("#ffffff"))
            p.drawText(rp.adjusted(4, 2, -2, 0), Qt.AlignLeft | Qt.AlignTop | Qt.TextWordWrap,
                       tr("Camera picture (ignored)"))
        p.setRenderHint(QPainter.Antialiasing)
        for i, reg in enumerate(self.regions):
            rp = self._rect_px(reg)
            sel = i == self.selected
            p.setPen(QPen(QColor(theme.ACCENT if sel else theme.SPEED), 2, Qt.DashLine))
            p.setBrush(Qt.NoBrush)
            p.drawRect(rp)
            p.setBrush(QColor(theme.ACCENT if sel else theme.SPEED))
            p.setPen(Qt.NoPen)
            for c in (rp.topLeft(), rp.topRight(), rp.bottomLeft(), rp.bottomRight()):
                p.drawRect(QRectF(c.x() - 4, c.y() - 4, 8, 8))
            p.setPen(QColor("#ffffff"))
            p.drawText(rp.adjusted(6, 4, 0, 0), Qt.AlignLeft | Qt.AlignTop,
                       tr("Board {n}").format(n=i + 1))
        if not self.regions:
            p.setPen(QColor(theme.TEXT))
            p.drawText(r.adjusted(0, 0, 0, -12), Qt.AlignHCenter | Qt.AlignBottom,
                       tr("Whole frame is analysed. Drag to mark the board area."))
        p.end()

    def mousePressEvent(self, e) -> None:
        pos = e.position()
        if e.button() != Qt.LeftButton:
            return
        i, part = self._hit(pos)
        if i >= 0:
            self.selected = i
            self._drag = {"i": i, "part": part, "start": self._to_norm(pos),
                          "orig": list(self.regions[i])}
        else:
            n = self._to_norm(pos)
            self.regions.append([n.x(), n.y(), 0.0, 0.0])
            self.selected = len(self.regions) - 1
            self._drag = {"i": self.selected, "part": "new", "start": n,
                          "orig": list(self.regions[-1])}
        self.update()

    def mouseMoveEvent(self, e) -> None:
        pos = e.position()
        d = self._drag
        if d is None:
            _, part = self._hit(pos)
            self.setCursor({"move": Qt.SizeAllCursor, "tl": Qt.SizeFDiagCursor,
                            "br": Qt.SizeFDiagCursor, "tr": Qt.SizeBDiagCursor,
                            "bl": Qt.SizeBDiagCursor}.get(part, Qt.CrossCursor))
            return
        n = self._to_norm(pos)
        x, y, w, h = d["orig"]
        s = d["start"]
        if d["part"] == "move":
            dx, dy = n.x() - s.x(), n.y() - s.y()
            x = min(max(0.0, x + dx), 1.0 - w)
            y = min(max(0.0, y + dy), 1.0 - h)
            reg = [x, y, w, h]
        else:
            x0, y0, x1, y1 = x, y, x + w, y + h
            part = d["part"]
            if part == "new":
                x0, y0, x1, y1 = s.x(), s.y(), n.x(), n.y()
            else:
                if "l" in part:
                    x0 = n.x()
                if "r" in part:
                    x1 = n.x()
                if "t" in part:
                    y0 = n.y()
                if "b" in part:
                    y1 = n.y()
            reg = [min(x0, x1), min(y0, y1), abs(x1 - x0), abs(y1 - y0)]
        self.regions[d["i"]] = reg
        self.update()

    def mouseReleaseEvent(self, _e) -> None:
        if self._drag is not None:
            i = self._drag["i"]
            if 0 <= i < len(self.regions):
                reg = self.regions[i]
                if reg[2] < 0.03 or reg[3] < 0.03:
                    del self.regions[i]
                    self.selected = -1
            self._drag = None
            self.update()
            self.changed.emit()

    def keyPressEvent(self, e) -> None:
        if e.key() in (Qt.Key_Delete, Qt.Key_Backspace) and 0 <= self.selected < len(self.regions):
            del self.regions[self.selected]
            self.selected = -1
            self.update()
            self.changed.emit()
            return
        super().keyPressEvent(e)


class BoardRegionDialog(QDialog):
    def __init__(self, media: MediaInfo, regions: List[List[float]], position: float,
                 parent: Optional[QWidget] = None,
                 excluded: Optional[List[List[float]]] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(tr("Board region"))
        self.media = media
        lay = QVBoxLayout(self)
        info = QLabel(tr("Mark the blackboard / whiteboard with one or more rectangles. Only "
                         "these areas are used to detect writing, so movement elsewhere (the "
                         "audience, a door, a screen) is ignored. Without a region the whole "
                         "frame is used.") + "\n" +
                      tr("Screen recordings of digital notes: mark the page area without the "
                         "app's toolbars and status bar. A camera picture of the lecturer is "
                         "found and ignored automatically."))
        info.setWordWrap(True)
        info.setObjectName("dim")
        lay.addWidget(info)
        self.canvas = RegionCanvas()
        self.canvas.regions = [list(r) for r in regions]
        self.canvas.excluded = [list(r) for r in (excluded or [])]
        lay.addWidget(self.canvas, 1)
        row = QHBoxLayout()
        row.addWidget(QLabel(tr("Frame")))
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(0, max(1, int(media.duration * 10)))
        self.slider.setValue(int(position * 10))
        row.addWidget(self.slider, 1)
        self.time_label = QLabel(fmt_time(position, 1))
        self.time_label.setFont(theme.mono_font())
        row.addWidget(self.time_label)
        lay.addLayout(row)
        brow = QHBoxLayout()
        self.del_btn = QPushButton(icons.icon("delete"), tr("Delete selected"))
        self.clear_btn = QPushButton(tr("Use whole frame"))
        brow.addWidget(self.del_btn)
        brow.addWidget(self.clear_btn)
        brow.addStretch(1)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText(tr("OK"))
        bb.button(QDialogButtonBox.Cancel).setText(tr("Cancel"))
        brow.addWidget(bb)
        lay.addLayout(brow)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        self.del_btn.clicked.connect(self._delete)
        self.clear_btn.clicked.connect(self._clear)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(200)
        self._timer.timeout.connect(self._load_frame)
        self.slider.valueChanged.connect(self._slider_moved)
        self.resize(960, 680)
        QTimer.singleShot(0, self._load_frame)

    def _slider_moved(self, v: int) -> None:
        self.time_label.setText(fmt_time(v / 10.0, 1))
        self._timer.start()

    def _load_frame(self) -> None:
        self.setCursor(Qt.WaitCursor)
        img = grab_frame(self.media, self.slider.value() / 10.0)
        self.unsetCursor()
        self.canvas.set_image(img)

    def _delete(self) -> None:
        c = self.canvas
        if 0 <= c.selected < len(c.regions):
            del c.regions[c.selected]
            c.selected = -1
            c.update()

    def _clear(self) -> None:
        self.canvas.regions = []
        self.canvas.selected = -1
        self.canvas.update()

    def regions(self) -> List[List[float]]:
        return [[round(v, 4) for v in r] for r in self.canvas.regions]


# ============================================================================
# export
# ============================================================================

ENCODER_NAMES = {
    Encoder.AUTO: "Automatic (GPU if available)",
    Encoder.X264: "H.264 - CPU (x264)",
    Encoder.X265: "H.265/HEVC - CPU (x265)",
    Encoder.NVENC_H264: "H.264 - NVIDIA GPU (NVENC)",
    Encoder.NVENC_HEVC: "H.265/HEVC - NVIDIA GPU (NVENC)",
    Encoder.QSV_H264: "H.264 - Intel GPU (Quick Sync)",
    Encoder.AMF_H264: "H.264 - AMD GPU (AMF)",
    Encoder.VT_H264: "H.264 - Apple VideoToolbox",
    Encoder.VT_HEVC: "H.265/HEVC - Apple VideoToolbox",
}


class EncoderProbe(QThread):
    found = Signal(list)

    def run(self) -> None:
        from ..render.exporter import detect_available_encoders

        try:
            self.found.emit(detect_available_encoders())
        except Exception:
            self.found.emit([Encoder.AUTO])


_encoder_cache: Optional[List[str]] = None
_probe_thread: Optional[EncoderProbe] = None


def _shared_probe() -> EncoderProbe:
    """One application wide probe thread (it outlives the dialog)."""
    global _probe_thread
    if _probe_thread is None:
        _probe_thread = EncoderProbe()

        def done(names: List[str]) -> None:
            global _encoder_cache
            _encoder_cache = names
        _probe_thread.found.connect(done)
        _probe_thread.start()
    return _probe_thread


def stop_background_threads() -> None:
    if _probe_thread is not None and _probe_thread.isRunning():
        _probe_thread.wait(20000)


class ExportDialog(QDialog):
    def __init__(self, media: MediaInfo, settings: ExportSettings, out_duration: float,
                 default_path: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(tr("Export"))
        self.media = media
        self.settings = ExportSettings.from_dict(settings.to_dict())
        lay = QVBoxLayout(self)
        form = QFormLayout()
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(8)
        prow = QHBoxLayout()
        self.path = QLineEdit(default_path)
        browse = QPushButton(tr("Browse..."))
        browse.clicked.connect(self._browse)
        prow.addWidget(self.path, 1)
        prow.addWidget(browse)
        form.addRow(tr("File"), prow)
        self.res = QComboBox()
        for h in RESOLUTION_PRESETS:
            if h == 0:
                self.res.addItem(tr("Same as source ({w}×{h})").format(w=media.width,
                                                                       h=media.height), 0)
            elif h < media.height:
                self.res.addItem(f"{h}p", h)
        self.res.setCurrentIndex(max(0, self.res.findData(self.settings.height)))
        form.addRow(tr("Resolution"), self.res)
        self.fps = QComboBox()
        for f in FPS_PRESETS:
            if f == 0:
                self.fps.addItem(tr("Same as source ({f:g} fps)").format(f=round(media.fps, 3)),
                                 0.0)
            else:
                self.fps.addItem(f"{f:g} fps", f)
        idx = self.fps.findData(self.settings.fps)
        self.fps.setCurrentIndex(max(0, idx))
        form.addRow(tr("Frame rate"), self.fps)
        self.quality = QComboBox()
        for q, text in ((3, tr("Best")), (2, tr("High (recommended)")), (1, tr("Standard")),
                        (0, tr("Small file"))):
            self.quality.addItem(text, q)
        self.quality.setCurrentIndex(max(0, self.quality.findData(self.settings.quality)))
        form.addRow(tr("Quality"), self.quality)
        self.encoder = QComboBox()
        form.addRow(tr("Encoder"), self.encoder)
        self.hw = QCheckBox(tr("Use GPU for decoding (faster, if supported)"))
        self.hw.setChecked(self.settings.hw_decode)
        form.addRow("", self.hw)
        self.audio = QComboBox()
        for kb in (128, 160, 192, 256, 320):
            self.audio.addItem(f"AAC {kb} kbit/s", kb)
        self.audio.setCurrentIndex(max(0, self.audio.findData(self.settings.audio_bitrate)))
        if not media.has_audio:
            self.audio.setEnabled(False)
        form.addRow(tr("Audio"), self.audio)
        lay.addLayout(form)
        summary = QLabel(tr("Output length: {d}  (original {o})").format(
            d=fmt_time(out_duration, 1), o=fmt_time(media.duration, 1)))
        summary.setObjectName("dim")
        lay.addWidget(summary)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText(tr("Export"))
        bb.button(QDialogButtonBox.Ok).setObjectName("primary")
        bb.button(QDialogButtonBox.Cancel).setText(tr("Cancel"))
        bb.accepted.connect(self._accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self.resize(560, 0)
        self._fill_encoders(_encoder_cache or [Encoder.AUTO], probing=_encoder_cache is None)
        if _encoder_cache is None:
            probe = _shared_probe()
            probe.found.connect(self._encoders_found)

    def _fill_encoders(self, names: List[str], probing: bool = False) -> None:
        current = self.encoder.currentData() or self.settings.encoder
        self.encoder.clear()
        for n in names:
            self.encoder.addItem(tr(ENCODER_NAMES.get(n, n)), n)
        if probing:
            self.encoder.addItem(tr("(checking GPU encoders...)"), None)
            self.encoder.model().item(self.encoder.count() - 1).setEnabled(False)
        self.encoder.setCurrentIndex(max(0, self.encoder.findData(current)))

    def _encoders_found(self, names: List[str]) -> None:
        try:
            self._fill_encoders(names)
        except RuntimeError:  # dialog already closed
            pass

    def _browse(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, tr("Export"), self.path.text(),
                                              tr("MP4 video (*.mp4)"))
        if path:
            if not path.lower().endswith(".mp4"):
                path += ".mp4"
            self.path.setText(path)

    def _accept(self) -> None:
        path = self.path.text().strip()
        if not path:
            return
        if not path.lower().endswith(".mp4"):
            path += ".mp4"
            self.path.setText(path)
        from ..render.exporter import same_file

        if same_file(path, self.media.path):
            QMessageBox.warning(self, tr("Export"), tr("The source video cannot be overwritten."))
            return
        if os.path.exists(path):
            r = QMessageBox.question(self, tr("Export"),
                                     tr("The file already exists. Overwrite it?"))
            if r != QMessageBox.Yes:
                return
        self.settings.height = int(self.res.currentData() or 0)
        self.settings.fps = float(self.fps.currentData() or 0.0)
        self.settings.quality = int(self.quality.currentData())
        self.settings.encoder = self.encoder.currentData() or Encoder.AUTO
        self.settings.hw_decode = self.hw.isChecked()
        self.settings.audio_bitrate = int(self.audio.currentData() or 192)
        self.settings.last_dir = os.path.dirname(os.path.abspath(path))
        self.accept()

    def output_path(self) -> str:
        return self.path.text().strip()


def prewarm_encoder_probe() -> None:
    """Detect working encoders in the background at start-up."""
    _shared_probe()


# ============================================================================
# preferences / about
# ============================================================================

class PreferencesDialog(QDialog):
    def __init__(self, qsettings, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.qs = qsettings
        self.setWindowTitle(tr("Preferences"))
        lay = QVBoxLayout(self)
        form = QFormLayout()
        self.lang = QComboBox()
        self.lang.addItem(tr("Automatic (system)"), "auto")
        for code, name in SUPPORTED.items():
            self.lang.addItem(name, code)
        self.lang.setCurrentIndex(max(0, self.lang.findData(qsettings.value("language", "auto"))))
        form.addRow(tr("Language"), self.lang)
        frow = QHBoxLayout()
        self.ffmpeg = QLineEdit(qsettings.value("ffmpeg_path", "") or "")
        self.ffmpeg.setPlaceholderText(tr("Automatic"))
        b = QPushButton(tr("Browse..."))
        b.clicked.connect(self._browse)
        frow.addWidget(self.ffmpeg, 1)
        frow.addWidget(b)
        form.addRow(tr("FFmpeg"), frow)
        try:
            current = ffmpeg.find_ffmpeg()
        except Exception as exc:  # pragma: no cover
            current = str(exc)
        cur = QLabel(tr("In use: {p}").format(p=current))
        cur.setObjectName("dim")
        cur.setWordWrap(True)
        form.addRow("", cur)
        self.hw = QCheckBox(tr("Use GPU video decoding for analysis"))
        self.hw.setChecked(str(qsettings.value("hw_decode", "false")).lower() == "true")
        form.addRow("", self.hw)
        lay.addLayout(form)
        note = QLabel(tr("A language change takes effect after restarting CutSensei."))
        note.setObjectName("dim")
        lay.addWidget(note)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText(tr("OK"))
        bb.button(QDialogButtonBox.Cancel).setText(tr("Cancel"))
        bb.accepted.connect(self._save)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self.resize(520, 0)

    def _browse(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, tr("FFmpeg"), "", "ffmpeg*")
        if path:
            self.ffmpeg.setText(path)

    def _save(self) -> None:
        self.qs.setValue("language", self.lang.currentData())
        path = self.ffmpeg.text().strip()
        if path and not os.path.isfile(path):
            QMessageBox.warning(self, tr("Preferences"), tr("FFmpeg was not found at this path."))
            return
        self.qs.setValue("ffmpeg_path", path)
        ffmpeg.set_ffmpeg_path(path or None)
        self.qs.setValue("hw_decode", "true" if self.hw.isChecked() else "false")
        self.accept()


SHORTCUTS = [
    ("Space", "Play / pause"),
    ("← / →", "Previous / next frame"),
    ("Shift+← / Shift+→", "Back / forward 1 second"),
    ("↑ / ↓", "Previous / next boundary"),
    ("Home / End", "Start / end"),
    ("N / Shift+N", "Next / previous item to check"),
    ("A", "Play around the playhead"),
    ("S", "Split at playhead"),
    ("[ / ]", "Set segment start / end to playhead"),
    ("1 / 2 / 3", "Normal speed / speed up / delete"),
    ("Delete", "Delete and close the gap"),
    ("R", "Restore deleted segment"),
    ("P", "Always keep (protect)"),
    ("C", "Mark as checked"),
    ("V", "Switch preview: edited / original"),
    ("+ / - / 0", "Zoom in / out / fit"),
    ("Ctrl+Z / Ctrl+Shift+Z", "Undo / redo"),
    ("Ctrl+I", "Import video"),
    ("Ctrl+O / Ctrl+S", "Open / save project"),
    ("Ctrl+R", "Auto edit"),
    ("Ctrl+E", "Export"),
]


def show_shortcuts(parent: QWidget) -> None:
    rows = "".join(f"<tr><td style='padding:2px 16px 2px 0'><b>{k}</b></td>"
                   f"<td>{tr(v)}</td></tr>" for k, v in SHORTCUTS)
    box = QMessageBox(parent)
    box.setWindowTitle(tr("Keyboard shortcuts"))
    box.setTextFormat(Qt.RichText)
    box.setText(f"<table>{rows}</table>")
    box.exec()


def show_about(parent: QWidget) -> None:
    try:
        import PySide6

        qt = PySide6.__version__
    except Exception:  # pragma: no cover
        qt = "?"
    try:
        ff = ffmpeg.find_ffmpeg()
        ffv = ".".join(map(str, ffmpeg.ffmpeg_version()))
    except Exception:
        ff, ffv = "-", "-"
    text = (f"<h3>CutSensei {__version__}</h3>"
            f"<p>{tr('Automatic editing for lecture videos: explanations at normal speed, silent board writing sped up, waiting removed.')}</p>"
            f"<p>{tr('License')}: MIT<br>"
            f"Python {sys.version.split()[0]} · Qt/PySide6 {qt} · FFmpeg {ffv}<br>"
            f"<span style='color:{theme.TEXT_DIM}'>{ff}</span></p>"
            f"<p style='color:{theme.TEXT_DIM}'>{tr('Third-party components')}: Qt / PySide6 "
            f"(LGPLv3), FFmpeg (LGPL/GPL), Silero VAD (MIT), ONNX Runtime (MIT), OpenCV "
            f"(Apache 2.0), NumPy (BSD).</p>")
    QMessageBox.about(parent, tr("About CutSensei"), text)
