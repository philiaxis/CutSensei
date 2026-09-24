"""GUI tests (run with the offscreen Qt platform)."""

import json
import os
import string
import subprocess
import sys

import pytest

pytest.importorskip("PySide6.QtMultimedia")
pytestmark = pytest.mark.gui

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture
def window(qtbot, tmp_path, monkeypatch):
    monkeypatch.setenv("CUTSENSEI_SETTINGS", str(tmp_path / "settings.ini"))
    from cutsensei.ui import i18n, theme
    from cutsensei.ui.main_window import MainWindow
    from PySide6.QtWidgets import QApplication, QMessageBox

    i18n.set_language("en")
    theme.apply_theme(QApplication.instance())
    # never block on message boxes in tests
    monkeypatch.setattr(QMessageBox, "exec", lambda self: QMessageBox.Ok)
    monkeypatch.setattr(QMessageBox, "question",
                        staticmethod(lambda *a, **k: QMessageBox.Discard))
    win = MainWindow()
    qtbot.addWidget(win)
    win.resize(1400, 860)
    win.show()
    yield win
    if win.ctrl.project is not None:
        win.ctrl.project.dirty = False
    win.engine.pause()


def test_translations_complete():
    out = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "extract_strings.py")],
                         capture_output=True, text=True, cwd=ROOT, check=True).stdout
    strings = [json.loads(line) for line in out.splitlines() if line.strip()]
    from cutsensei.ui.translations_ja import STRINGS

    missing = [s for s in strings if s not in STRINGS]
    assert not missing, missing
    for key, value in STRINGS.items():
        ph_k = sorted(f[1] for f in string.Formatter().parse(key) if f[1])
        ph_v = sorted(f[1] for f in string.Formatter().parse(value) if f[1])
        assert ph_k == ph_v, key


def test_empty_window_renders(window):
    img = window.grab()
    assert not img.isNull()
    assert not window.a_export.isEnabled()


@pytest.mark.slow
def test_full_workflow(window, qtbot, short_video, tmp_path, monkeypatch):
    from cutsensei.core.timeline import Action
    from cutsensei.demo import board_region

    path, spec = short_video
    window.import_video(path)
    assert window.ctrl.project is not None
    qtbot.waitUntil(lambda: window.engine._loaded, timeout=15000)
    window.ctrl.set_board_regions([board_region(spec)])
    # auto edit runs in a worker thread with a progress dialog
    window.run_auto_edit()
    qtbot.waitUntil(lambda: window.ctrl.project.auto_applied, timeout=120000)
    qtbot.waitUntil(lambda: not window._busy, timeout=10000)
    tl = window.ctrl.timeline
    assert any(s.action == Action.SPEED for s in tl)
    assert any(s.action == Action.CUT for s in tl)
    stats = tl.stats(window.ctrl.settings)
    assert stats["output"] < stats["source"]

    # timeline paints in both modes and at different zoom levels
    tv = window.timeline.view
    for mode in ("edited", "source"):
        window._set_mode(mode)
        tv.zoom(3.0)
        assert not tv.grab().isNull()
        tv.zoom_to_fit()

    # manual edits + undo / redo
    i = tl.index_at(1.0)
    window.ctrl.select_index(i)
    before = window.ctrl.project.edit_state()
    window.a_delete.trigger()
    assert window.ctrl.timeline.segment_at(1.0).action == Action.CUT
    window.a_restore.trigger()
    assert window.ctrl.timeline.segment_at(1.0).action == Action.KEEP
    window.a_undo.trigger()
    window.a_undo.trigger()
    assert window.ctrl.project.edit_state()["segments"] == before["segments"]
    window.a_redo.trigger()
    assert window.ctrl.timeline.segment_at(1.0).action == Action.CUT
    window.a_undo.trigger()

    # split at the playhead, protect, re-apply keeps protected segments
    window.engine.seek(3.0)
    n = len(window.ctrl.timeline)
    window.split_at_playhead()
    assert len(window.ctrl.timeline) == n + 1
    j = window.ctrl.timeline.index_at(16.0)
    window.ctrl.select_index(j)
    window.a_protect.trigger()
    assert window.ctrl.timeline.segment_at(16.0).protected
    window.ctrl.update_settings(aggressiveness=100)
    window.run_auto_edit()
    qtbot.waitUntil(lambda: not window._busy, timeout=60000)
    seg = window.ctrl.timeline.segment_at(16.0)
    assert seg.protected and seg.action != Action.CUT

    # save / reopen
    proj_path = str(tmp_path / "gui.cutsensei")
    window.ctrl.project.path = proj_path
    assert window.save_project()
    window.open_project(proj_path)
    assert window.ctrl.timeline.segment_at(16.0).protected

    # export through the worker
    from cutsensei.render.exporter import export_video
    from cutsensei.core.settings import ExportSettings

    out = str(tmp_path / "gui_out.mp4")
    p = window.ctrl.project
    res = export_video(p.media, p.timeline, p.settings, ExportSettings(encoder="libx264",
                                                                        quality=0), out)
    assert os.path.isfile(out) and res.duration > 0


@pytest.mark.slow
def test_preview_skips_cuts(qtbot, short_video):
    from cutsensei.core.settings import AutoEditSettings
    from cutsensei.core.timeline import Action, Kind, Segment, Timeline
    from cutsensei.ui.preview import PreviewEngine

    path, spec = short_video
    eng = PreviewEngine()
    eng.load(path, spec.duration, spec.fps)
    qtbot.waitUntil(lambda: eng._loaded, timeout=15000)
    st = AutoEditSettings()
    tl = Timeline(spec.duration, [Segment(0, 1.0, Kind.SPEECH, Action.KEEP),
                                  Segment(1.0, 6.0, Kind.IDLE, Action.CUT),
                                  Segment(6.0, spec.duration, Kind.SPEECH, Action.KEEP)])
    eng.set_edit(tl, tl.build_map(st))
    positions = []
    eng.positionChanged.connect(positions.append)
    eng.seek(0.0)
    eng.play()
    qtbot.wait(2200)
    eng.pause()
    inside_cut = [p for p in positions if 1.15 < p < 5.9]
    assert eng.position > 6.0
    assert len(inside_cut) <= 2


def _timeline_window(window, qtbot, short_video):
    from cutsensei.core.project import Project
    from cutsensei.core.media import probe
    from cutsensei.core.timeline import Action, Kind, Segment, Timeline

    path, spec = short_video
    proj = Project(probe(path))
    proj.timeline = Timeline(spec.duration, [
        Segment(0, 5, Kind.SPEECH, Action.KEEP, auto_action=Action.KEEP),
        Segment(5, 10, Kind.WRITING, Action.SPEED, auto_action=Action.SPEED),
        Segment(10, 14, Kind.IDLE, Action.CUT, auto_action=Action.CUT),
        Segment(14, spec.duration, Kind.SPEECH, Action.KEEP, auto_action=Action.KEEP)])
    window._set_project(proj)
    qtbot.waitUntil(lambda: window.engine._loaded, timeout=15000)
    tv = window.timeline.view
    tv.zoom_to_fit()
    return tv


@pytest.mark.slow
def test_timeline_drag_boundary_and_pull_cut(window, qtbot, short_video):
    from PySide6.QtCore import QPoint, Qt

    from cutsensei.core.timeline import Action

    tv = _timeline_window(window, qtbot, short_video)
    _, _, segs, _ = tv._tracks()
    y = int(segs.center().y())
    # --- edited view: boundary between 1x (0-5) and 4x (5-10) follows the mouse
    x0 = int(round(tv.x_of(tv.to_disp(5.0))))
    qtbot.mousePress(tv, Qt.LeftButton, pos=QPoint(x0, y))
    qtbot.mouseMove(tv, QPoint(x0 + 30, y))
    qtbot.mouseRelease(tv, Qt.LeftButton, pos=QPoint(x0 + 30, y))
    moved = window.ctrl.timeline[1].start
    assert moved == pytest.approx(5.0 + 30 / tv.pps, abs=2 / tv.pps)
    assert window.ctrl.timeline[0].manual and window.ctrl.timeline[1].manual
    window.a_undo.trigger()
    assert window.ctrl.timeline[1].start == pytest.approx(5.0)
    # --- pulling the cut marker to the left reveals material before the next part
    tv.refresh()
    xc = int(round(tv.x_of(tv.to_disp(12.0))))
    qtbot.mousePress(tv, Qt.LeftButton, pos=QPoint(xc, y))
    qtbot.mouseMove(tv, QPoint(xc - 5, y))
    qtbot.mouseMove(tv, QPoint(xc - 25, y))
    qtbot.mouseRelease(tv, Qt.LeftButton, pos=QPoint(xc - 25, y))
    tl = window.ctrl.timeline
    cut = next(s for s in tl if s.action == Action.CUT)
    assert cut.end == pytest.approx(14.0 - 25 / tv.pps, abs=2 / tv.pps)  # less removed
    assert tl.segment_at(13.9).action == Action.KEEP
    # --- source view: plain linear dragging
    window._set_mode("source")
    tv.refresh()
    xb = int(round(tv.x_of(10.0)))
    qtbot.mousePress(tv, Qt.LeftButton, pos=QPoint(xb, y))
    qtbot.mouseMove(tv, QPoint(xb + 20, y))
    qtbot.mouseRelease(tv, Qt.LeftButton, pos=QPoint(xb + 20, y))
    assert window.ctrl.timeline.segment_at(10.0 + 10 / tv.pps).action == Action.SPEED


@pytest.mark.slow
def test_timeline_click_select_zoom_and_properties(window, qtbot, short_video):
    from PySide6.QtCore import QPoint, QPointF, Qt
    from PySide6.QtGui import QWheelEvent
    from PySide6.QtWidgets import QApplication

    from cutsensei.core.timeline import Action

    tv = _timeline_window(window, qtbot, short_video)
    _, _, segs, _ = tv._tracks()
    y = int(segs.center().y())
    x = int(tv.x_of(tv.to_disp(2.0)))
    qtbot.mouseClick(tv, Qt.LeftButton, pos=QPoint(x, y))
    assert window.ctrl.selected_indices() == [0]
    # shift-click extends the selection
    x2 = int(tv.x_of(tv.to_disp(7.0)))
    qtbot.mouseClick(tv, Qt.LeftButton, Qt.ShiftModifier, QPoint(x2, y))
    assert window.ctrl.selected_indices() == [0, 1]
    # properties panel: set speed 2x for both
    window.ctrl.select_index(1)
    props = window.props.segment
    props.speed.setValue(2.0)
    assert window.ctrl.timeline[1].speed == 2.0
    props.act_buttons[Action.KEEP].click()
    assert window.ctrl.timeline[1].action == Action.KEEP
    # ctrl+wheel zooms in around the cursor
    pps = tv.pps
    ev = QWheelEvent(QPointF(200, y), QPointF(tv.mapToGlobal(QPoint(200, y))), QPoint(0, 0),
                     QPoint(0, 240), Qt.NoButton, Qt.ControlModifier, Qt.NoScrollPhase, False)
    QApplication.sendEvent(tv, ev)
    assert tv.pps > pps
    # clicking the ruler seeks
    qtbot.mouseClick(tv, Qt.LeftButton, pos=QPoint(int(tv.x_of(tv.to_disp(3.0))), 8))
    assert window.engine.position == pytest.approx(3.0, abs=0.2)


@pytest.mark.slow
def test_autosave_recovery(window, qtbot, short_video, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    from cutsensei.core.timeline import Action

    path, _spec = short_video
    window.import_video(path)
    window.ctrl.select_index(0)
    window.ctrl.set_action(Action.CUT)
    assert window.ctrl.project.dirty
    window._autosave()
    auto = window._autosave_file(window.ctrl.project)
    assert os.path.isfile(auto)
    # simulate a crash: forget the project without the save question
    window.ctrl.project.dirty = False
    window.ctrl.set_project(None)
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.Yes))
    window.import_video(path)
    assert window.ctrl.project.dirty
    assert window.ctrl.timeline[0].action == Action.CUT
    # saving removes the backup
    window.ctrl.project.path = str(os.path.join(os.path.dirname(auto), "..", "rec.cutsensei"))
    assert window.save_project()
    assert not os.path.isfile(auto)


@pytest.mark.slow
def test_play_around_returns_to_playhead(qtbot, short_video):
    from cutsensei.core.settings import AutoEditSettings
    from cutsensei.core.timeline import Timeline
    from cutsensei.ui.preview import PreviewEngine

    path, spec = short_video
    eng = PreviewEngine()
    eng.load(path, spec.duration, spec.fps)
    qtbot.waitUntil(lambda: eng._loaded, timeout=15000)
    tl = Timeline(spec.duration)
    eng.set_edit(tl, tl.build_map(AutoEditSettings()))
    eng.seek(5.0)
    eng.play_range(4.5, 5.5, return_to=5.0)
    assert eng.playing
    qtbot.waitUntil(lambda: not eng.playing, timeout=5000)
    assert eng.position == pytest.approx(5.0, abs=0.01)
    # source-mode range from the edited preview restores the mode afterwards
    eng.play_range(1.0, 1.4, mode="source")
    assert eng.mode == "source"
    qtbot.waitUntil(lambda: not eng.playing, timeout=5000)
    assert eng.mode == "edited"


@pytest.mark.slow
def test_set_edges_to_playhead(window, qtbot, short_video):
    tv = _timeline_window(window, qtbot, short_video)
    window.ctrl.select_index(1)            # writing segment 5-10
    window.engine.seek(6.0)
    window.a_set_start.trigger()
    assert window.ctrl.timeline[1].start == pytest.approx(6.0, abs=0.01)
    assert window.ctrl.timeline[0].end == pytest.approx(6.0, abs=0.01)
    window.engine.seek(9.0)
    window.ctrl.select_index(1)
    window.a_set_end.trigger()
    assert window.ctrl.timeline[1].end == pytest.approx(9.0, abs=0.01)
    window.a_undo.trigger()
    assert window.ctrl.timeline[1].end == pytest.approx(10.0, abs=0.01)
    # V toggles the preview / timeline mode
    window.a_toggle_mode.trigger()
    assert window.engine.mode == "source" and tv.mode == "source"
    window.a_toggle_mode.trigger()
    assert window.engine.mode == "edited"
