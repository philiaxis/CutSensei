import math

import pytest

from cutsensei.core.settings import AutoEditSettings, SpeedAudio
from cutsensei.core.timeline import (MIN_SEGMENT, Action, Kind, Segment, Timeline,
                                     overlay_locked)


def make_tl():
    # 0-10 speech keep, 10-30 writing speed, 30-40 idle cut, 40-50 speech keep
    return Timeline(50.0, [
        Segment(0, 10, Kind.SPEECH, Action.KEEP, auto_action=Action.KEEP),
        Segment(10, 30, Kind.WRITING, Action.SPEED, auto_action=Action.SPEED),
        Segment(30, 40, Kind.IDLE, Action.CUT, auto_action=Action.CUT),
        Segment(40, 50, Kind.SPEECH, Action.KEEP, auto_action=Action.KEEP),
    ])


def test_output_duration_and_map():
    st = AutoEditSettings(writing_speed=4.0)
    tl = make_tl()
    m = tl.build_map(st)
    assert m.out_duration == pytest.approx(10 + 5 + 10)
    assert m.src_to_out(5) == pytest.approx(5)
    assert m.src_to_out(20) == pytest.approx(10 + 10 / 4)
    # inside the cut maps to the start of the next kept piece
    assert m.src_to_out(35) == pytest.approx(15)
    assert m.src_to_out(45) == pytest.approx(20)
    assert m.out_to_src(12.5) == pytest.approx(20)
    assert m.out_to_src(15.0) == pytest.approx(40)
    assert m.piece_index_at_src(35) == -1


def test_design_doc_example_arithmetic():
    """60 s speech + 40 s writing at 4x + 20 s idle -> 70 s."""
    st = AutoEditSettings(writing_speed=4.0)
    tl = Timeline(120.0, [Segment(0, 60, Kind.SPEECH, Action.KEEP),
                          Segment(60, 100, Kind.WRITING, Action.SPEED),
                          Segment(100, 120, Kind.IDLE, Action.CUT)])
    assert tl.build_map(st).out_duration == pytest.approx(70.0)
    stats = tl.stats(st)
    assert stats["output"] == pytest.approx(70.0)
    assert stats["cut"] == pytest.approx(20.0)
    assert stats["sped_output"] == pytest.approx(10.0)


def test_normalize_fills_gaps_and_overlaps():
    tl = Timeline(10.0, [Segment(0, 3), Segment(2, 6), Segment(7, 12)])
    starts = [s.start for s in tl]
    ends = [s.end for s in tl]
    assert starts[0] == 0.0 and ends[-1] == 10.0
    for a, b in zip(tl.segments, tl.segments[1:]):
        assert a.end == pytest.approx(b.start)


def test_split_and_move_boundary():
    tl = make_tl()
    res = tl.split_at(5.0)
    assert res == (0, 1)
    assert tl[0].end == 5.0 and tl[1].start == 5.0
    assert tl.split_at(5.01) is None  # too close to the new boundary
    applied = tl.move_boundary(1, 7.0)
    assert applied == 7.0 and tl[0].end == 7.0 and tl[1].start == 7.0
    assert tl[0].manual and tl[1].manual
    # clamped inside the neighbours
    applied = tl.move_boundary(1, 100.0)
    assert applied == pytest.approx(tl[1].end - MIN_SEGMENT)


def test_actions_protect_restore_unlock():
    tl = make_tl()
    tl.set_protected([3], True)
    tl.set_action([3], Action.CUT)
    assert tl[3].action == Action.KEEP  # protected is never cut
    tl.restore([2])
    assert tl[2].action == Action.KEEP and tl[2].manual
    tl.set_speed([0], 2.0)
    assert tl[0].action == Action.SPEED and tl[0].speed == 2.0
    tl.set_speed([0], 1.0)
    assert tl[0].action == Action.KEEP
    tl.unlock([0, 2])
    assert not tl[0].manual and tl[2].action == Action.CUT


def test_volume_rules():
    st = AutoEditSettings(speed_audio=SpeedAudio.MUTE)
    tl = make_tl()
    assert tl[1].volume_value(st) == 0.0
    st.speed_audio = SpeedAudio.VOLUME
    st.speed_audio_volume = 0.3
    assert tl[1].volume_value(st) == pytest.approx(0.3)
    assert tl[0].volume_value(st) == 1.0
    tl.set_volume([0], 0.5)
    assert tl[0].volume_value(st) == 0.5


def test_overlay_locked_keeps_manual_segments():
    old = make_tl()
    old[2].action = Action.KEEP
    old[2].manual = True          # user restored 30-40
    auto = [Segment(0, 25, Kind.SPEECH, Action.KEEP), Segment(25, 50, Kind.IDLE, Action.CUT)]
    merged = overlay_locked(auto, old.segments, 50.0)
    tl = Timeline(50.0, merged)
    assert tl.segment_at(35).manual and tl.segment_at(35).action == Action.KEEP
    assert tl.segment_at(27).action == Action.CUT
    assert tl.segment_at(45).action == Action.CUT
    assert tl[0].start == 0 and tl[-1].end == 50


def test_merge_equal_neighbors():
    tl = Timeline(10, [Segment(0, 5, Kind.SPEECH, Action.KEEP, reason="speech"),
                       Segment(5, 10, Kind.SPEECH, Action.KEEP, reason="speech")])
    tl.merge_equal_neighbors()
    assert len(tl) == 1


def test_serialization_roundtrip():
    tl = make_tl()
    tl[1].speed = 3.0
    tl[1].review = True
    data = tl.to_list()
    tl2 = Timeline.from_list(50.0, data)
    assert [s.to_dict() for s in tl2] == data


def test_speed_value_inf_for_cut():
    tl = make_tl()
    assert math.isinf(tl[2].speed_value(AutoEditSettings()))
