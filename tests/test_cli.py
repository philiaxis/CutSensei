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
