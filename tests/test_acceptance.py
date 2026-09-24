"""Acceptance scenario from the design document (section 6).

"説明60秒・無言の板書40秒・不要な待機20秒の動画を板書4倍速で処理した場合、
保護余白を除けば、説明60秒＋板書10秒＝70秒になる。"

The demo video is analysed with the real pipeline (audio + video), edited
automatically and exported; the result must be ~70 s plus the protective
margins, speech must stay at 1x, silent writing must be sped up (not cut)
and the idle part must be removed.
"""

import pytest

from cutsensei.analysis.classifier import regenerate
from cutsensei.analysis.pipeline import analyze
from cutsensei.core.media import probe
from cutsensei.core.project import Project
from cutsensei.core.settings import AutoEditSettings, ExportSettings
from cutsensei.core.timeline import Action, Kind
from cutsensei.demo import board_region
from cutsensei.render.exporter import export_video

pytestmark = pytest.mark.slow


@pytest.fixture(scope="module")
def analysed(demo_video):
    path, spec = demo_video
    media = probe(path)
    res = analyze(media, [board_region(spec)])
    return media, spec, res


def _check_timeline(tl, st):
    edit = tl.build_map(st)
    # speech (0-60 s) is kept at normal speed, including its start and end
    for t in (0.05, 10, 30, 59.8):
        seg = tl.segment_at(t)
        assert seg.action == Action.KEEP and seg.speed_value(st) == 1.0, t
    # silent writing (60-100 s) is sped up, never removed
    for t in (62, 70, 80, 90, 99):
        seg = tl.segment_at(t)
        assert seg.action == Action.SPEED, t
        assert seg.kind == Kind.WRITING
    # most of the idle part (100-120 s) is removed
    cut = sum(s.duration for s in tl if s.action == Action.CUT and s.start >= 99.0)
    assert cut >= 14.0
    return edit.out_duration


@pytest.mark.parametrize("aggressiveness", [0, 50, 100])
def test_design_doc_scenario(analysed, aggressiveness):
    media, spec, res = analysed
    proj = Project(media)
    proj.settings = AutoEditSettings(writing_speed=4.0, aggressiveness=aggressiveness)
    tl = regenerate(proj.timeline, res, proj.settings)
    out = _check_timeline(tl, proj.settings)
    # 70 s + protective margins (speech margin, finished-board hold, detection tails)
    assert 70.0 <= out <= 75.0, out


def test_regions_and_whole_frame_agree(demo_video):
    path, _spec = demo_video
    media = probe(path)
    res = analyze(media, [])   # no board region: whole frame
    tl = regenerate(Project(media).timeline, res, AutoEditSettings())
    out = _check_timeline(tl, AutoEditSettings())
    assert 70.0 <= out <= 76.0


def test_export_of_design_doc_scenario(analysed, tmp_path):
    media, spec, res = analysed
    st = AutoEditSettings(writing_speed=4.0)
    proj = Project(media)
    tl = regenerate(proj.timeline, res, st)
    expected = tl.build_map(st).out_duration
    out = tmp_path / "edited.mp4"
    result = export_video(media, tl, st, ExportSettings(encoder="libx264", quality=0), str(out))
    info = probe(str(out))
    assert info.duration == pytest.approx(expected, abs=0.1)
    assert result.duration == pytest.approx(expected)
