"""The main window: tool bar, panels, preview and timeline."""

from __future__ import annotations

import json
import os
from typing import List, Optional

from PySide6.QtCore import QByteArray, QSize, Qt, QTimer, QUrl
from PySide6.QtGui import QAction, QDesktopServices, QKeySequence
from PySide6.QtWidgets import (QApplication, QFileDialog, QLabel, QMainWindow, QMenu,
                               QMessageBox, QSizePolicy, QSplitter, QToolBar, QToolButton,
                               QVBoxLayout, QWidget)

from .. import APP_NAME, __version__
from ..analysis.pipeline import analyze
from ..analysis.result import regions_signature
from ..analysis.thumbnails import generate_thumbnails, thumbnail_info
from ..core.errors import CutSenseiError
from ..core.media import VIDEO_EXTENSIONS, probe
from ..core.paths import autosave_path
from ..core.project import PROJECT_EXTENSION, Project
from ..core.settings import AutoEditSettings, VadEngine
from ..core.timeline import Action
from ..render.exporter import export_video
from . import icons, theme
from .controller import ProjectController
from .dialogs import (BoardRegionDialog, ExportDialog, PreferencesDialog, show_about,
                      show_shortcuts)
from .fmt import fmt_duration, fmt_time
from .i18n import tr
from .panels import MediaPanel, PropertiesPanel
from .settings_store import app_settings
from .preview import EDITED, SOURCE, PreviewEngine, TransportBar, VideoView, badge_for
from .timeline_view import TimelinePanel
from .workers import JobThread, ProgressRunner

VIDEO_FILTER = "*" + " *".join(VIDEO_EXTENSIONS)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.qs = app_settings()
        self.ctrl = ProjectController(self)
        self.engine = PreviewEngine(self)
        self._thumb_job: Optional[JobThread] = None
        self._busy = False
        self.setWindowIcon(icons.app_icon())
        self.setAcceptDrops(True)
        self.setMinimumSize(980, 640)
        self._build_actions()
        self._build_ui()
        self._build_menus()
        self._connect()
        self._restore_window()
        self._update_title()
        self._update_actions()
        app = QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self._stop_threads)
        self._autosave_timer = QTimer(self)
        self._autosave_timer.setInterval(90_000)
        self._autosave_timer.timeout.connect(self._autosave)
        self._autosave_timer.start()

    # ================================================================== UI
    def _build_actions(self) -> None:
        def act(text: str, icon: Optional[str] = None, shortcut=None, slot=None,
                tip: Optional[str] = None, checkable: bool = False) -> QAction:
            a = QAction(tr(text), self)
            if icon:
                a.setIcon(icons.icon(icon))
            if shortcut:
                if isinstance(shortcut, (list, tuple)):
                    a.setShortcuts([QKeySequence(s) for s in shortcut])
                else:
                    a.setShortcut(QKeySequence(shortcut))
            if tip:
                a.setToolTip(tip)
            elif shortcut:
                sc = shortcut[0] if isinstance(shortcut, (list, tuple)) else shortcut
                a.setToolTip(f"{tr(text)} ({QKeySequence(sc).toString(QKeySequence.NativeText)})")
            a.setCheckable(checkable)
            if slot:
                a.triggered.connect(slot)
            self.addAction(a)
            return a

        self.a_import = act("Import video", "import", "Ctrl+I", self.import_video)
        self.a_open = act("Open project", "open", QKeySequence.Open, self.open_project)
        self.a_save = act("Save", "save", QKeySequence.Save, self.save_project)
        self.a_save_as = act("Save as...", None, QKeySequence.SaveAs, self.save_project_as)
        self.a_close = act("Close project", None, None, self.close_project)
        self.a_quit = act("Quit", None, QKeySequence.Quit, self.close)
        self.a_undo = act("Undo", "undo", QKeySequence.Undo, self.ctrl.undo)
        self.a_redo = act("Redo", "redo", ["Ctrl+Shift+Z", "Ctrl+Y"], self.ctrl.redo)
        self.a_board = act("Board region", "board", None, self.edit_board_region,
                           tip=tr("Mark the blackboard / whiteboard area (optional)"))
        self.a_auto = act("Auto edit", "auto", "Ctrl+R", self.run_auto_edit,
                          tip=tr("Analyse the video and create the edit (Ctrl+R)"))
        self.a_export = act("Export", "export", "Ctrl+E", self.export)
        self.a_prefs = act("Preferences...", "settings", QKeySequence.Preferences,
                           self.show_preferences)
        # playback
        self.a_play = act("Play / pause", "play", "Space", self.engine.toggle)
        self.a_frame_back = act("Previous frame", "frame_back", "Left",
                                lambda: self.engine.step(-1))
        self.a_frame_fwd = act("Next frame", "frame_fwd", "Right", lambda: self.engine.step(1))
        self.a_back_1s = act("Back 1 second", None, "Shift+Left", lambda: self._jump_seconds(-1))
        self.a_fwd_1s = act("Forward 1 second", None, "Shift+Right", lambda: self._jump_seconds(1))
        self.a_prev_mark = act("Previous boundary", "prev_mark", "Up", lambda: self.goto_boundary(-1))
        self.a_next_mark = act("Next boundary", "next_mark", "Down", lambda: self.goto_boundary(1))
        self.a_home = act("Go to start", None, "Home", lambda: self.engine.seek(0.0))
        self.a_end = act("Go to end", None, "End", self._goto_end)
        self.a_prev_review = act("Previous item to check", "prev_review", "Shift+N",
                                 lambda: self.goto_review(-1))
        self.a_next_review = act("Next item to check", "next_review", "N",
                                 lambda: self.goto_review(1))
        self.a_around = act("Play around the playhead", "loop", "A", self.play_around)
        self.a_toggle_mode = act("Switch edited / original", None, "Tab", self._toggle_mode)
        self.a_auto_around = act("Play around automatically after jumping", None, None,
                                 self._auto_around_toggled, checkable=True)
        # editing
        self.a_split = act("Split at playhead", "split", "S", self.split_at_playhead)
        self.a_keep = act("Normal speed", "keep", "1", lambda: self.ctrl.set_action(Action.KEEP))
        self.a_speed = act("Speed up", "speed", "2", self._set_speed_action)
        self.a_delete = act("Delete", "delete", ["Delete", "Backspace", "3"],
                            lambda: self.ctrl.set_action(Action.CUT))
        self.a_restore = act("Restore", "restore", "R", lambda: self.ctrl.restore())
        self.a_protect = act("Always keep (protect)", "protect", "P", self._toggle_protect)
        self.a_set_start = act("Set segment start to playhead", None, "[",
                               lambda: self._set_edge_to_playhead(start=True))
        self.a_set_end = act("Set segment end to playhead", None, "]",
                             lambda: self._set_edge_to_playhead(start=False))
        self.a_unlock = act("Revert to automatic", "unlock", None, lambda: self.ctrl.unlock())
        self.a_unlock_all = act("Release all manual edits...", None, None, self._unlock_all)
        self.a_select_all = act("Select all segments", None, QKeySequence.SelectAll,
                                self._select_all)
        self.a_mark_checked = act("Mark as checked", "check", "C",
                                  lambda: self.ctrl.set_reviewed(True))
        # view
        self.a_zoom_in = act("Zoom in", "zoom_in", ["+", "="], lambda: self.timeline.view.zoom(1.5))
        self.a_zoom_out = act("Zoom out", "zoom_out", "-",
                              lambda: self.timeline.view.zoom(1 / 1.5))
        self.a_zoom_fit = act("Fit timeline", "fit", "0", lambda: self.timeline.view.zoom_to_fit())
        self.a_left = act("Media panel", "panel_left", None, self._toggle_left, checkable=True)
        self.a_right = act("Properties panel", "panel_right", None, self._toggle_right,
                           checkable=True)
        self.a_shortcuts = act("Keyboard shortcuts", None, "F1", lambda: show_shortcuts(self))
        self.a_about = act("About CutSensei", "info", None, lambda: show_about(self))

    def _build_ui(self) -> None:
        tb = QToolBar(tr("Main"))
        tb.setMovable(False)
        tb.setIconSize(QSize(20, 20))
        tb.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.addToolBar(tb)
        self.project_label = QLabel()
        self.project_label.setObjectName("heading")
        self.project_label.setContentsMargins(4, 0, 12, 0)
        tb.addWidget(self.project_label)
        tb.addSeparator()
        for a in (self.a_import, self.a_open, self.a_save):
            tb.addAction(a)
        tb.addSeparator()
        for a in (self.a_undo, self.a_redo):
            tb.addAction(a)
            tb.widgetForAction(a).setToolButtonStyle(Qt.ToolButtonIconOnly)
        tb.addSeparator()
        tb.addAction(self.a_board)
        tb.addAction(self.a_auto)
        tb.addAction(self.a_export)
        for a, name in ((self.a_auto, "auto"), (self.a_export, "export")):
            w = tb.widgetForAction(a)
            if isinstance(w, QToolButton):
                w.setObjectName("primary")
                a.setIcon(icons.icon(name, "#ffffff"))
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        tb.addWidget(spacer)
        for a in (self.a_left, self.a_right):
            tb.addAction(a)
            tb.widgetForAction(a).setToolButtonStyle(Qt.ToolButtonIconOnly)
        for w in tb.findChildren(QToolButton):
            w.setFocusPolicy(Qt.NoFocus)

        # panels
        self.media_panel = MediaPanel(self.ctrl)
        self.props = PropertiesPanel(self.ctrl)
        center = QWidget()
        cl = QVBoxLayout(center)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(0)
        self.video = VideoView()
        self.video.set_hint(tr("1. Import a lecture video (Ctrl+I) or drop it here\n"
                               "2. Optionally mark the board region\n"
                               "3. Press \"Auto edit\"\n"
                               "4. Check and correct on the timeline\n"
                               "5. Export"))
        cl.addWidget(self.video, 1)
        self.transport = TransportBar()
        cl.addWidget(self.transport)
        self.timeline = TimelinePanel(self.ctrl)

        self.media_panel.setMinimumWidth(260)
        self.props.setMinimumWidth(330)
        center.setMinimumWidth(520)
        self.hsplit = QSplitter(Qt.Horizontal)
        self.hsplit.addWidget(self.media_panel)
        self.hsplit.addWidget(center)
        self.hsplit.addWidget(self.props)
        self.hsplit.setStretchFactor(0, 0)
        self.hsplit.setStretchFactor(1, 1)
        self.hsplit.setStretchFactor(2, 0)
        self.hsplit.setCollapsible(1, False)
        self.hsplit.setSizes([270, 900, 340])
        self.vsplit = QSplitter(Qt.Vertical)
        self.vsplit.addWidget(self.hsplit)
        self.vsplit.addWidget(self.timeline)
        self.vsplit.setStretchFactor(0, 1)
        self.vsplit.setStretchFactor(1, 0)
        self.vsplit.setCollapsible(0, False)
        self.vsplit.setCollapsible(1, False)
        self.vsplit.setSizes([560, 250])
        self.setCentralWidget(self.vsplit)

        sb = self.statusBar()
        self.status_summary = QLabel()
        self.gpu_label = QLabel()
        sb.addPermanentWidget(self.status_summary)
        sb.addPermanentWidget(self.gpu_label)
        self.a_left.setChecked(True)
        self.a_right.setChecked(True)

    def _build_menus(self) -> None:
        mb = self.menuBar()
        m = mb.addMenu(tr("&File"))
        for a in (self.a_import, self.a_open):
            m.addAction(a)
        self.recent_menu = m.addMenu(tr("Recent"))
        m.addSeparator()
        for a in (self.a_save, self.a_save_as, self.a_close):
            m.addAction(a)
        m.addSeparator()
        m.addAction(self.a_export)
        m.addSeparator()
        m.addAction(self.a_prefs)
        m.addSeparator()
        m.addAction(self.a_quit)
        m = mb.addMenu(tr("&Edit"))
        for a in (self.a_undo, self.a_redo):
            m.addAction(a)
        m.addSeparator()
        for a in (self.a_split, self.a_set_start, self.a_set_end, self.a_keep, self.a_speed,
                  self.a_delete, self.a_restore, self.a_protect, self.a_mark_checked,
                  self.a_unlock):
            m.addAction(a)
        m.addSeparator()
        m.addAction(self.a_select_all)
        m.addAction(self.a_unlock_all)
        m = mb.addMenu(tr("&Auto edit"))
        m.addAction(self.a_board)
        m.addAction(self.a_auto)
        m = mb.addMenu(tr("&Playback"))
        for a in (self.a_play, self.a_frame_back, self.a_frame_fwd, self.a_back_1s, self.a_fwd_1s,
                  self.a_prev_mark, self.a_next_mark, self.a_prev_review, self.a_next_review,
                  self.a_around, self.a_home, self.a_end, self.a_toggle_mode):
            m.addAction(a)
        m.addSeparator()
        m.addAction(self.a_auto_around)
        m = mb.addMenu(tr("&View"))
        for a in (self.a_zoom_in, self.a_zoom_out, self.a_zoom_fit):
            m.addAction(a)
        m.addSeparator()
        m.addAction(self.a_left)
        m.addAction(self.a_right)
        m = mb.addMenu(tr("&Help"))
        m.addAction(self.a_shortcuts)
        m.addAction(self.a_about)
        self._refresh_recent()

    def _connect(self) -> None:
        c = self.ctrl
        c.timelineChanged.connect(self._on_timeline_changed)
        c.settingsChanged.connect(self._on_timeline_changed)
        c.historyChanged.connect(self._update_actions)
        c.selectionChanged.connect(self._on_selection)
        c.dirtyChanged.connect(lambda _d: self._update_title())
        c.projectChanged.connect(self._update_actions)
        e = self.engine
        e.frameReady.connect(self.video.set_frame)
        e.positionChanged.connect(self._on_position)
        e.playingChanged.connect(self._on_playing)
        e.mediaLoaded.connect(self._on_media_loaded)
        t = self.transport
        t.play.clicked.connect(e.toggle)
        t.frame_back.clicked.connect(lambda: e.step(-1))
        t.frame_fwd.clicked.connect(lambda: e.step(1))
        t.prev_mark.clicked.connect(lambda: self.goto_boundary(-1))
        t.next_mark.clicked.connect(lambda: self.goto_boundary(1))
        t.prev_review.clicked.connect(lambda: self.goto_review(-1))
        t.next_review.clicked.connect(lambda: self.goto_review(1))
        t.around.clicked.connect(self.play_around)
        t.mode_edited.toggled.connect(lambda on: on and self._set_mode(EDITED))
        t.mode_source.toggled.connect(lambda on: on and self._set_mode(SOURCE))
        t.volume.valueChanged.connect(self._volume_changed)
        t.mute.toggled.connect(lambda _on: self._volume_changed(t.volume.value()))
        self._volume_changed(t.volume.value())
        self.video.clicked.connect(e.toggle)
        tv = self.timeline.view
        tv.seekRequested.connect(e.seek)
        tv.playFromRequested.connect(self._play_from)
        tv.contextMenuRequested.connect(self._segment_menu)
        tp = self.timeline
        tp.split_btn.clicked.connect(self.split_at_playhead)
        tp.keep_btn.clicked.connect(lambda: self.ctrl.set_action(Action.KEEP))
        tp.speed_btn.clicked.connect(self._set_speed_action)
        tp.delete_btn.clicked.connect(lambda: self.ctrl.set_action(Action.CUT))
        tp.restore_btn.clicked.connect(lambda: self.ctrl.restore())
        tp.protect_btn.clicked.connect(self._toggle_protect)
        tp.mode_edited.toggled.connect(lambda on: on and self._set_mode(EDITED))
        tp.mode_source.toggled.connect(lambda on: on and self._set_mode(SOURCE))
        self.media_panel.seekRequested.connect(e.seek)
        self.media_panel.playRangeRequested.connect(self._play_range)
        self.props.segment.playSegment.connect(self._play_selected)
        self.props.auto.applyRequested.connect(self.run_auto_edit)
        self.hsplit.splitterMoved.connect(lambda *_: self._sync_panel_actions())

    # ================================================================== window state
    def _restore_window(self) -> None:
        geo = self.qs.value("geometry")
        if isinstance(geo, QByteArray):
            self.restoreGeometry(geo)
        else:
            self.resize(1440, 900)
        for key, split in (("hsplit", self.hsplit), ("vsplit", self.vsplit)):
            st = self.qs.value(key)
            if isinstance(st, QByteArray):
                split.restoreState(st)
        if not isinstance(self.qs.value("hsplit"), QByteArray):
            # first start on a small screen: give the preview the room
            w = self.width()
            if w < 1220:
                self.hsplit.setSizes([0, max(520, w - 330), 330])
            else:
                side = 260 if w < 1500 else 280
                right = 330 if w < 1500 else 360
                self.hsplit.setSizes([side, w - side - right, right])
        self._sync_panel_actions()
        self.a_auto_around.setChecked(
            str(self.qs.value("auto_play_around", "true")).lower() == "true")
        QTimer.singleShot(0, self._detect_gpu)

    def closeEvent(self, event) -> None:
        if not self._maybe_save():
            event.ignore()
            return
        self._discard_autosave()
        self.engine.pause()
        self._stop_threads()
        self.qs.setValue("geometry", self.saveGeometry())
        self.qs.setValue("hsplit", self.hsplit.saveState())
        self.qs.setValue("vsplit", self.vsplit.saveState())
        super().closeEvent(event)

    def _stop_threads(self) -> None:
        from .dialogs import stop_background_threads

        for job in (self._thumb_job, getattr(self, "_gpu_job", None)):
            if job is not None:
                try:
                    job.cancel()
                    job.wait(20000)
                except RuntimeError:  # already deleted
                    pass
        self._thumb_job = None
        self._gpu_job = None
        stop_background_threads()

    def _detect_gpu(self) -> None:
        def probe_gpu(_progress, _token):
            from ..core import ffmpeg as ff

            names = []
            for enc, label in (("h264_nvenc", "NVENC"), ("h264_videotoolbox", "VideoToolbox"),
                               ("h264_qsv", "Quick Sync"), ("h264_amf", "AMF")):
                try:
                    if ff.encoder_works(enc):
                        names.append(label)
                except Exception:
                    pass
            return names

        job = JobThread(probe_gpu, self)

        def done(names) -> None:
            if names:
                self.gpu_label.setText(tr("GPU encoder: {n}").format(n=", ".join(names)))
            else:
                self.gpu_label.setText(tr("CPU encoding"))
        job.succeeded.connect(done)
        job.failed.connect(lambda *_: self.gpu_label.setText(tr("FFmpeg not found")))

        def finished() -> None:
            self._gpu_job = None
            job.deleteLater()
        job.finished.connect(finished)
        self._gpu_job: Optional[JobThread] = job
        job.start()

    # ================================================================== helpers
    def _update_title(self) -> None:
        p = self.ctrl.project
        if p is None:
            self.setWindowTitle(f"{APP_NAME} {__version__}")
            self.project_label.setText(APP_NAME)
            return
        name = p.name + (" *" if p.dirty else "")
        self.setWindowTitle(f"{name} - {APP_NAME}")
        self.project_label.setText(name)

    def _update_actions(self) -> None:
        has = self.ctrl.has_project
        for a in (self.a_save, self.a_save_as, self.a_close, self.a_board, self.a_auto,
                  self.a_export, self.a_play, self.a_split, self.a_zoom_in, self.a_zoom_out,
                  self.a_zoom_fit, self.a_unlock_all, self.a_select_all):
            a.setEnabled(has and not self._busy)
        self.a_undo.setEnabled(self.ctrl.history.can_undo and not self._busy)
        self.a_redo.setEnabled(self.ctrl.history.can_redo and not self._busy)
        undo_l = self.ctrl.history.undo_label
        redo_l = self.ctrl.history.redo_label
        self.a_undo.setToolTip(tr("Undo") + (f": {undo_l}" if undo_l and undo_l != "volume-drag"
                                             else "") + " (Ctrl+Z)")
        self.a_redo.setToolTip(tr("Redo") + (f": {redo_l}" if redo_l and redo_l != "volume-drag"
                                             else "") + " (Ctrl+Shift+Z)")
        self._on_selection()

    def _on_selection(self) -> None:
        segs = self.ctrl.selected_segments()
        has = bool(segs)
        for a in (self.a_keep, self.a_speed, self.a_delete, self.a_protect, self.a_unlock,
                  self.a_mark_checked, self.a_set_start, self.a_set_end):
            a.setEnabled(has and not self._busy)
        self.a_restore.setEnabled(any(s.action == Action.CUT for s in segs))
        tp = self.timeline
        tp.keep_btn.setEnabled(has)
        tp.speed_btn.setEnabled(has)
        tp.delete_btn.setEnabled(has and not any(s.protected for s in segs))
        tp.restore_btn.setEnabled(any(s.action == Action.CUT for s in segs))
        tp.protect_btn.setEnabled(has)
        tp.split_btn.setEnabled(self.ctrl.has_project)
        self.timeline.view.update()

    def _on_timeline_changed(self) -> None:
        tl = self.ctrl.timeline
        self.engine.set_edit(tl, self.ctrl.edit_map())
        self.timeline.view.refresh()
        self._update_summary()
        self._on_position(self.engine.position)
        self._on_selection()

    def _update_summary(self) -> None:
        p = self.ctrl.project
        if p is None:
            self.status_summary.setText("")
            return
        st = p.timeline.stats(p.settings)
        text = tr("Original {o} → edited {e} (−{r:.0f}%)").format(
            o=fmt_time(st["source"], 0), e=fmt_time(st["output"], 0), r=st["reduction"] * 100)
        if st["review_open"]:
            text += "  ·  " + tr("{n} to check").format(n=int(st["review_open"]))
        self.status_summary.setText(text)

    def _on_position(self, t: float) -> None:
        self.timeline.view.set_playhead(t)
        m = self.ctrl.edit_map()
        if m is None:
            self.transport.set_times(0, 0, 0, False)
            return
        if self.engine.mode == EDITED:
            self.transport.set_times(m.src_to_out(t), m.out_duration, t, True)
        else:
            dur = self.ctrl.project.media.duration if self.ctrl.project else 0
            self.transport.set_times(t, dur, t, False)
        self.video.set_badge(badge_for(self.ctrl.timeline, m, t, self.ctrl.settings))

    def _on_playing(self, playing: bool) -> None:
        self.transport.set_playing(playing)
        self.a_play.setIcon(icons.icon("pause" if playing else "play"))

    def _on_media_loaded(self, ok: bool, message: str) -> None:
        if not ok and self.ctrl.has_project:
            self.statusBar().showMessage(
                tr("The preview cannot play this file ({m}). Analysis and export still "
                   "work.").format(m=message or "?"), 15000)

    def _volume_changed(self, value: int) -> None:
        self.engine.set_volume(value / 100.0, self.transport.mute.isChecked())

    def _set_mode(self, mode: str) -> None:
        self.engine.set_mode(mode)
        self.timeline.view.set_mode(mode)
        for a, b in ((self.transport.mode_edited, self.transport.mode_source),
                     (self.timeline.mode_edited, self.timeline.mode_source)):
            a.blockSignals(True)
            b.blockSignals(True)
            a.setChecked(mode == EDITED)
            b.setChecked(mode == SOURCE)
            a.blockSignals(False)
            b.blockSignals(False)
        self._on_position(self.engine.position)

    def _toggle_mode(self) -> None:
        self._set_mode(SOURCE if self.engine.mode == EDITED else EDITED)

    def _toggle_left(self, on: bool) -> None:
        self._toggle_panel(0, on, 270)

    def _toggle_right(self, on: bool) -> None:
        self._toggle_panel(2, on, 350)

    def _toggle_panel(self, index: int, on: bool, default: int) -> None:
        sizes = self.hsplit.sizes()
        if on and sizes[index] == 0:
            sizes[index] = default
            sizes[1] = max(300, sizes[1] - default)
        elif not on:
            sizes[1] += sizes[index]
            sizes[index] = 0
        self.hsplit.setSizes(sizes)

    def _sync_panel_actions(self) -> None:
        sizes = self.hsplit.sizes()
        self.a_left.blockSignals(True)
        self.a_right.blockSignals(True)
        self.a_left.setChecked(sizes[0] > 0)
        self.a_right.setChecked(sizes[2] > 0)
        self.a_left.blockSignals(False)
        self.a_right.blockSignals(False)

    def _error(self, title: str, message: str, details: str = "") -> None:
        box = QMessageBox(QMessageBox.Warning, title, message, QMessageBox.Ok, self)
        if details:
            box.setDetailedText(details)
        box.exec()

    # ================================================================== project I/O
    def _maybe_save(self) -> bool:
        p = self.ctrl.project
        if p is None or not p.dirty:
            return True
        r = QMessageBox.question(
            self, APP_NAME, tr("Save changes to \"{n}\"?").format(n=p.name),
            QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel, QMessageBox.Save)
        if r == QMessageBox.Cancel:
            return False
        if r == QMessageBox.Save:
            return self.save_project()
        self._discard_autosave()   # the user chose to throw the changes away
        return True

    def _last_dir(self) -> str:
        return str(self.qs.value("last_dir", os.path.expanduser("~")))

    def import_video(self, path: Optional[str] = None) -> None:
        if not isinstance(path, str) or not path:
            path, _ = QFileDialog.getOpenFileName(
                self, tr("Import video"), self._last_dir(),
                tr("Videos") + f" ({VIDEO_FILTER});;" + tr("All files") + " (*)")
            if not path:
                return
        if not self._maybe_save():
            return
        self.qs.setValue("last_dir", os.path.dirname(path))
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            media = probe(path)
        except CutSenseiError as exc:
            QApplication.restoreOverrideCursor()
            self._error(tr("Import video"), str(exc), getattr(exc, "stderr", ""))
            return
        QApplication.restoreOverrideCursor()
        project = Project(media)
        project.settings = self._default_settings()
        project = self._offer_recovery(project)
        self._set_project(project)
        if project.dirty:
            self.ctrl.mark_dirty()
        self.statusBar().showMessage(
            tr("Loaded {f}. Mark the board region if needed, then press \"Auto edit\".")
            .format(f=os.path.basename(path)), 12000)

    def _default_settings(self) -> AutoEditSettings:
        raw = self.qs.value("default_settings", "")
        try:
            return AutoEditSettings.from_dict(json.loads(raw)) if raw else AutoEditSettings()
        except (ValueError, TypeError):
            return AutoEditSettings()

    def _remember_settings(self) -> None:
        if self.ctrl.project:
            self.qs.setValue("default_settings", json.dumps(self.ctrl.project.settings.to_dict()))

    def open_project(self, path: Optional[str] = None) -> None:
        if not isinstance(path, str) or not path:
            path, _ = QFileDialog.getOpenFileName(
                self, tr("Open project"), self._last_dir(),
                tr("CutSensei project") + f" (*{PROJECT_EXTENSION})")
            if not path:
                return
        if not self._maybe_save():
            return
        try:
            project = Project.load(path)
        except (CutSenseiError, OSError, ValueError, KeyError) as exc:
            self._error(tr("Open project"), tr("The project could not be opened."), str(exc))
            return
        if not project.media_exists:
            r = QMessageBox.question(
                self, tr("Open project"),
                tr("The source video was not found:\n{p}\n\nLocate it now?").format(
                    p=project.media.path))
            if r != QMessageBox.Yes:
                return
            new, _ = QFileDialog.getOpenFileName(
                self, tr("Locate source video"), os.path.dirname(path),
                tr("Videos") + f" ({VIDEO_FILTER})")
            if not new:
                return
            try:
                project = Project.load(path, media_override=new)
                fresh = probe(new)
            except (CutSenseiError, OSError) as exc:
                self._error(tr("Open project"), str(exc))
                return
            if abs(fresh.duration - project.media.duration) > 0.5:
                self._error(tr("Open project"), tr("The selected video has a different length "
                                                   "than the original source."))
                return
            project.media.size, project.media.mtime = fresh.size, fresh.mtime
            project.dirty = True
        self.qs.setValue("last_dir", os.path.dirname(path))
        self._add_recent(path)
        project = self._offer_recovery(project)
        self._set_project(project)
        if project.dirty:
            self.ctrl.mark_dirty()

    # ================================================================== autosave
    def _autosave_file(self, project: Project) -> str:
        return str(autosave_path(project.media.fingerprint()))

    def _autosave(self) -> None:
        p = self.ctrl.project
        if p is None or not p.dirty or self._busy:
            return
        try:
            p.save(self._autosave_file(p), as_copy=True)
        except OSError:
            pass

    def _discard_autosave(self) -> None:
        p = self.ctrl.project
        if p is not None:
            try:
                os.unlink(self._autosave_file(p))
            except OSError:
                pass

    def _offer_recovery(self, project: Project) -> Project:
        auto = self._autosave_file(project)
        if not os.path.isfile(auto):
            return project
        newer = project.path is None or not os.path.isfile(project.path) or \
            os.path.getmtime(auto) > os.path.getmtime(project.path)
        if newer:
            r = QMessageBox.question(
                self, APP_NAME,
                tr("Unsaved edits of this video were recovered from an automatic backup. "
                   "Restore them?"), QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
            if r == QMessageBox.Yes:
                try:
                    rec = Project.load(auto, media_override=project.media.path)
                except (CutSenseiError, OSError, ValueError, KeyError):
                    return project
                rec.path = project.path
                rec.name = project.name
                rec.media = project.media
                rec.dirty = True
                return rec
        try:
            os.unlink(auto)
        except OSError:
            pass
        return project

    def _set_project(self, project: Optional[Project]) -> None:
        self.engine.pause()
        self.ctrl.set_project(project)
        self.video.clear()
        if project is None:
            self.engine.unload()
            self.timeline.view.set_thumbnails(None)
            self.media_panel.set_thumbnails(None)
            self.video.set_hint(tr("No project"))
        else:
            m = project.media
            self.video.set_hint("")
            self.video.set_aspect(m.width / m.height if m.height else 16 / 9)
            self.timeline.view.set_media_aspect(m.width / m.height if m.height else 16 / 9)
            self.engine.load(m.path, m.duration, m.fps)
            self._load_thumbnails()
        self._set_mode(EDITED)
        self._on_timeline_changed()
        self.timeline.view.request_fit()
        self._update_title()
        self._update_actions()

    def _load_thumbnails(self) -> None:
        p = self.ctrl.project
        if p is None or not p.media_exists:
            return
        info = thumbnail_info(p.media)
        if info:
            self.timeline.view.set_thumbnails(info)
            self.media_panel.set_thumbnails(info)
            return
        if self._thumb_job is not None:
            self._thumb_job.cancel()
        media = p.media
        job = JobThread(lambda _prog, token: generate_thumbnails(media, token), self)

        def done(res) -> None:
            if self.ctrl.project is not None and self.ctrl.project.media is media:
                self.timeline.view.set_thumbnails(res)
                self.media_panel.set_thumbnails(res)
        job.succeeded.connect(done)
        job.finished.connect(lambda j=job: self._job_finished(j))
        self._thumb_job = job
        job.start()

    def _job_finished(self, job: JobThread) -> None:
        if self._thumb_job is job:
            self._thumb_job = None
        job.deleteLater()

    def save_project(self) -> bool:
        p = self.ctrl.project
        if p is None:
            return False
        if not p.path:
            return self.save_project_as()
        try:
            p.save(p.path)
        except OSError as exc:
            self._error(tr("Save"), tr("The project could not be saved."), str(exc))
            return False
        self._discard_autosave()
        self.ctrl.mark_saved()
        self._remember_settings()
        self.statusBar().showMessage(tr("Saved {p}").format(p=p.path), 5000)
        return True

    def save_project_as(self) -> bool:
        p = self.ctrl.project
        if p is None:
            return False
        default = p.path or os.path.join(os.path.dirname(p.media.path), p.name + PROJECT_EXTENSION)
        path, _ = QFileDialog.getSaveFileName(self, tr("Save as..."), default,
                                              tr("CutSensei project") + f" (*{PROJECT_EXTENSION})")
        if not path:
            return False
        if not path.endswith(PROJECT_EXTENSION):
            path += PROJECT_EXTENSION
        p.name = os.path.splitext(os.path.basename(path))[0]
        p.path = path
        ok = self.save_project()
        if ok:
            self._add_recent(path)
            self._update_title()
        return ok

    def close_project(self) -> None:
        if self._maybe_save():
            self._discard_autosave()
            self._set_project(None)

    def _add_recent(self, path: str) -> None:
        recent = [r for r in self._recent() if r != path]
        recent.insert(0, path)
        self.qs.setValue("recent", json.dumps(recent[:10]))
        self._refresh_recent()

    def _recent(self) -> List[str]:
        try:
            return list(json.loads(self.qs.value("recent", "[]")))
        except (ValueError, TypeError):
            return []

    def _refresh_recent(self) -> None:
        self.recent_menu.clear()
        items = [r for r in self._recent() if os.path.isfile(r)]
        for path in items:
            a = self.recent_menu.addAction(os.path.basename(path))
            a.setToolTip(path)
            a.triggered.connect(lambda _=False, p=path: self.open_project(p))
        self.recent_menu.setEnabled(bool(items))

    # ================================================================== drag & drop
    def dragEnterEvent(self, e) -> None:
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e) -> None:
        urls = [u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()]
        if not urls:
            return
        path = urls[0]
        if path.endswith(PROJECT_EXTENSION):
            self.open_project(path)
        else:
            self.import_video(path)

    # ================================================================== board / auto edit
    def edit_board_region(self) -> None:
        p = self.ctrl.project
        if p is None:
            return
        self.engine.pause()
        dlg = BoardRegionDialog(p.media, p.board_regions, self.engine.position, self)
        if dlg.exec() == BoardRegionDialog.Accepted:
            before = regions_signature(p.board_regions)
            self.ctrl.set_board_regions(dlg.regions())
            if before != regions_signature(p.board_regions) and p.analysis is not None:
                self.statusBar().showMessage(
                    tr("Board region changed. Press \"Auto edit\" to analyse again."), 10000)

    def run_auto_edit(self) -> None:
        p = self.ctrl.project
        if p is None:
            self.import_video()
            return
        if not p.media_exists:
            self._error(tr("Auto edit"), tr("The source video was not found."))
            return
        self.engine.pause()
        need_video = p.analysis is None or \
            regions_signature(p.analysis.board_regions) != regions_signature(p.board_regions)
        engine = p.settings.vad_engine
        need_audio = p.analysis is None or (
            engine != VadEngine.AUTO and p.analysis.vad_engine and engine != p.analysis.vad_engine)
        if not need_video and not need_audio:
            self._apply_auto()
            return
        media, regions = p.media, [list(r) for r in p.board_regions]
        previous = p.analysis
        hw = str(self.qs.value("hw_decode", "false")).lower() == "true"

        def work(progress, token):
            return analyze(media, regions, engine, progress=progress, cancel=token,
                           hw_decode=hw, previous=previous,
                           reuse_audio=(previous is not None and not need_audio))

        def ok(result) -> None:
            if self.ctrl.project is not p:
                return
            self.ctrl.set_analysis(result)
            self._apply_auto()

        self._set_busy(True)
        runner = ProgressRunner(self, tr("Auto edit"), tr("Analysing speech and board writing..."),
                                work, ok,
                                lambda m, d: self._error(tr("Auto edit"),
                                                         tr("The analysis failed: {m}").format(m=m),
                                                         d),
                                lambda: self.statusBar().showMessage(tr("Analysis cancelled."),
                                                                     5000))
        runner.finished.connect(lambda: self._set_busy(False))
        runner.start()

    def _apply_auto(self) -> None:
        p = self.ctrl.project
        if p is None:
            return
        locked = self.ctrl.locked_count()
        self.ctrl.apply_auto_edit()
        self._remember_settings()
        st = p.timeline.stats(p.settings)
        msg = tr("Auto edit done: {o} → {e}. Sped up {s}, deleted {c}, {n} to check.").format(
            o=fmt_duration(st["source"]), e=fmt_duration(st["output"]),
            s=fmt_duration(st["sped_source"]), c=fmt_duration(st["cut"]), n=int(st["review"]))
        if locked:
            msg += " " + tr("{n} manually edited segments were kept.").format(n=locked)
        self.statusBar().showMessage(msg, 20000)
        self.timeline.view.request_fit()
        if st["review_open"]:
            self.media_panel.show_review_tab()

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._update_actions()

    # ================================================================== export
    def export(self) -> None:
        p = self.ctrl.project
        if p is None:
            return
        if not p.media_exists:
            self._error(tr("Export"), tr("The source video was not found."))
            return
        self.engine.pause()
        out_dir = p.export.last_dir or os.path.dirname(p.media.path)
        default = os.path.join(out_dir, f"{p.name}_edited.mp4")
        m = self.ctrl.edit_map()
        dlg = ExportDialog(p.media, p.export, m.out_duration if m else 0, default, self)
        if dlg.exec() != ExportDialog.Accepted:
            return
        p.export = dlg.settings
        self.ctrl.mark_dirty()
        out = dlg.output_path()
        media, timeline = p.media, p.timeline.clone()
        settings = AutoEditSettings.from_dict(p.settings.to_dict())
        ex = dlg.settings

        def work(progress, token):
            return export_video(media, timeline, settings, ex, out, progress=progress,
                                cancel=token)

        def ok(res) -> None:
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Information)
            box.setWindowTitle(tr("Export"))
            text = tr("Exported {p}\nLength {d} · encoder {e}").format(
                p=res.path, d=fmt_time(res.duration, 1), e=res.encoder)
            if res.fallback_reason:
                text += "\n" + res.fallback_reason
            box.setText(text)
            open_btn = box.addButton(tr("Open folder"), QMessageBox.ActionRole)
            box.addButton(QMessageBox.Ok)
            box.exec()
            if box.clickedButton() is open_btn:
                QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.dirname(res.path)))

        self._set_busy(True)
        runner = ProgressRunner(self, tr("Export"), tr("Exporting video..."), work, ok,
                                lambda msg, d: self._error(tr("Export"),
                                                           tr("Export failed: {m}").format(m=msg),
                                                           d),
                                lambda: self.statusBar().showMessage(tr("Export cancelled."),
                                                                     5000),
                                messages={"audio": tr("Rendering audio..."),
                                          "video": tr("Encoding video...")})
        runner.finished.connect(lambda: self._set_busy(False))
        runner.start()

    def show_preferences(self) -> None:
        PreferencesDialog(self.qs, self).exec()

    # ================================================================== navigation
    def _jump_seconds(self, sec: float) -> None:
        m = self.ctrl.edit_map()
        if m is None:
            return
        if self.engine.mode == EDITED:
            self.engine.seek_output(max(0.0, m.src_to_out(self.engine.position) + sec))
        else:
            self.engine.seek(self.engine.position + sec)

    def _goto_end(self) -> None:
        p = self.ctrl.project
        if p is not None:
            self.engine.seek(p.media.duration)

    def _marks(self) -> List[float]:
        tl = self.ctrl.timeline
        m = self.ctrl.edit_map()
        if tl is None or m is None:
            return []
        if self.engine.mode == EDITED:
            return sorted({pc.src_start for pc in m.pieces} | {pc.src_end for pc in m.pieces})
        return sorted({s.start for s in tl} | {tl.duration})

    def _auto_around_toggled(self, on: bool) -> None:
        self.qs.setValue("auto_play_around", "true" if on else "false")

    def _after_jump(self) -> None:
        if self.a_auto_around.isChecked():
            self.play_around()

    def goto_boundary(self, direction: int) -> None:
        marks = self._marks()
        t = self.engine.position
        if direction > 0:
            nxt = [x for x in marks if x > t + 1e-3]
            if nxt:
                self.engine.pause()
                self.engine.seek(nxt[0])
                self._after_jump()
        else:
            prv = [x for x in marks if x < t - 0.05]
            if prv:
                self.engine.pause()
                self.engine.seek(prv[-1])
                self._after_jump()

    def goto_review(self, direction: int) -> None:
        tl = self.ctrl.timeline
        if tl is None:
            return
        items = [s for s in tl if s.review and not s.reviewed] or [s for s in tl if s.review]
        if not items:
            self.statusBar().showMessage(tr("There is nothing to check."), 4000)
            return
        t = self.engine.position
        if direction > 0:
            cand = [s for s in items if s.start > t + 1e-3] or items[:1]
            seg = cand[0]
        else:
            cand = [s for s in items if s.start < t - 0.05] or items[-1:]
            seg = cand[-1]
        self.engine.pause()
        self.ctrl.select([seg.id])
        self.engine.seek(seg.start)
        self._after_jump()

    def play_around(self) -> None:
        m = self.ctrl.edit_map()
        if m is None:
            return
        t = self.engine.position
        if self.engine.mode == EDITED:
            o = m.src_to_out(t)
            a = m.out_to_src(max(0.0, o - 2.0))
            b = m.out_to_src(min(m.out_duration, o + 2.0))
            self.engine.play_range(a, b, return_to=t)
        else:
            self.engine.play_range(max(0.0, t - 2.0), t + 2.0, return_to=t)

    def _play_range(self, start: float, end: float, mode: str) -> None:
        if mode == "source" and self.engine.mode == EDITED:
            self.engine.play_range(start, end, SOURCE)
        elif mode == "edited" and self.engine.mode == SOURCE:
            self.engine.play_range(start, end, EDITED)
        else:
            self.engine.play_range(start, end)

    def _play_from(self, t: float) -> None:
        self.engine.seek(t)
        self.engine.play()

    def _play_selected(self) -> None:
        segs = self.ctrl.selected_segments()
        if not segs:
            return
        a, b = segs[0].start, segs[-1].end
        if any(s.action == Action.CUT for s in segs):
            self.engine.play_range(a, b, SOURCE if self.engine.mode == EDITED else None)
        else:
            self.engine.play_range(a, b)

    # ================================================================== edits
    def split_at_playhead(self) -> None:
        if not self.ctrl.split_at(self.engine.position):
            self.statusBar().showMessage(tr("Cannot split here (too close to a boundary)."), 4000)

    def _set_edge_to_playhead(self, start: bool) -> None:
        idx = self.ctrl.selected_indices()
        tl = self.ctrl.timeline
        if tl is None:
            return
        t = self.engine.position
        i = (idx[0] if start else idx[-1]) if idx else tl.index_at(t)
        boundary = i if start else i + 1
        if not self.ctrl.set_boundary(boundary, t):
            self.statusBar().showMessage(tr("There is no neighbouring segment on that side."),
                                         4000)

    def _set_speed_action(self) -> None:
        segs = self.ctrl.selected_segments()
        if not segs:
            return
        speed = next((s.speed for s in segs if s.speed), None) or self.ctrl.settings.writing_speed
        self.ctrl.set_action(Action.SPEED, speed=speed)

    def _toggle_protect(self) -> None:
        segs = self.ctrl.selected_segments()
        if segs:
            self.ctrl.set_protected(not all(s.protected for s in segs))

    def _unlock_all(self) -> None:
        if not self.ctrl.has_project:
            return
        r = QMessageBox.question(self, tr("Release all manual edits"),
                                 tr("All manual changes and protections are released and the "
                                    "automatic edit is regenerated. Continue?"))
        if r == QMessageBox.Yes:
            self.ctrl.unlock_all()

    def _select_all(self) -> None:
        tl = self.ctrl.timeline
        if tl is not None:
            self.ctrl.select([s.id for s in tl])

    def _segment_menu(self, pos, index: int) -> None:
        tl = self.ctrl.timeline
        if tl is None:
            return
        menu = QMenu(self)
        for a in (self.a_keep, self.a_speed, self.a_delete, self.a_restore):
            menu.addAction(a)
        menu.addSeparator()
        menu.addAction(self.a_protect)
        menu.addAction(self.a_mark_checked)
        menu.addAction(self.a_unlock)
        menu.addSeparator()
        menu.addAction(self.a_split)
        play = menu.addAction(icons.icon("play"), tr("Play segment"))
        play.triggered.connect(self._play_selected)
        menu.exec(pos)
