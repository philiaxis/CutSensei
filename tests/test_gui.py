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
    from PySide6.QtCore import QSettings

    QSettings.setDefaultFormat(QSettings.IniFormat)
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, str(tmp_path / "settings"))
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
