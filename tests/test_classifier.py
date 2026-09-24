import numpy as np
import pytest

from cutsensei.analysis.classifier import auto_segments, regenerate
from cutsensei.analysis.result import AnalysisResult
from cutsensei.core.settings import AutoEditSettings
from cutsensei.core.timeline import Action, Kind, Timeline


def make_result(spec):
    """spec: list of (duration, speech, ink, clicks, motion)."""
    dur = sum(s[0] for s in spec)
    res = AnalysisResult(duration=dur)
    n = res.frames_for(dur)
    arrays = {k: np.zeros(n, np.float32) for k in ("speech", "ink", "clicks", "motion", "hand")}
    i = 0
    for d, sp, ink, clicks, motion in spec:
        m = int(round(d / res.hop))
        arrays["speech"][i:i + m] = sp
        arrays["ink"][i:i + m] = ink
        arrays["clicks"][i:i + m] = clicks
        arrays["motion"][i:i + m] = motion
        i += m
    for k, v in arrays.items():
        setattr(res, k, v)
    res.ensure_lengths()
    return res


def segment_at(segs, t):
    return next(s for s in segs if s.start <= t < s.end)


def test_basic_rules():
    res = make_result([(20, 0.9, 0, 0, 0), (20, 0, 300, 3, 0), (20, 0, 0, 0, 0),
                       (10, 0.9, 0, 0, 0)])
    st = AutoEditSettings()
    segs = auto_segments(res, st)
    assert segment_at(segs, 10).action == Action.KEEP
    assert segment_at(segs, 10).kind == Kind.SPEECH
    assert segment_at(segs, 30).action == Action.SPEED
    assert segment_at(segs, 50).action == Action.CUT
    assert segment_at(segs, 65).action == Action.KEEP
    tl = Timeline(res.duration, segs)
    out = tl.build_map(st).out_duration
    # 30 s speech + 20 s / 4 + protective margins
    assert 35.0 <= out <= 38.5


def test_speech_wins_over_writing():
    res = make_result([(10, 0.9, 300, 3, 0.02)])
    segs = auto_segments(res, AutoEditSettings())
    assert all(s.action == Action.KEEP and s.kind == Kind.SPEECH for s in segs)


def test_speech_margins():
    res = make_result([(10, 0, 0, 0, 0), (5, 0.9, 0, 0, 0), (10, 0, 0, 0, 0)])
    st = AutoEditSettings(pad_before=0.3, pad_after=0.5)
    segs = auto_segments(res, st)
    speech = [s for s in segs if s.kind == Kind.SPEECH]
    assert len(speech) == 1
    assert speech[0].start == pytest.approx(10 - 0.3, abs=0.11)
    assert speech[0].end == pytest.approx(15 + 0.5, abs=0.11)


def test_short_pause_is_not_cut():
    res = make_result([(10, 0.9, 0, 0, 0), (1.5, 0, 0, 0, 0), (10, 0.9, 0, 0, 0)])
    segs = auto_segments(res, AutoEditSettings())
    assert not any(s.action == Action.CUT for s in segs)


def test_breathing_gaps_stay_speech():
    spec = []
    for _ in range(10):
        spec += [(1.2, 0.9, 0, 0, 0), (0.3, 0.0, 0, 0, 0)]
    res = make_result(spec)
    segs = auto_segments(res, AutoEditSettings())
    assert len(segs) == 1 and segs[0].kind == Kind.SPEECH


def test_short_writing_not_sped_up():
    res = make_result([(10, 0.9, 0, 0, 0), (0.8, 0, 400, 3, 0), (10, 0.9, 0, 0, 0)])
    segs = auto_segments(res, AutoEditSettings())
    assert not any(s.action == Action.SPEED for s in segs)


def test_writing_with_short_pauses_is_one_segment():
    spec = []
    for _ in range(6):
        spec += [(3.0, 0, 300, 3, 0), (1.0, 0, 0, 0, 0)]
    res = make_result([(5, 0.9, 0, 0, 0)] + spec + [(5, 0.9, 0, 0, 0)])
    segs = auto_segments(res, AutoEditSettings())
    speeds = [s for s in segs if s.action == Action.SPEED]
    assert len(speeds) == 1


def test_uncertain_is_kept_and_flagged():
    # silent movement without writing -> review
    res = make_result([(10, 0.9, 0, 0, 0), (6, 0.0, 0, 0, 0.03), (10, 0.9, 0, 0, 0)])
    segs = auto_segments(res, AutoEditSettings())
    mid = segment_at(segs, 13)
    assert mid.action == Action.KEEP and mid.review and mid.kind == Kind.UNCERTAIN
    # possibly quiet speech -> review, never cut
    res = make_result([(10, 0.9, 0, 0, 0), (6, 0.36, 0, 0, 0), (10, 0.9, 0, 0, 0)])
    segs = auto_segments(res, AutoEditSettings())
    assert segment_at(segs, 13).action == Action.KEEP


def test_aggressiveness_changes_cut_length():
    res = make_result([(10, 0.9, 0, 0, 0), (3, 0, 0, 0, 0), (10, 0.9, 0, 0, 0)])
    cons = auto_segments(res, AutoEditSettings(aggressiveness=0))
    aggr = auto_segments(res, AutoEditSettings(aggressiveness=100))
    assert not any(s.action == Action.CUT for s in cons)
    assert any(s.action == Action.CUT for s in aggr)


def test_regenerate_preserves_manual_and_protected():
    res = make_result([(20, 0.9, 0, 0, 0), (20, 0, 300, 3, 0), (20, 0, 0, 0, 0)])
    st = AutoEditSettings()
    tl = Timeline(res.duration, auto_segments(res, st))
    i = tl.index_at(50)
    assert tl[i].action == Action.CUT
    tl.set_protected([i], True)             # user: always keep
    j = tl.index_at(30)
    tl.set_speed([j], 2.0)                  # user: 2x instead of 4x
    st.writing_speed = 8.0
    st.aggressiveness = 100
    new = regenerate(tl, res, st)
    assert new.segment_at(50).protected and new.segment_at(50).action == Action.KEEP
    assert new.segment_at(30).speed == 2.0
    unlocked = regenerate(tl, res, st, keep_locked=False)
    assert unlocked.segment_at(50).action == Action.CUT


def test_review_fragments_are_merged():
    # chalk sounds without visible ink: many "possibly writing" bits with short gaps
    spec = [(10, 0.9, 0, 0, 0)]
    for _ in range(5):
        spec += [(1.5, 0, 0, 6.0, 0), (0.8, 0, 0, 0, 0)]
    spec += [(10, 0.9, 0, 0, 0)]
    res = make_result(spec)
    segs = auto_segments(res, AutoEditSettings())
    reviews = [s for s in segs if s.review]
    assert len(reviews) == 1
    assert reviews[0].duration > 9.0
