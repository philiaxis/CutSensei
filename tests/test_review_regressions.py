"""Regression tests for issues found in code review."""

import os

import pytest

from cutsensei.core.media import MediaInfo
from cutsensei.core.project import Project
from cutsensei.core.timeline import Action, Kind, Segment, Timeline


def _project(tmp_path, duration=10.0):
    src = tmp_path / "video.mp4"
    src.write_bytes(b"x")
    p = Project(MediaInfo(path=str(src), duration=duration, width=320, height=180, fps=30.0))
    p.timeline = Timeline(duration, [Segment(0, 5, Kind.SPEECH, Action.KEEP),
                                     Segment(5, duration, Kind.IDLE, Action.KEEP)])
    return p


def test_undo_merge_rules(qapp, tmp_path):
    from cutsensei.ui.controller import ProjectController

    c = ProjectController()
    c.set_project(_project(tmp_path))
    # a new edit after undo must clear redo (no silent merge)
    c.update_settings(writing_speed=3.0)
    c.set_action(Action.CUT, [1])
    c.undo()
    c.update_settings(pad_before=0.9)
    assert not c.history.can_redo
    # rapid changes of the same setting are one undo step
    c.update_settings(aggressiveness=60)
    c.update_settings(aggressiveness=70)
    c.undo()
    assert c.project.settings.aggressiveness == 50
    # volume changes of different segments are separate steps
    c.set_volume(0.5, [0], merge_key="drag")
    c.set_volume(0.2, [1], merge_key="drag")
    c.undo()
    assert [s.volume for s in c.project.timeline] == [0.5, None]
    # clicking a boundary without moving it creates no undo step
    n = len(c.history._undo)
    c.begin_boundary_drag()
    c.end_boundary_drag()
    assert len(c.history._undo) == n


def test_relative_media_path_is_portable(tmp_path):
    p = _project(tmp_path)
    sub = tmp_path / "projects"
    sub.mkdir()
    data = p.to_dict(str(sub / "a.cutsensei"))
    assert data["media"]["relpath"] == "../video.mp4"
    # a project written on Windows (backslashes) opens elsewhere
    import json
    data["media"]["relpath"] = "..\\video.mp4"
    data["media"]["path"] = "C:\\Users\\x\\video.mp4"
    (sub / "b.cutsensei").write_text(json.dumps(data), encoding="utf-8")
    loaded = Project.load(str(sub / "b.cutsensei"))
    assert os.path.samefile(loaded.media.path, tmp_path / "video.mp4")


def test_same_file_detection(tmp_path):
    from cutsensei.render.exporter import same_file

    f = tmp_path / "a.mp4"
    f.write_bytes(b"x")
    assert same_file(str(f), str(tmp_path / "." / "a.mp4"))
    if hasattr(os, "symlink"):
        link = tmp_path / "link.mp4"
        try:
            os.symlink(f, link)
        except OSError:
            pass
        else:
            assert same_file(str(link), str(f))
    assert not same_file(str(tmp_path / "b.mp4"), str(f))


@pytest.mark.slow
def test_export_keeps_result_when_target_is_locked(short_video, tmp_path, monkeypatch):
    from cutsensei.core.media import probe
    from cutsensei.core.settings import AutoEditSettings, ExportSettings
    from cutsensei.render import exporter

    path, spec = short_video
    media = probe(path)
    out = tmp_path / "out.mp4"
    out.write_bytes(b"old, open in a player")
    real_replace = os.replace

    def locked_replace(src, dst):
        if os.path.abspath(dst) == os.path.abspath(out):
            raise PermissionError("in use")
        return real_replace(src, dst)
    monkeypatch.setattr(exporter.os, "replace", locked_replace)
    tl = Timeline(spec.duration, [Segment(0, 3, Kind.SPEECH, Action.KEEP),
                                  Segment(3, spec.duration, Kind.IDLE, Action.CUT)])
    res = exporter.export_video(media, tl, AutoEditSettings(),
                                ExportSettings(encoder="libx264", quality=0), str(out))
    assert res.path != str(out) and os.path.isfile(res.path)
    assert "in use" in res.fallback_reason
    assert probe(res.path).duration == pytest.approx(3.0, abs=0.1)


@pytest.mark.slow
def test_preview_reload_same_file_and_restart_after_end(qtbot, short_video):
    from cutsensei.core.settings import AutoEditSettings
    from cutsensei.ui.preview import PreviewEngine

    path, spec = short_video
    eng = PreviewEngine()
    eng.load(path, spec.duration, spec.fps)
    qtbot.waitUntil(lambda: eng._loaded, timeout=15000)
    eng.load(path, spec.duration, spec.fps)          # same file again
    qtbot.waitUntil(lambda: eng._loaded, timeout=15000)
    # edit ending with a cut at a non frame aligned time
    tl = Timeline(spec.duration, [Segment(0, 1.23, Kind.SPEECH, Action.KEEP),
                                  Segment(1.23, spec.duration, Kind.IDLE, Action.CUT)])
    eng.set_edit(tl, tl.build_map(AutoEditSettings()))
    eng.seek(0.8)
    eng.play()
    qtbot.waitUntil(lambda: not eng.playing, timeout=5000)
    eng.play()                                        # starts again from the beginning
    qtbot.wait(300)
    assert eng.playing and eng.position < 0.9
    eng.pause()
    # play-around near the end returns to the playhead
    eng.seek(1.0)
    eng.play_range(0.8, 1.23, return_to=1.0)
    qtbot.waitUntil(lambda: not eng.playing, timeout=5000)
    assert eng.position == pytest.approx(1.0, abs=0.01)


@pytest.fixture
def lagging_players(monkeypatch):
    """QMediaPlayer.position() keeps reporting the old time for 400 ms after
    each seek, like on a busy machine (seen on the macOS CI runners)."""
    import time

    from PySide6.QtMultimedia import QMediaPlayer

    real_pos, real_set = QMediaPlayer.position, QMediaPlayer.setPosition
    state = {}

    def position(self):
        old = state.get(id(self))
        if old is not None and time.monotonic() - old[1] < 0.4:
            return old[0]
        return real_pos(self)

    def set_position(self, ms):
        # a new seek before the previous one finished keeps the stale value
        state[id(self)] = (position(self), time.monotonic())
        real_set(self, ms)

    monkeypatch.setattr(QMediaPlayer, "setPosition", set_position)
    monkeypatch.setattr(QMediaPlayer, "position", position)


def _engine(qtbot, short_video, segments):
    from cutsensei.core.settings import AutoEditSettings
    from cutsensei.ui.preview import PreviewEngine

    path, spec = short_video
    eng = PreviewEngine()
    eng.load(path, spec.duration, spec.fps)
    qtbot.waitUntil(lambda: eng._loaded, timeout=15000)
    tl = Timeline(spec.duration, segments)
    eng.set_edit(tl, tl.build_map(AutoEditSettings()))
    return eng


@pytest.mark.slow
def test_preview_skips_cut_when_seeking_is_slow(qtbot, short_video, lagging_players):
    """A stale position() right after jumping over a cut used to trigger the
    jump again on every tick, so the seek never finished (stuck at 6.0 s)."""
    _path, spec = short_video
    eng = _engine(qtbot, short_video, [Segment(0, 1.0, Kind.SPEECH, Action.KEEP),
                                       Segment(1.0, 6.0, Kind.IDLE, Action.CUT),
                                       Segment(6.0, spec.duration, Kind.SPEECH, Action.KEEP)])
    # the standby player never shows its frame in time: every jump has to seek
    on_frame = eng._on_frame
    eng._on_frame = lambda slot, frame: None if slot.index == 1 else on_frame(slot, frame)
    eng.seek(0.0)
    eng.play()
    qtbot.wait(2600)
    eng.pause()
    assert eng.position > 6.3


@pytest.mark.slow
def test_preview_restarts_when_seeking_is_slow(qtbot, short_video, lagging_players):
    """A stale position() at the end of the edit used to stop the playback
    that Play had just restarted from the beginning."""
    _path, spec = short_video
    eng = _engine(qtbot, short_video, [Segment(0, 1.23, Kind.SPEECH, Action.KEEP),
                                       Segment(1.23, spec.duration, Kind.IDLE, Action.CUT)])
    eng.seek(0.8)
    eng.play()
    qtbot.waitUntil(lambda: not eng.playing, timeout=5000)
    eng.play()
    qtbot.wait(300)
    assert eng.playing and eng.position < 0.9


@pytest.mark.slow
def test_preview_reload_is_not_ready_before_the_new_media_is(qtbot, short_video):
    """Re-opening a file: stop() reports "LoadedMedia" for the old media,
    which used to mark the preview as ready while the file was still loading
    (Play then ran against a loading player on slower machines)."""
    from cutsensei.ui.preview import PreviewEngine

    path, spec = short_video
    eng = PreviewEngine()
    eng.load(path, spec.duration, spec.fps)
    qtbot.waitUntil(lambda: eng._loaded, timeout=15000)
    eng.play()
    qtbot.wait(300)
    eng.pause()
    eng.load(path, spec.duration, spec.fps)
    assert not eng._loaded
    qtbot.waitUntil(lambda: eng._loaded, timeout=15000)
    eng.play()
    qtbot.wait(300)
    assert eng.playing and 0.0 < eng.position < 1.0
