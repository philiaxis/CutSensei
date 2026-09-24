"""End-to-end export tests: durations, audio/video synchronisation."""

import json
import subprocess

import numpy as np
import pytest

from conftest import SYNC_DUR, SYNC_FPS, SYNC_SR, decode_frame_pattern, tone_freq
from cutsensei.core import ffmpeg
from cutsensei.core.media import probe
from cutsensei.core.settings import AutoEditSettings, ExportSettings, SpeedAudio
from cutsensei.core.timeline import Action, Kind, Segment, Timeline
from cutsensei.render.exporter import build_video_filter, export_video

pytestmark = pytest.mark.slow


def stream_durations(path):
    probe_exe = ffmpeg.find_ffprobe()
    if probe_exe is None:
        info = probe(path)
        return info.duration, info.duration
    out = subprocess.run([probe_exe, "-v", "error", "-show_entries", "stream=codec_type,duration",
                          "-of", "json", path], capture_output=True, check=True).stdout
    streams = json.loads(out)["streams"]
    v = next(float(s["duration"]) for s in streams if s["codec_type"] == "video")
    a = next((float(s["duration"]) for s in streams if s["codec_type"] == "audio"), None)
    return v, a


def decode_frames(path, w=320, h=96):
    args = ["-i", path, "-an", "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1"]
    frames = []
    with ffmpeg.RawPipeReader(args) as reader:
        for raw in reader.read_blocks(w * h):
            if len(raw) < w * h:
                break
            frames.append(np.frombuffer(raw, np.uint8).reshape(h, w))
    return frames


def decode_audio(path, sr=SYNC_SR):
    args = ["-i", path, "-vn", "-ac", "1", "-ar", str(sr), "-f", "f32le", "pipe:1"]
    chunks = []
    with ffmpeg.RawPipeReader(args) as reader:
        for raw in reader.read_blocks(sr * 4):
            chunks.append(np.frombuffer(raw, np.float32))
    return np.concatenate(chunks)


def tone_center(audio, freq, sr, t_expect, search=0.4):
    """Time (s) of maximum energy at ``freq`` near ``t_expect``."""
    win = int(0.02 * sr)
    i0 = max(0, int((t_expect - search) * sr))
    i1 = min(len(audio), int((t_expect + search) * sr))
    seg = audio[i0:i1]
    if len(seg) < win * 2:
        return None, 0.0
    t = np.arange(len(seg)) / sr
    c = np.cos(2 * np.pi * freq * t) * seg
    s = np.sin(2 * np.pi * freq * t) * seg
    kernel = np.ones(win)
    energy = np.convolve(c, kernel, "same") ** 2 + np.convolve(s, kernel, "same") ** 2
    k = int(np.argmax(energy))
    return (i0 + k) / sr, float(energy[k])


def sync_timeline():
    st = AutoEditSettings(writing_speed=4.0, speed_audio=SpeedAudio.KEEP)
    tl = Timeline(SYNC_DUR, [
        Segment(0.0, 4.5, Kind.SPEECH, Action.KEEP),
        Segment(4.5, 8.3, Kind.IDLE, Action.CUT),
        Segment(8.3, 16.3, Kind.WRITING, Action.SPEED),          # 8 s -> 2 s
        Segment(16.3, 19.7, Kind.SPEECH, Action.KEEP),
        Segment(19.7, 21.2, Kind.IDLE, Action.CUT),
        Segment(21.2, SYNC_DUR, Kind.SPEECH, Action.KEEP, volume=0.8),
    ])
    return tl, st


def test_filter_graph_is_well_formed():
    tl, st = sync_timeline()
    graph = build_video_filter(tl.build_map(st), "30/1", None, 1 / 60)
    assert graph.startswith("[0:v]select=") and graph.endswith("[vout]")
    assert graph.count("gte(t,") == 4


def test_export_av_sync(sync_video, tmp_path):
    media = probe(sync_video)
    tl, st = sync_timeline()
    edit = tl.build_map(st)
    out = str(tmp_path / "out.mp4")
    res = export_video(media, tl, st, ExportSettings(encoder="libx264", quality=2), out)
    assert res.duration == pytest.approx(edit.out_duration)
    v_dur, a_dur = stream_durations(out)
    assert v_dur == pytest.approx(edit.out_duration, abs=0.06)
    assert a_dur == pytest.approx(edit.out_duration, abs=0.06)

    # --- video: every output frame shows the source frame the edit map predicts
    frames = decode_frames(out)
    assert abs(len(frames) - round(edit.out_duration * SYNC_FPS)) <= 1
    errors = []
    for i, frame in enumerate(frames):
        t_out = (i + 0.5) / SYNC_FPS
        expect_src = edit.out_to_src(t_out)
        piece = edit.pieces[edit.piece_index_at_out(t_out)]
        got_src = decode_frame_pattern(frame) / SYNC_FPS
        tolerance = (1.5 * piece.speed) / SYNC_FPS + 0.02
        errors.append(abs(got_src - expect_src) > tolerance)
    assert sum(errors) <= 2, f"{sum(errors)} frames out of sync"

    # --- audio: each tone that survives the edit is where the map says
    audio = decode_audio(out)
    checked = 0
    for k in range(SYNC_DUR):
        center_src = k + 0.03
        pi = edit.piece_index_at_src(center_src)
        if pi < 0:
            continue
        p = edit.pieces[pi]
        if center_src - p.src_start < 0.08 or p.src_end - center_src < 0.08:
            continue
        expect = edit.src_to_out(center_src)
        found, energy = tone_center(audio, tone_freq(k), SYNC_SR, expect)
        assert found is not None and energy > 0
        tol = 0.04 if p.speed == 1.0 else 0.06
        assert abs(found - expect) < tol, f"tone {k}: {found:.3f} vs {expect:.3f}"
        checked += 1
    assert checked >= 12


def test_export_keeps_pitch_in_sped_up_part(sync_video, tmp_path):
    media = probe(sync_video)
    st = AutoEditSettings(writing_speed=4.0)
    tl = Timeline(SYNC_DUR, [Segment(0.0, SYNC_DUR, Kind.WRITING, Action.SPEED)])
    out = str(tmp_path / "fast.mp4")
    export_video(media, tl, st, ExportSettings(encoder="libx264", quality=0), out)
    audio = decode_audio(out)
    # tone k=10 (1900 Hz) must still be at 1900 Hz after 4x speed-up
    center = (10 + 0.03) / 4.0
    seg = audio[int((center - 0.01) * SYNC_SR):int((center + 0.01) * SYNC_SR)]
    spec = np.abs(np.fft.rfft(seg * np.hanning(len(seg)), 8192))
    peak = np.fft.rfftfreq(8192, 1 / SYNC_SR)[np.argmax(spec)]
    assert abs(peak - tone_freq(10)) < 60


def test_export_rejects_empty_and_source_overwrite(sync_video, tmp_path):
    from cutsensei.core.errors import CutSenseiError

    media = probe(sync_video)
    st = AutoEditSettings()
    tl = Timeline(SYNC_DUR, [Segment(0.0, SYNC_DUR, Kind.IDLE, Action.CUT)])
    with pytest.raises(CutSenseiError):
        export_video(media, tl, st, ExportSettings(), str(tmp_path / "x.mp4"))
    tl2 = Timeline(SYNC_DUR)
    with pytest.raises(CutSenseiError):
        export_video(media, tl2, st, ExportSettings(), sync_video)


def test_export_scaling_and_fps(sync_video, tmp_path):
    media = probe(sync_video)
    st = AutoEditSettings()
    tl = Timeline(SYNC_DUR, [Segment(0.0, 6.0, Kind.SPEECH, Action.KEEP),
                             Segment(6.0, SYNC_DUR, Kind.IDLE, Action.CUT)])
    out = str(tmp_path / "small.mp4")
    ex = ExportSettings(height=48, fps=15.0, encoder="libx264", quality=0)
    export_video(media, tl, st, ex, out)
    info = probe(out)
    assert info.height == 48 and info.width == 160
    assert info.fps == pytest.approx(15.0, abs=0.1)
    assert info.duration == pytest.approx(6.0, abs=0.1)


def test_cancel_export(sync_video, tmp_path):
    from cutsensei.core.errors import Cancelled
    from cutsensei.core.jobs import CancelToken

    media = probe(sync_video)
    st = AutoEditSettings()
    tl = Timeline(SYNC_DUR)
    token = CancelToken()
    token.cancel()
    with pytest.raises(Cancelled):
        export_video(media, tl, st, ExportSettings(encoder="libx264"),
                     str(tmp_path / "c.mp4"), cancel=token)
    assert not (tmp_path / "c.mp4").exists()
