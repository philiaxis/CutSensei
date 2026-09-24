import json

import pytest

from cutsensei.cli import main
from cutsensei.core.media import probe
from cutsensei.core.project import Project


def test_info(capsys):
    assert main(["info"]) == 0
    out = capsys.readouterr().out
    assert "ffmpeg" in out and "encoders" in out


@pytest.mark.slow
def test_analyze_segments_export(short_video, tmp_path, capsys):
    path, spec = short_video
    proj = str(tmp_path / "short.cutsensei")
    assert main(["-q", "analyze", path, "-o", proj, "--speed", "4",
                 "--board", "0.08,0.08,0.84,0.64"]) == 0
    p = Project.load(proj)
    assert p.auto_applied and p.analysis is not None
    assert p.board_regions == [[0.08, 0.08, 0.84, 0.64]]
    capsys.readouterr()
    assert main(["segments", proj, "--json"]) == 0
    segs = json.loads(capsys.readouterr().out)
    assert segs[0]["start"] == 0.0 and segs[-1]["end"] == pytest.approx(p.media.duration)
    assert any(s["action"] == "speed" for s in segs)
    out_mp4 = str(tmp_path / "short_edited.mp4")
    assert main(["-q", "export", proj, "-o", out_mp4, "--encoder", "libx264",
                 "--quality", "0"]) == 0
    info = probe(out_mp4)
    expected = p.timeline.build_map(p.settings).out_duration
    assert info.duration == pytest.approx(expected, abs=0.1)
    assert info.duration < p.media.duration


def test_bad_board_argument(short_video):
    path, _ = short_video
    with pytest.raises(SystemExit):
        main(["analyze", path, "--board", "0.1,0.2"])


@pytest.mark.slow
def test_screen_recording_source_option(screen_webcam_video, tmp_path, capsys):
    path, _spec = screen_webcam_video
    proj = str(tmp_path / "notes.cutsensei")
    assert main(["-q", "analyze", path, "-o", proj]) == 0
    out = capsys.readouterr().out
    assert "recording   : screen (detected, camera picture ignored)" in out
    p = Project.load(proj)
    assert p.analysis.source == "screen" and p.settings.source_type == "auto"
    # forcing the other type is honoured (and remembered in the project)
    assert main(["-q", "analyze", path, "-o", proj, "--source", "camera"]) == 0
    assert "recording   : camera (chosen)" in capsys.readouterr().out
    p = Project.load(proj)
    assert p.analysis.source == "camera" and p.settings.source_type == "camera"
    assert p.analysis.source_detected == "screen"


def test_demo_screen_scenario(tmp_path, capsys):
    out = str(tmp_path / "notes.mp4")
    assert main(["demo", out, "--scenario", "screen"]) == 0
    info = probe(out)
    assert info.width == 960 and info.height == 720
    assert info.duration == pytest.approx(120.0, abs=0.2)
