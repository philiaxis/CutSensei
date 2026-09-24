"""Side panels: media & results (left), properties & settings (right)."""

from __future__ import annotations

import os
from typing import Callable, List, Optional

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor, QImage, QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QButtonGroup, QCheckBox, QComboBox,
                               QDoubleSpinBox, QFormLayout, QFrame, QGridLayout, QHBoxLayout,
                               QHeaderView, QLabel, QPushButton, QScrollArea, QSizePolicy,
                               QSlider, QSpinBox, QTabWidget, QToolButton,
                               QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from ..analysis.thumbnails import thumbnail_path
from ..core.settings import AutoEditSettings, SourceType, SpeedAudio, VadEngine
from ..core.timeline import Action
from . import icons, theme
from .fmt import (action_label, fmt_duration, fmt_time, kind_label, reason_label,
                  source_label)
from .i18n import tr


def _hline() -> QFrame:
    f = QFrame()
    f.setObjectName("hline")
    f.setFrameShape(QFrame.NoFrame)
    return f


def _heading(text: str) -> QLabel:
    lab = QLabel(text)
    lab.setObjectName("heading")
    return lab


class _Debounce:
    def __init__(self, ms: int, func: Callable[[], None]) -> None:
        self.timer = QTimer()
        self.timer.setSingleShot(True)
        self.timer.setInterval(ms)
        self.timer.timeout.connect(func)

    def __call__(self) -> None:
        self.timer.start()


# ============================================================================
# left panel
# ============================================================================

class MediaPanel(QTabWidget):
    seekRequested = Signal(float)
    playRangeRequested = Signal(float, float, str)   # start, end, mode

    def __init__(self, controller, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.ctrl = controller
        self.setDocumentMode(True)
        self._thumbs: Optional[dict] = None
        # ---- media tab
        media = QWidget()
        ml = QVBoxLayout(media)
        ml.setContentsMargins(10, 10, 10, 10)
        ml.setSpacing(8)
        self.poster = QLabel()
        self.poster.setAlignment(Qt.AlignCenter)
        self.poster.setMinimumHeight(90)
        self.poster.setStyleSheet(f"background:{theme.BG0}; border-radius:4px;")
        ml.addWidget(self.poster)
        self.file_label = QLabel()
        self.file_label.setObjectName("heading")
        self.file_label.setWordWrap(True)
        ml.addWidget(self.file_label)
        self.meta_label = QLabel()
        self.meta_label.setObjectName("dim")
        self.meta_label.setWordWrap(True)
        ml.addWidget(self.meta_label)
        ml.addWidget(_hline())
        self.stats = QGridLayout()
        self.stats.setHorizontalSpacing(12)
        self.stats.setVerticalSpacing(6)
        self._stat_labels = {}
        rows = [("source", "Original length"), ("output", "Edited length"),
                ("reduction", "Reduction"), ("kept", "Kept at 1x"),
                ("sped", "Sped up"), ("cut", "Deleted"), ("review", "To check")]
        for r, (key, text) in enumerate(rows):
            name = QLabel(tr(text))
            name.setObjectName("dim")
            val = QLabel("-")
            val.setObjectName("statValue")
            val.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self.stats.addWidget(name, r, 0)
            self.stats.addWidget(val, r, 1)
            self._stat_labels[key] = val
        ml.addLayout(self.stats)
        self.pending_label = QLabel()
        self.pending_label.setWordWrap(True)
        self.pending_label.setStyleSheet(f"color:{theme.SPEED};")
        ml.addWidget(self.pending_label)
        ml.addStretch(1)
        self.addTab(media, tr("Media"))

        # ---- review tab
        review = QWidget()
        rl = QVBoxLayout(review)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(0)
        self.review_tree = self._make_tree([tr("Time"), tr("Length"), tr("Reason")])
        rl.addWidget(self.review_tree, 1)
        rb = QHBoxLayout()
        rb.setContentsMargins(8, 6, 8, 8)
        self.review_play = QPushButton(tr("Play around"))
        self.review_done = QPushButton(tr("Mark as checked"))
        rb.addWidget(self.review_play)
        rb.addWidget(self.review_done)
        rl.addLayout(rb)
        self.addTab(review, tr("Review"))

        # ---- deleted tab
        cuts = QWidget()
        cl = QVBoxLayout(cuts)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(0)
        self.cut_tree = self._make_tree([tr("Time"), tr("Length"), tr("Reason")])
        cl.addWidget(self.cut_tree, 1)
        cb = QHBoxLayout()
        cb.setContentsMargins(8, 6, 8, 8)
        self.cut_play = QPushButton(tr("Play original"))
        self.cut_restore = QPushButton(tr("Restore"))
        cb.addWidget(self.cut_play)
        cb.addWidget(self.cut_restore)
        cl.addLayout(cb)
        self.addTab(cuts, tr("Removed"))

        self.review_tree.itemSelectionChanged.connect(
            lambda: self._tree_selected(self.review_tree))
        self.cut_tree.itemSelectionChanged.connect(lambda: self._tree_selected(self.cut_tree))
        self.review_tree.itemDoubleClicked.connect(lambda *_: self._play_review())
        self.cut_tree.itemDoubleClicked.connect(lambda *_: self._play_cut())
        self.review_play.clicked.connect(self._play_review)
        self.review_done.clicked.connect(self._mark_reviewed)
        self.cut_play.clicked.connect(self._play_cut)
        self.cut_restore.clicked.connect(self._restore_cut)

        self._refresh_later = _Debounce(120, self.refresh)
        controller.timelineChanged.connect(self._refresh_later)
        controller.projectChanged.connect(self.refresh)
        controller.settingsChanged.connect(self._refresh_later)
        controller.analysisChanged.connect(self._refresh_later)
        controller.selectionChanged.connect(self._sync_selection)
        self._updating = False
        self.refresh()

    def _make_tree(self, headers: List[str]) -> QTreeWidget:
        t = QTreeWidget()
        t.setHeaderLabels(headers)
        t.setRootIsDecorated(False)
        t.setUniformRowHeights(True)
        t.setAlternatingRowColors(True)
        t.setSelectionMode(QAbstractItemView.ExtendedSelection)
        t.header().setStretchLastSection(True)
        t.header().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        t.header().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        return t

    def set_thumbnails(self, info: Optional[dict]) -> None:
        self._thumbs = info
        self._update_poster()

    def _update_poster(self) -> None:
        p = self.ctrl.project
        if p is None or not self._thumbs or not self._thumbs.get("count"):
            self.poster.setPixmap(QPixmap())
            self.poster.setText(tr("No preview") if p is None else "")
            return
        idx = min(self._thumbs["count"] - 1, max(0, self._thumbs["count"] // 3))
        path = thumbnail_path(self._thumbs["dir"], idx)
        if os.path.isfile(path):
            img = QImage(path)
            w = max(120, self.width() - 24)
            self.poster.setPixmap(QPixmap.fromImage(img).scaledToWidth(w, Qt.SmoothTransformation))

    def resizeEvent(self, e) -> None:
        super().resizeEvent(e)
        self._update_poster()

    # ------------------------------------------------------------ refresh
    def refresh(self) -> None:
        p = self.ctrl.project
        self._updating = True
        try:
            if p is None:
                self.file_label.setText(tr("No video loaded"))
                self.meta_label.setText(tr("Use \"Import\" or drop a video file here."))
                for lab in self._stat_labels.values():
                    lab.setText("-")
                self.review_tree.clear()
                self.cut_tree.clear()
                self.pending_label.setText("")
                self._update_poster()
                return
            m = p.media
            self.file_label.setText(os.path.basename(m.path))
            parts = [f"{m.width}×{m.height}", f"{m.fps:.2f} fps".replace(".00", ""),
                     fmt_duration(m.duration)]
            if not m.has_audio:
                parts.append(tr("no audio"))
            if not p.media_exists:
                parts.append(tr("source file missing!"))
            if p.analysis is not None:
                parts.append(source_label(p.analysis.source))
            self.meta_label.setText(" · ".join(parts))
            st = p.timeline.stats(p.settings)
            analysed = p.auto_applied
            self._stat_labels["source"].setText(fmt_time(st["source"], 1))
            self._stat_labels["output"].setText(fmt_time(st["output"], 1))
            self._stat_labels["reduction"].setText(f"{st['reduction'] * 100:.1f} %")
            self._stat_labels["kept"].setText(fmt_time(st["kept"], 1))
            self._stat_labels["sped"].setText(
                f"{fmt_time(st['sped_source'], 0)} → {fmt_time(st['sped_output'], 0)}")
            self._stat_labels["cut"].setText(fmt_time(st["cut"], 1))
            open_n = int(st["review_open"])
            self._stat_labels["review"].setText(
                tr("{open} open / {total}").format(open=open_n, total=int(st["review"])))
            if not analysed:
                self.pending_label.setText(tr("Press \"Auto edit\" to analyse the video."))
            elif self.ctrl.settings_pending:
                self.pending_label.setText(
                    tr("Settings changed. Press \"Re-apply\" to regenerate the edit."))
            else:
                self.pending_label.setText("")
            self._fill_trees()
            self.setTabText(1, tr("Review") + (f" ({open_n})" if open_n else ""))
            n_cut = sum(1 for s in p.timeline if s.action == Action.CUT)
            self.setTabText(2, tr("Removed") + (f" ({n_cut})" if n_cut else ""))
            self._update_poster()
        finally:
            self._updating = False
        self._sync_selection()

    def _fill_trees(self) -> None:
        p = self.ctrl.project
        assert p is not None
        self.review_tree.clear()
        self.cut_tree.clear()
        for seg in p.timeline:
            if seg.review:
                it = QTreeWidgetItem([fmt_time(seg.start, 1), f"{seg.duration:.1f}s",
                                      reason_label(seg.reason)])
                it.setData(0, Qt.UserRole, seg.id)
                if seg.reviewed:
                    it.setIcon(0, icons.icon("check", theme.KEEP))
                    for c in range(3):
                        it.setForeground(c, QColor(theme.TEXT_DIM))
                else:
                    it.setIcon(0, icons.icon("flag", theme.REVIEW))
                self.review_tree.addTopLevelItem(it)
            if seg.action == Action.CUT:
                it = QTreeWidgetItem([fmt_time(seg.start, 1), f"{seg.duration:.1f}s",
                                      reason_label(seg.reason) if not seg.manual
                                      else tr("Deleted manually")])
                it.setData(0, Qt.UserRole, seg.id)
                it.setIcon(0, icons.icon("cut", theme.CUT))
                self.cut_tree.addTopLevelItem(it)

    def _sync_selection(self) -> None:
        if self._updating:
            return
        self._updating = True
        try:
            sel = set(self.ctrl.selected_ids())
            for tree in (self.review_tree, self.cut_tree):
                tree.blockSignals(True)
                for i in range(tree.topLevelItemCount()):
                    it = tree.topLevelItem(i)
                    it.setSelected(it.data(0, Qt.UserRole) in sel)
                tree.blockSignals(False)
        finally:
            self._updating = False

    def _tree_selected(self, tree: QTreeWidget) -> None:
        if self._updating:
            return
        ids = [it.data(0, Qt.UserRole) for it in tree.selectedItems()]
        if ids:
            self.ctrl.select(ids)
            tl = self.ctrl.timeline
            i = tl.index_of(ids[0]) if tl else -1
            if i >= 0:
                self.seekRequested.emit(tl[i].start)

    def _selected_segment(self, tree: QTreeWidget):
        items = tree.selectedItems()
        tl = self.ctrl.timeline
        if not items or tl is None:
            return None
        i = tl.index_of(items[0].data(0, Qt.UserRole))
        return tl[i] if i >= 0 else None

    def _play_review(self) -> None:
        seg = self._selected_segment(self.review_tree)
        if seg is not None:
            self.playRangeRequested.emit(max(0.0, seg.start - 2.0), seg.end + 2.0, "edited")

    def _mark_reviewed(self) -> None:
        tl = self.ctrl.timeline
        if tl is None:
            return
        ids = [it.data(0, Qt.UserRole) for it in self.review_tree.selectedItems()]
        idx = [tl.index_of(i) for i in ids if tl.index_of(i) >= 0]
        if idx:
            self.ctrl.set_reviewed(True, idx)

    def _play_cut(self) -> None:
        seg = self._selected_segment(self.cut_tree)
        if seg is not None:
            self.playRangeRequested.emit(seg.start, seg.end, "source")

    def _restore_cut(self) -> None:
        tl = self.ctrl.timeline
        if tl is None:
            return
        ids = [it.data(0, Qt.UserRole) for it in self.cut_tree.selectedItems()]
        idx = [tl.index_of(i) for i in ids if tl.index_of(i) >= 0]
        if idx:
            self.ctrl.restore(idx)

    def show_review_tab(self) -> None:
        self.setCurrentIndex(1)


# ============================================================================
# right panel
# ============================================================================

class AutoSpin(QDoubleSpinBox):
    """Spin box whose minimum value means "automatic" (None)."""

    def __init__(self, lo: float, hi: float, step: float, decimals: int = 1,
                 suffix: str = "") -> None:
        super().__init__()
        self.setRange(lo - step, hi)
        self.setSingleStep(step)
        self.setDecimals(decimals)
        self.setSpecialValueText(tr("Auto"))
        if suffix:
            self.setSuffix(suffix)
        self._auto = lo - step
        self.setKeyboardTracking(False)
        self.setMinimumWidth(84)
        self.setMaximumWidth(120)

    def value_or_none(self) -> Optional[float]:
        v = self.value()
        return None if v <= self._auto + 1e-9 else float(v)

    def set_value_or_none(self, v: Optional[float], placeholder: float) -> None:
        self.blockSignals(True)
        self.setValue(self._auto if v is None else v)
        self.setToolTip(tr("Automatic: {v:.2f}").format(v=placeholder) if v is None else "")
        self.blockSignals(False)


class SegmentProperties(QWidget):
    playSegment = Signal()
    splitRequested = Signal()

    def __init__(self, controller, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.ctrl = controller
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(10)
        self.empty = QLabel(tr("Select a segment on the timeline to edit it."))
        self.empty.setObjectName("dim")
        self.empty.setWordWrap(True)
        lay.addWidget(self.empty)

        self.body = QWidget()
        bl = QVBoxLayout(self.body)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.setSpacing(10)
        head = QHBoxLayout()
        self.kind_chip = QLabel()
        self.kind_chip.setObjectName("chip")
        head.addWidget(self.kind_chip)
        self.title = QLabel()
        self.title.setObjectName("heading")
        head.addWidget(self.title, 1)
        bl.addLayout(head)
        self.range_label = QLabel()
        self.range_label.setFont(theme.mono_font(9.5))
        bl.addWidget(self.range_label)
        self.reason_label = QLabel()
        self.reason_label.setObjectName("dim")
        self.reason_label.setWordWrap(True)
        bl.addWidget(self.reason_label)
        bl.addWidget(_hline())

        bl.addWidget(_heading(tr("Processing")))
        seg_row = QHBoxLayout()
        seg_row.setSpacing(0)
        self.act_group = QButtonGroup(self)
        self.act_group.setExclusive(True)
        self.act_buttons = {}
        short = {Action.KEEP: tr("Normal"), Action.SPEED: tr("Speed up"),
                 Action.CUT: tr("Delete")}
        for act, icon_name in ((Action.KEEP, "keep"), (Action.SPEED, "speed"),
                               (Action.CUT, "cut")):
            b = QPushButton(short[act])
            b.setToolTip(action_label(act))
            b.setObjectName("segment")
            b.setCheckable(True)
            b.setIcon(icons.icon(icon_name))
            b.setFocusPolicy(Qt.NoFocus)
            b.setMinimumWidth(0)
            b.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
            self.act_group.addButton(b)
            self.act_buttons[act] = b
            seg_row.addWidget(b)
        bl.addLayout(seg_row)

        form = QFormLayout()
        form.setRowWrapPolicy(QFormLayout.WrapLongRows)
        form.setLabelAlignment(Qt.AlignLeft)
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(8)
        sp_row = QHBoxLayout()
        self.speed = QDoubleSpinBox()
        self.speed.setRange(1.0, 32.0)
        self.speed.setSingleStep(0.25)
        self.speed.setDecimals(2)
        self.speed.setSuffix(" ×")
        self.speed.setKeyboardTracking(False)
        sp_row.addWidget(self.speed, 1)
        self.speed_presets = []
        for v in (2, 4, 8):
            b = QToolButton()
            b.setText(f"{v}×")
            b.setMinimumWidth(0)
            b.setFocusPolicy(Qt.NoFocus)
            b.clicked.connect(lambda _=False, v=v: self._set_speed(float(v)))
            sp_row.addWidget(b)
            self.speed_presets.append(b)
        form.addRow(tr("Speed"), sp_row)
        vol_row = QHBoxLayout()
        self.volume = QSlider(Qt.Horizontal)
        self.volume.setRange(0, 200)
        self.volume.setFocusPolicy(Qt.StrongFocus)
        self.volume_label = QLabel("100 %")
        self.volume_label.setMinimumWidth(44)
        self.volume_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        vol_row.addWidget(self.volume, 1)
        vol_row.addWidget(self.volume_label)
        form.addRow(tr("Volume"), vol_row)
        bl.addLayout(form)
        self.vol_default = QPushButton(tr("Default volume"))
        self.vol_default.setFocusPolicy(Qt.NoFocus)
        bl.addWidget(self.vol_default, 0, Qt.AlignLeft)

        bl.addWidget(_hline())
        self.protect = QCheckBox(tr("Always keep this segment (protect)"))
        self.protect.setToolTip(tr("Protected segments are never deleted and never changed "
                                   "by the automatic edit."))
        bl.addWidget(self.protect)
        self.reviewed = QCheckBox(tr("Checked"))
        bl.addWidget(self.reviewed)
        self.lock_label = QLabel()
        self.lock_label.setObjectName("dim")
        self.lock_label.setWordWrap(True)
        bl.addWidget(self.lock_label)
        btns = QVBoxLayout()
        self.play_btn = QPushButton(icons.icon("play"), tr("Play segment"))
        self.unlock_btn = QPushButton(icons.icon("unlock"), tr("Revert to automatic"))
        self.unlock_btn.setToolTip(tr("Release manual changes and protection; the automatic "
                                      "edit decides this part again."))
        btns.addWidget(self.play_btn)
        btns.addWidget(self.unlock_btn)
        bl.addLayout(btns)
        bl.addStretch(1)
        lay.addWidget(self.body)
        lay.addStretch(1)

        for act, b in self.act_buttons.items():
            b.clicked.connect(lambda _=False, a=act: self._set_action(a))
        self.speed.valueChanged.connect(self._set_speed)
        self.volume.valueChanged.connect(self._volume_moved)
        self.vol_default.clicked.connect(lambda: self.ctrl.set_volume(None))
        self.protect.toggled.connect(lambda on: self._guard(lambda: self.ctrl.set_protected(on)))
        self.reviewed.toggled.connect(lambda on: self._guard(lambda: self.ctrl.set_reviewed(on)))
        self.play_btn.clicked.connect(self.playSegment)
        self.unlock_btn.clicked.connect(lambda: self.ctrl.unlock())
        self._loading = False
        controller.selectionChanged.connect(self.refresh)
        controller.timelineChanged.connect(self.refresh)
        controller.settingsChanged.connect(self.refresh)
        self.refresh()

    def _guard(self, fn: Callable[[], None]) -> None:
        if not self._loading:
            fn()

    def _set_action(self, action: str) -> None:
        if self._loading:
            return
        if action == Action.SPEED:
            segs = self.ctrl.selected_segments()
            speed = next((s.speed for s in segs if s.speed), None) or \
                self.ctrl.settings.writing_speed
            self.ctrl.set_action(Action.SPEED, speed=speed)
        else:
            self.ctrl.set_action(action)

    def _set_speed(self, v: float) -> None:
        if not self._loading:
            self.ctrl.set_speed(float(v))

    def _volume_moved(self, v: int) -> None:
        self.volume_label.setText(f"{v} %")
        if not self._loading:
            self.ctrl.set_volume(v / 100.0, merge_key="volume-drag")

    def refresh(self) -> None:
        segs = self.ctrl.selected_segments()
        self.empty.setVisible(not segs)
        self.body.setVisible(bool(segs))
        if not segs:
            return
        self._loading = True
        try:
            st = self.ctrl.settings
            first = segs[0]
            tl = self.ctrl.timeline
            if len(segs) == 1:
                idx = tl.index_of(first.id)
                self.title.setText(tr("Segment {i} of {n}").format(i=idx + 1, n=len(tl)))
                text = f"{fmt_time(first.start, 2)} – {fmt_time(first.end, 2)}"
                text += "\n" + tr("{d:.2f} s").format(d=first.duration)
                if first.action != Action.CUT:
                    text += "  →  " + tr("{d:.2f} s").format(d=first.output_duration(st))
                else:
                    text += "  →  " + tr("removed")
                self.range_label.setText(text)
                self.reason_label.setText(reason_label(first.reason) +
                                          (f" · {tr('confidence')} {first.confidence:.0%}"
                                           if first.reason not in ("", "unanalyzed") else ""))
            else:
                total = sum(s.duration for s in segs)
                self.title.setText(tr("{n} segments selected").format(n=len(segs)))
                self.range_label.setText(f"{fmt_time(segs[0].start, 2)} – "
                                         f"{fmt_time(segs[-1].end, 2)}\n"
                                         + tr("{d:.2f} s").format(d=total))
                self.reason_label.setText("")
            kinds = {s.kind for s in segs}
            kind = first.kind if len(kinds) == 1 else None
            self.kind_chip.setText(kind_label(kind) if kind else tr("Mixed"))
            col = theme.KIND_COLORS.get(kind or "", theme.TEXT_DIM)
            self.kind_chip.setStyleSheet(f"background:{col};")
            actions = {s.action for s in segs}
            self.act_group.setExclusive(False)
            for act, b in self.act_buttons.items():
                b.setChecked(len(actions) == 1 and act in actions)
            self.act_group.setExclusive(True)
            any_protected = any(s.protected for s in segs)
            self.act_buttons[Action.CUT].setEnabled(not any_protected)
            is_cut = all(s.action == Action.CUT for s in segs)
            speed = first.speed_value(st) if first.action != Action.CUT else st.writing_speed
            self.speed.setValue(1.0 if speed == float("inf") else speed)
            self.speed.setEnabled(not is_cut)
            for b in self.speed_presets:
                b.setEnabled(not is_cut)
            vol = first.volume_value(st)
            self.volume.setValue(int(round(vol * 100)))
            self.volume_label.setText(f"{int(round(vol * 100))} %")
            self.volume.setEnabled(not is_cut)
            self.vol_default.setEnabled(any(s.volume is not None for s in segs))
            self.protect.setChecked(all(s.protected for s in segs))
            reviewable = [s for s in segs if s.review]
            self.reviewed.setVisible(bool(reviewable))
            self.reviewed.setChecked(bool(reviewable) and all(s.reviewed for s in reviewable))
            locked = [s for s in segs if s.locked]
            self.unlock_btn.setEnabled(bool(locked))
            self.lock_label.setText(tr("Edited manually - the automatic edit will not change "
                                       "this segment.") if locked else "")
        finally:
            self._loading = False


class AutoEditPanel(QScrollArea):
    applyRequested = Signal()

    def __init__(self, controller, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.ctrl = controller
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        inner = QWidget()
        lay = QVBoxLayout(inner)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(10)

        lay.addWidget(_heading(tr("Recording type")))
        self.source = QComboBox()
        self.source.addItem(tr("Automatic"), SourceType.AUTO)
        self.source.addItem(tr("Camera (blackboard, whiteboard)"), SourceType.CAMERA)
        self.source.addItem(tr("Screen recording (digital notes)"), SourceType.SCREEN)
        self.source.setToolTip(tr(
            "Camera: a classroom recording of a blackboard, whiteboard or electronic board.\n"
            "Screen recording: GoodNotes, Notability, OneNote, a whiteboard app or slides "
            "written on with a pen, recorded on a tablet or PC."))
        lay.addWidget(self.source)
        self.source_hint = QLabel()
        self.source_hint.setObjectName("dim")
        self.source_hint.setWordWrap(True)
        lay.addWidget(self.source_hint)

        lay.addWidget(_heading(tr("Board writing speed")))
        row = QHBoxLayout()
        self.speed_slider = QSlider(Qt.Horizontal)
        self.speed_slider.setRange(4, 64)   # quarter steps: 1.0x .. 16x
        self.speed = QDoubleSpinBox()
        self.speed.setRange(1.0, 16.0)
        self.speed.setSingleStep(0.5)
        self.speed.setDecimals(1)
        self.speed.setSuffix(" ×")
        self.speed.setKeyboardTracking(False)
        row.addWidget(self.speed_slider, 1)
        row.addWidget(self.speed)
        lay.addLayout(row)

        lay.addWidget(_heading(tr("Cut aggressiveness")))
        self.aggr = QSlider(Qt.Horizontal)
        self.aggr.setRange(0, 100)
        self.aggr.setPageStep(10)
        lay.addWidget(self.aggr)
        scale = QHBoxLayout()
        for text, align in ((tr("Conservative"), Qt.AlignLeft), (tr("Standard"), Qt.AlignCenter),
                            (tr("Aggressive"), Qt.AlignRight)):
            lab = QLabel(text)
            lab.setObjectName("faint")
            lab.setAlignment(align)
            scale.addWidget(lab, 1)
        lay.addLayout(scale)
        self.aggr_hint = QLabel()
        self.aggr_hint.setObjectName("dim")
        self.aggr_hint.setWordWrap(True)
        lay.addWidget(self.aggr_hint)

        lay.addWidget(_heading(tr("Margin around speech")))
        pad = QFormLayout()
        pad.setRowWrapPolicy(QFormLayout.WrapLongRows)
        pad.setHorizontalSpacing(10)
        self.pad_before = QDoubleSpinBox()
        self.pad_after = QDoubleSpinBox()
        for sb in (self.pad_before, self.pad_after):
            sb.setRange(0.0, 3.0)
            sb.setSingleStep(0.05)
            sb.setDecimals(2)
            sb.setSuffix(tr(" s"))
            sb.setKeyboardTracking(False)
        pad.addRow(tr("Before speech"), self.pad_before)
        pad.addRow(tr("After speech"), self.pad_after)
        lay.addLayout(pad)

        lay.addWidget(_heading(tr("Audio of sped-up parts")))
        arow = QHBoxLayout()
        self.speed_audio = QComboBox()
        self.speed_audio.addItem(tr("Keep"), SpeedAudio.KEEP)
        self.speed_audio.addItem(tr("Lower volume"), SpeedAudio.VOLUME)
        self.speed_audio.addItem(tr("Mute"), SpeedAudio.MUTE)
        self.speed_audio_vol = QSpinBox()
        self.speed_audio_vol.setRange(0, 200)
        self.speed_audio_vol.setSuffix(" %")
        self.speed_audio_vol.setKeyboardTracking(False)
        arow.addWidget(self.speed_audio, 1)
        arow.addWidget(self.speed_audio_vol)
        lay.addLayout(arow)

        # advanced settings (collapsible)
        self.adv_toggle = QToolButton()
        self.adv_toggle.setText(tr("Advanced settings"))
        self.adv_toggle.setCheckable(True)
        self.adv_toggle.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.adv_toggle.setArrowType(Qt.RightArrow)
        self.adv_toggle.setFocusPolicy(Qt.NoFocus)
        lay.addWidget(self.adv_toggle)
        self.adv = QWidget()
        adv = QFormLayout(self.adv)
        adv.setRowWrapPolicy(QFormLayout.WrapLongRows)
        adv.setContentsMargins(4, 0, 0, 0)
        adv.setHorizontalSpacing(10)
        adv.setVerticalSpacing(6)
        s_ = tr(" s")
        self.min_cut = AutoSpin(0.3, 60.0, 0.1, 1, s_)
        self.min_speed = AutoSpin(0.3, 60.0, 0.1, 1, s_)
        self.speech_gap = AutoSpin(0.0, 5.0, 0.1, 1, s_)
        self.writing_gap = AutoSpin(0.0, 10.0, 0.1, 1, s_)
        self.hold = AutoSpin(0.0, 10.0, 0.1, 1, s_)
        self.cut_margin = AutoSpin(0.0, 3.0, 0.05, 2, s_)
        self.speech_th = AutoSpin(0.05, 0.95, 0.05, 2)
        self.writing_th = AutoSpin(0.05, 0.95, 0.05, 2)
        self.review_band = AutoSpin(0.0, 0.5, 0.01, 2)
        adv.addRow(tr("Shortest waiting to delete"), self.min_cut)
        adv.addRow(tr("Shortest writing to speed up"), self.min_speed)
        adv.addRow(tr("Join pauses in speech up to"), self.speech_gap)
        adv.addRow(tr("Join pauses in writing up to"), self.writing_gap)
        adv.addRow(tr("Show finished board for"), self.hold)
        adv.addRow(tr("Margin next to cuts"), self.cut_margin)
        adv.addRow(tr("Speech threshold"), self.speech_th)
        adv.addRow(tr("Writing threshold"), self.writing_th)
        adv.addRow(tr("Uncertainty band"), self.review_band)
        self.motion_unc = QCheckBox(tr("Silent movement is marked for checking"))
        adv.addRow(self.motion_unc)
        self.vad = QComboBox()
        self.vad.addItem(tr("Automatic"), VadEngine.AUTO)
        self.vad.addItem(tr("Neural (Silero VAD)"), VadEngine.SILERO)
        self.vad.addItem(tr("Signal processing"), VadEngine.DSP)
        adv.addRow(tr("Speech detector"), self.vad)
        self.adv.setVisible(False)
        lay.addWidget(self.adv)
        self.adv_toggle.toggled.connect(self._toggle_adv)

        lay.addWidget(_hline())
        self.note = QLabel(tr("Segments you edited or protected are not overwritten when the "
                              "edit is regenerated."))
        self.note.setObjectName("dim")
        self.note.setWordWrap(True)
        lay.addWidget(self.note)
        brow = QHBoxLayout()
        self.apply_btn = QPushButton(icons.icon("auto", "#ffffff"), tr("Re-apply auto edit"))
        self.apply_btn.setObjectName("primary")
        self.reset_btn = QPushButton(tr("Defaults"))
        brow.addWidget(self.apply_btn, 1)
        brow.addWidget(self.reset_btn)
        lay.addLayout(brow)
        lay.addStretch(1)
        self.setWidget(inner)

        self._loading = False
        self.speed_slider.valueChanged.connect(
            lambda v: self._loading or self.speed.setValue(v / 4.0))
        self.speed.valueChanged.connect(self._speed_changed)
        self.aggr.valueChanged.connect(self._aggr_moved)
        self.aggr.sliderReleased.connect(self._aggr_commit)
        self.pad_before.valueChanged.connect(lambda v: self._set(pad_before=float(v)))
        self.pad_after.valueChanged.connect(lambda v: self._set(pad_after=float(v)))
        self.speed_audio.currentIndexChanged.connect(self._audio_changed)
        self.source.currentIndexChanged.connect(
            lambda _i: self._set(source_type=self.source.currentData()))
        self.speed_audio_vol.valueChanged.connect(
            lambda v: self._set(speed_audio_volume=v / 100.0))
        for name, w in (("min_cut", self.min_cut), ("min_speed", self.min_speed),
                        ("speech_gap_fill", self.speech_gap),
                        ("writing_gap_fill", self.writing_gap),
                        ("hold_after_writing", self.hold), ("cut_margin", self.cut_margin),
                        ("speech_threshold", self.speech_th),
                        ("writing_threshold", self.writing_th),
                        ("review_band", self.review_band)):
            w.valueChanged.connect(lambda _v, n=name, w=w: self._set(**{n: w.value_or_none()}))
        self.motion_unc.toggled.connect(lambda on: self._set(motion_is_uncertain=bool(on)))
        self.vad.currentIndexChanged.connect(
            lambda _i: self._set(vad_engine=self.vad.currentData()))
        self.apply_btn.clicked.connect(self.applyRequested)
        self.reset_btn.clicked.connect(self._reset)
        controller.settingsChanged.connect(self.refresh)
        controller.projectChanged.connect(self.refresh)
        controller.analysisChanged.connect(self.refresh)
        self.refresh()

    def _toggle_adv(self, on: bool) -> None:
        self.adv.setVisible(on)
        self.adv_toggle.setArrowType(Qt.DownArrow if on else Qt.RightArrow)

    def _set(self, **changes) -> None:
        if self._loading or not self.ctrl.has_project:
            return
        self.ctrl.update_settings(**changes)

    def _speed_changed(self, v: float) -> None:
        if self._loading:
            return
        self._loading = True
        self.speed_slider.setValue(int(round(v * 4)))
        self._loading = False
        self._set(writing_speed=float(v))

    def _aggr_moved(self, v: int) -> None:
        self._update_aggr_hint(v)
        if not self.aggr.isSliderDown():
            self._aggr_commit()

    def _aggr_commit(self) -> None:
        self._set(aggressiveness=int(self.aggr.value()))

    def _update_aggr_hint(self, v: int) -> None:
        st = AutoEditSettings(aggressiveness=v)
        self.aggr_hint.setText(tr("Waiting longer than {c:.1f} s is deleted; writing longer "
                                  "than {w:.1f} s is sped up.").format(c=st.eff_min_cut,
                                                                        w=st.eff_min_speed))

    def _audio_changed(self, _i: int) -> None:
        mode = self.speed_audio.currentData()
        self.speed_audio_vol.setEnabled(mode == SpeedAudio.VOLUME)
        self._set(speed_audio=mode)

    def _reset(self) -> None:
        if self.ctrl.has_project:
            self.ctrl.replace_settings(AutoEditSettings())

    def _source_hint(self) -> str:
        p = self.ctrl.project
        if p is None:
            return ""
        a = p.analysis
        chosen = p.settings.source_type
        if a is None:
            return tr("Detected when the video is analysed.") if chosen == SourceType.AUTO else ""
        lines = []
        if chosen == SourceType.AUTO or a.source_detected != chosen:
            lines.append(tr("Detected: {kind}").format(kind=source_label(a.source_detected)))
        if not self.ctrl.analysis_is_current():
            lines.append(tr("Press \"Re-apply auto edit\" to analyse the video again."))
        elif a.source == SourceType.SCREEN and a.live_rects:
            lines.append(tr("A camera picture in the recording is ignored."))
        return "\n".join(lines)

    def refresh(self) -> None:
        st = self.ctrl.settings
        self._loading = True
        try:
            enabled = self.ctrl.has_project
            self.setEnabled(enabled)
            self.speed.setValue(st.writing_speed)
            self.speed_slider.setValue(int(round(st.writing_speed * 4)))
            self.aggr.setValue(int(st.aggressiveness))
            self._update_aggr_hint(int(st.aggressiveness))
            self.pad_before.setValue(st.pad_before)
            self.pad_after.setValue(st.pad_after)
            self.speed_audio.setCurrentIndex(max(0, self.speed_audio.findData(st.speed_audio)))
            self.speed_audio_vol.setValue(int(round(st.speed_audio_volume * 100)))
            self.speed_audio_vol.setEnabled(st.speed_audio == SpeedAudio.VOLUME)
            self.min_cut.set_value_or_none(st.min_cut, st.eff_min_cut)
            self.min_speed.set_value_or_none(st.min_speed, st.eff_min_speed)
            self.speech_gap.set_value_or_none(st.speech_gap_fill, st.eff_speech_gap_fill)
            self.writing_gap.set_value_or_none(st.writing_gap_fill, st.eff_writing_gap_fill)
            self.hold.set_value_or_none(st.hold_after_writing, st.eff_hold_after_writing)
            self.cut_margin.set_value_or_none(st.cut_margin, st.eff_cut_margin)
            self.speech_th.set_value_or_none(st.speech_threshold, st.eff_speech_threshold)
            self.writing_th.set_value_or_none(st.writing_threshold, st.eff_writing_threshold)
            self.review_band.set_value_or_none(st.review_band, st.eff_review_band)
            self.motion_unc.setChecked(st.motion_is_uncertain)
            self.vad.setCurrentIndex(max(0, self.vad.findData(st.vad_engine)))
            self.source.setCurrentIndex(max(0, self.source.findData(st.source_type)))
            self.source_hint.setText(self._source_hint())
            self.source_hint.setVisible(bool(self.source_hint.text()))
            p = self.ctrl.project
            analysed = bool(p and p.auto_applied)
            self.apply_btn.setText(tr("Re-apply auto edit") if analysed else tr("Run auto edit"))
            self.apply_btn.setObjectName("primary" if (not analysed or self.ctrl.settings_pending)
                                         else "")
            self.apply_btn.style().unpolish(self.apply_btn)
            self.apply_btn.style().polish(self.apply_btn)
        finally:
            self._loading = False


class PropertiesPanel(QTabWidget):
    def __init__(self, controller, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setDocumentMode(True)
        self.ctrl = controller
        self.segment = SegmentProperties(controller)
        seg_scroll = QScrollArea()
        seg_scroll.setWidgetResizable(True)
        seg_scroll.setFrameShape(QFrame.NoFrame)
        seg_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        seg_scroll.setWidget(self.segment)
        self.auto = AutoEditPanel(controller)
        self.addTab(seg_scroll, tr("Segment"))
        self.addTab(self.auto, tr("Auto edit"))
        self.setCurrentIndex(1)
        controller.selectionChanged.connect(self._on_selection)

    def _on_selection(self) -> None:
        if self.ctrl.selected_ids():
            self.setCurrentIndex(0)
