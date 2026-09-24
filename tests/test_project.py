import json
import os

import numpy as np
import pytest

from cutsensei.analysis.result import AnalysisResult
from cutsensei.core.history import History
from cutsensei.core.media import MediaInfo, parse_ffmpeg_banner
from cutsensei.core.project import Project
from cutsensei.core.settings import AutoEditSettings, ExportSettings
from cutsensei.core.timeline import Action


def fake_media(tmp_path, duration=30.0):
    p = tmp_path / "video.mp4"
    p.write_bytes(b"not really a video")
    return MediaInfo(path=str(p), duration=duration, width=1920, height=1080, fps=30.0)


def test_project_roundtrip(tmp_path):
    proj = Project(fake_media(tmp_path))
    proj.settings.writing_speed = 6.0
    proj.board_regions = [[0.1, 0.1, 0.8, 0.6]]
    proj.timeline.split_at(10.0)
    proj.timeline.set_action([1], Action.CUT)
    res = AnalysisResult(duration=30.0)
    res.speech = np.linspace(0, 1, 300).astype(np.float32)
    res.wave_peaks = np.arange(3000, dtype=np.uint8)
    res.ensure_lengths()
    proj.analysis = res
    out = tmp_path / "p.cutsensei"
    proj.save(str(out))
    loaded = Project.load(str(out))
    assert loaded.settings.writing_speed == 6.0
    assert loaded.board_regions == [[0.1, 0.1, 0.8, 0.6]]
    assert [s.to_dict() for s in loaded.timeline] == [s.to_dict() for s in proj.timeline]
    assert loaded.analysis is not None
    assert np.allclose(loaded.analysis.speech, res.speech, atol=1e-3)
    assert loaded.analysis.wave_peaks.dtype == np.uint8
    assert not loaded.dirty


def test_project_finds_moved_media(tmp_path):
    proj = Project(fake_media(tmp_path))
    out = tmp_path / "p.cutsensei"
    proj.save(str(out))
    data = json.loads(out.read_text(encoding="utf-8"))
    data["media"]["path"] = "/nonexistent/dir/video.mp4"
    out.write_text(json.dumps(data), encoding="utf-8")
    loaded = Project.load(str(out))
    assert os.path.samefile(loaded.media.path, tmp_path / "video.mp4")


def test_settings_derivation_and_bounds():
    cons = AutoEditSettings(aggressiveness=0)
    aggr = AutoEditSettings(aggressiveness=100)
    assert cons.eff_min_cut > aggr.eff_min_cut
    assert cons.eff_hold_after_writing > aggr.eff_hold_after_writing
    st = AutoEditSettings(min_cut=7.5)
    assert st.eff_min_cut == 7.5
    loaded = AutoEditSettings.from_dict({"writing_speed": 999, "speed_audio": "bogus"})
    assert loaded.writing_speed == 64.0 and loaded.speed_audio == "keep"
    ex = ExportSettings.from_dict({"quality": 9, "encoder": "nope"})
    assert ex.quality == 3 and ex.encoder == "auto"


def test_history_undo_redo():
    h = History()
    state = {"v": 1}
    h.push("a", state)
    state = {"v": 2}
    h.push("b", state)
    state = {"v": 3}
    prev = h.undo(state)
    assert prev == {"v": 2}
    prev2 = h.undo(prev)
    assert prev2 == {"v": 1}
    assert not h.can_undo
    nxt = h.redo(prev2)
    assert nxt == {"v": 2}


BANNER = """Input #0, mov,mp4,m4a,3gp,3g2,mj2, from 'x.mp4':
  Duration: 01:02:03.50, start: 0.021333, bitrate: 1234 kb/s
  Stream #0:0[0x1](und): Video: h264 (High) (avc1 / 0x31637661), yuv420p(tv, bt709, progressive), 1920x1080 [SAR 1:1 DAR 16:9], 4000 kb/s, 29.97 fps, 29.97 tbr, 30k tbn (default)
      Side data:
        displaymatrix: rotation of -90.00 degrees
  Stream #0:1[0x2](und): Audio: aac (LC) (mp4a / 0x6134706D), 48000 Hz, stereo, fltp, 128 kb/s (default)
"""


def test_banner_parser():
    info = parse_ffmpeg_banner(BANNER, "x.mp4")
    assert info.duration == pytest.approx(3723.5)
    assert info.has_video and info.has_audio
    assert info.fps == pytest.approx(29.97)
    assert info.rotation == 90
    assert (info.width, info.height) == (1080, 1920)
    assert info.audio_rate == 48000 and info.audio_channels == 2
    assert info.start_time == pytest.approx(0.021333)
