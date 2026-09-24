"""Unusual inputs: no audio, odd sizes, rotation, VFR, unicode paths, tiny files."""

import os
import subprocess

import pytest

from cutsensei.analysis.classifier import regenerate
from cutsensei.analysis.pipeline import analyze
from cutsensei.core import ffmpeg
from cutsensei.core.media import probe
from cutsensei.core.project import Project
from cutsensei.core.settings import AutoEditSettings, ExportSettings
from cutsensei.core.timeline import Action, Kind, Segment, Timeline
from cutsensei.render.exporter import export_video

pytestmark = pytest.mark.slow


def make_video(path, *args, duration=6):
    cmd = [ffmpeg.find_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
           "-i", f"testsrc2=size=320x240:rate=25:duration={duration}"]
    cmd += list(args) + [str(path)]
    subprocess.run(cmd, check=True)
    return str(path)


def simple_edit(duration):
    return Timeline(duration, [Segment(0, 1.5, Kind.SPEECH, Action.KEEP),
                               Segment(1.5, 3.0, Kind.IDLE, Action.CUT),
                               Segment(3.0, duration, Kind.WRITING, Action.SPEED)])


def export_and_check(src, tmp_path, name="out.mp4", **ex):
    media = probe(src)
    st = AutoEditSettings(writing_speed=2.0)
    tl = simple_edit(media.duration)
    out = str(tmp_path / name)
    res = export_video(media, tl, st, ExportSettings(encoder="libx264", quality=0, **ex), out)
    info = probe(out)
    assert info.duration == pytest.approx(tl.build_map(st).out_duration, abs=0.15)
    return media, info, res


def test_video_without_audio(tmp_path):
    src = make_video(tmp_path / "noaudio.mp4", "-c:v", "libx264", "-pix_fmt", "yuv420p")
    media = probe(src)
    assert not media.has_audio
    res = analyze(media, [])
    assert not res.has_audio and res.speech.max() == 0
    tl = regenerate(Timeline(media.duration), res, AutoEditSettings())
    assert tl[0].start == 0 and tl[-1].end == pytest.approx(media.duration)
    _, info, _ = export_and_check(src, tmp_path)
    assert not info.has_audio


def test_odd_dimensions_and_mono_44k(tmp_path):
    src = make_video(tmp_path / "odd.mkv", "-f", "lavfi", "-i",
                     "sine=frequency=500:sample_rate=44100:duration=6",
                     "-vf", "scale=333:241", "-c:v", "libx264", "-pix_fmt", "yuv444p",
                     "-c:a", "aac", "-ac", "1", "-shortest")
    media, info, _ = export_and_check(src, tmp_path)
    assert info.width % 2 == 0 and info.height % 2 == 0
    assert info.has_audio


def test_rotated_phone_video(tmp_path):
    src = make_video(tmp_path / "rot.mp4", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                     "-metadata:s:v:0", "rotate=90")
    media = probe(src)
    if media.rotation not in (90, 270):
        # newer FFmpeg versions ignore the legacy rotate tag; use the display matrix
        src2 = str(tmp_path / "rot2.mp4")
        subprocess.run([ffmpeg.find_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
                        "-display_rotation", "90", "-i", src, "-c", "copy", src2], check=False)
        if os.path.isfile(src2):
            media = probe(src2)
            src = src2
    if media.rotation not in (90, 270):
        pytest.skip("this FFmpeg cannot write rotation metadata")
    assert (media.width, media.height) == (240, 320)
    res = analyze(media, [[0.1, 0.1, 0.8, 0.8]])
    assert len(res.ink) == res.n_frames
    _, info, _ = export_and_check(src, tmp_path, "rot_out.mp4")
    assert (info.width, info.height) in ((240, 320), (320, 240))


def test_variable_frame_rate_and_start_offset(tmp_path):
    src = make_video(tmp_path / "vfr.mkv", "-f", "lavfi", "-i",
                     "sine=frequency=700:duration=6", "-vf", "setpts=PTS+gt(N\\,50)*0.3/TB",
                     "-fps_mode", "passthrough", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                     "-c:a", "libopus"
                     if "libopus" in ffmpeg.available_encoders() else "aac", "-shortest")
    export_and_check(src, tmp_path, "vfr_out.mp4")


def test_unicode_and_space_paths(tmp_path):
    d = tmp_path / "講義 動画 テスト"
    d.mkdir()
    src = make_video(d / "第1回 講義.mp4", "-f", "lavfi", "-i", "sine=frequency=300:duration=6",
                     "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest")
    media = probe(src)
    res = analyze(media, [])
    proj = Project(media)
    proj.analysis = res
    proj.timeline = regenerate(proj.timeline, res, proj.settings)
    ppath = d / "プロジェクト.cutsensei"
    proj.save(str(ppath))
    loaded = Project.load(str(ppath))
    assert loaded.media.path == media.path
    export_and_check(src, tmp_path, "書き出し 結果.mp4")


def test_very_short_video(tmp_path):
    src = make_video(tmp_path / "tiny.mp4", "-f", "lavfi", "-i", "sine=duration=0.6",
                     "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
                     duration=0.6)
    media = probe(src)
    res = analyze(media, [])
    tl = regenerate(Timeline(media.duration), res, AutoEditSettings())
    assert tl[-1].end == pytest.approx(media.duration)
    out = str(tmp_path / "tiny_out.mp4")
    export_video(media, tl, AutoEditSettings(), ExportSettings(encoder="libx264"), out)
    assert probe(out).duration > 0.3


def test_many_segments(tmp_path):
    """Hundreds of segments (a long lecture) must not break the filter graph."""
    src = make_video(tmp_path / "many.mp4", "-f", "lavfi", "-i", "sine=duration=20",
                     "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
                     duration=20)
    media = probe(src)
    segs = []
    t = 0.0
    i = 0
    while t < media.duration - 0.05:
        end = min(media.duration, t + 0.05)
        action = (Action.KEEP, Action.SPEED, Action.CUT)[i % 3]
        segs.append(Segment(t, end, Kind.SPEECH, action))
        t = end
        i += 1
    tl = Timeline(media.duration, segs)
    st = AutoEditSettings(writing_speed=3.0)
    out = str(tmp_path / "many_out.mp4")
    export_video(media, tl, st, ExportSettings(encoder="libx264", quality=0), out)
    assert probe(out).duration == pytest.approx(tl.build_map(st).out_duration, abs=0.15)
