"""Export the edited video (MP4) with FFmpeg.

Video: one filter graph ``select`` (drop cut frames) -> ``setpts`` (piecewise
linear time mapping, compressing sped-up parts) -> ``fps`` (constant output
frame rate) -> optional ``scale``.  The graph is written to a script file so
there is no command line length limit even with thousands of segments.

Audio: rendered in Python (see :mod:`audio_render`) to raw float PCM with
exactly matching duration, then encoded to AAC in the same ffmpeg run.
"""

from __future__ import annotations

import os
import sys
import tempfile
from dataclasses import dataclass
from typing import List, Optional, Tuple

from ..core import ffmpeg
from ..core.errors import Cancelled, CutSenseiError, FFmpegError
from ..core.jobs import CancelToken, Progress, ProgressCallback
from ..core.media import MediaInfo
from ..core.settings import AutoEditSettings, Encoder, ExportSettings
from ..core.timeline import EditMap, Timeline
from .audio_render import render_audio

RESOLUTION_PRESETS = (0, 2160, 1440, 1080, 720, 540, 480, 360)
FPS_PRESETS = (0.0, 60.0, 50.0, 30.0, 29.97, 25.0, 24.0)


@dataclass
class ExportResult:
    path: str
    duration: float
    encoder: str
    fallback_reason: str = ""
    log: str = ""


# ---------------------------------------------------------------------------
# filter graph
# ---------------------------------------------------------------------------

def _merge_for_video(edit: EditMap) -> List[Tuple[float, float, float, float]]:
    """(src_start, src_end, out_start, speed) with contiguous equal-speed
    pieces merged; volume does not matter for video."""
    merged: List[List[float]] = []
    for p in edit.pieces:
        if merged and abs(merged[-1][1] - p.src_start) < 1e-9 and merged[-1][3] == p.speed:
            merged[-1][1] = p.src_end
        else:
            merged.append([p.src_start, p.src_end, p.out_start, p.speed])
    return [tuple(m) for m in merged]  # type: ignore[misc]


def _num(v: float) -> str:
    text = f"{v:.6f}".rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text


def _balanced_sum(terms: List[str]) -> str:
    """``a+b+c+...`` as a balanced tree of parentheses.

    Recent FFmpeg versions limit the nesting depth of expressions (a flat sum
    of ~100 terms is rejected), a balanced tree only needs log2(n) levels.
    """
    if not terms:
        return "0"
    while len(terms) > 1:
        paired = [f"({terms[i]}+{terms[i + 1]})" for i in range(0, len(terms) - 1, 2)]
        if len(terms) % 2:
            paired.append(terms[-1])
        terms = paired
    return terms[0]


def build_video_filter(edit: EditMap, out_fps: str, out_size: Optional[Tuple[int, int]],
                       frame_eps: float) -> str:
    ranges = _merge_for_video(edit)
    if not ranges:
        raise CutSenseiError("Nothing to export: every part of the video is cut.")
    # A frame at time t is shown if a <= t < b.  Frames are sampled at
    # discrete times; shift the start slightly so the first frame of a range
    # that starts between two frames is kept.
    sel_terms = []
    pts_terms = []
    for a, b, o, s in ranges:
        a_s = max(0.0, a - frame_eps)
        sel_terms.append(f"gte(t,{_num(a_s)})*lt(t,{_num(b)})")
        if s == 1.0:
            mapped = f"({_num(o - a)}+T)"
        else:
            mapped = f"({_num(o)}+(T-{_num(a)})/{_num(s)})"
        pts_terms.append(f"gte(T,{_num(a_s)})*lt(T,{_num(b)})*{mapped}")
    select = _balanced_sum(sel_terms)
    setpts = _balanced_sum(pts_terms)
    chain = [f"select='{select}'", f"setpts='max(0,{setpts})/TB'",
             f"fps=fps={out_fps}"]
    if out_size is not None:
        chain.append(f"scale={out_size[0]}:{out_size[1]}:flags=lanczos")
        chain.append("setsar=1")
    chain.append("format=yuv420p")
    return "[0:v]" + ",".join(chain) + "[vout]"


# ---------------------------------------------------------------------------
# encoders
# ---------------------------------------------------------------------------

def auto_encoder_candidates() -> List[str]:
    order = [Encoder.NVENC_H264]
    if sys.platform == "darwin":
        order = [Encoder.VT_H264] + order
    order += [Encoder.QSV_H264, Encoder.AMF_H264]
    return order


def software_fallbacks() -> List[str]:
    return ["libx264", "libopenh264", "mpeg4"]


def choose_encoder(requested: str) -> Tuple[str, str]:
    """Return (encoder, note).  ``note`` explains a fallback."""
    if requested == Encoder.AUTO:
        for enc in auto_encoder_candidates():
            if ffmpeg.encoder_works(enc):
                return enc, ""
        requested = Encoder.X264
    if ffmpeg.encoder_works(requested):
        return requested, ""
    for enc in software_fallbacks():
        if enc in ffmpeg.available_encoders():
            return enc, f"{requested} is not available, using {enc}"
    raise CutSenseiError("No usable H.264 encoder was found in FFmpeg.")


def detect_available_encoders() -> List[str]:
    """Encoders offered in the export dialog (only those that work)."""
    out = [Encoder.AUTO]
    for enc in (Encoder.X264, Encoder.X265, Encoder.NVENC_H264, Encoder.NVENC_HEVC,
                Encoder.QSV_H264, Encoder.AMF_H264, Encoder.VT_H264, Encoder.VT_HEVC):
        try:
            if ffmpeg.encoder_works(enc):
                out.append(enc)
        except Exception:
            pass
    return out


def encoder_args(enc: str, quality: int, width: int, height: int, fps: float) -> List[str]:
    q = max(0, min(3, quality))
    if enc == "libx264":
        crf = (28, 23, 20, 17)[q]
        preset = ("veryfast", "fast", "medium", "slow")[q]
        return ["-c:v", enc, "-preset", preset, "-crf", str(crf), "-profile:v", "high"]
    if enc == "libx265":
        crf = (30, 26, 23, 20)[q]
        return ["-c:v", enc, "-preset", "medium", "-crf", str(crf), "-tag:v", "hvc1"]
    if enc in ("h264_nvenc", "hevc_nvenc"):
        cq = (31, 27, 23, 20)[q]
        args = ["-c:v", enc, "-preset", "p5", "-rc", "vbr", "-cq", str(cq), "-b:v", "0"]
        if enc == "h264_nvenc":
            args += ["-profile:v", "high"]
        else:
            args += ["-tag:v", "hvc1"]
        return args
    if enc == "h264_qsv":
        return ["-c:v", enc, "-global_quality", str((30, 26, 23, 20)[q]), "-preset", "medium"]
    if enc == "h264_amf":
        qp = str((30, 26, 23, 20)[q])
        return ["-c:v", enc, "-quality", "balanced", "-rc", "cqp", "-qp_i", qp, "-qp_p", qp,
                "-qp_b", qp]
    if enc in ("h264_videotoolbox", "hevc_videotoolbox"):
        bpp = (0.05, 0.08, 0.12, 0.18)[q]
        rate = int(max(500_000, width * height * max(fps, 1.0) * bpp))
        args = ["-c:v", enc, "-b:v", str(rate), "-maxrate", str(int(rate * 1.5)),
                "-bufsize", str(rate * 2)]
        if enc == "hevc_videotoolbox":
            args += ["-tag:v", "hvc1"]
        return args
    if enc == "libopenh264":
        rate = int(max(500_000, width * height * max(fps, 1.0) * (0.06, 0.1, 0.14, 0.2)[q]))
        return ["-c:v", enc, "-b:v", str(rate)]
    # generic fallback
    return ["-c:v", enc, "-q:v", str((8, 5, 3, 2)[q])]


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------

def output_geometry(media: MediaInfo, settings: ExportSettings) -> Tuple[int, int, Optional[Tuple[int, int]]]:
    w, h = media.width or 1280, media.height or 720
    if settings.height and settings.height < h + 1 and settings.height != h:
        new_h = int(settings.height) // 2 * 2
        new_w = int(round(w * new_h / h / 2)) * 2
        return new_w, new_h, (new_w, new_h)
    if w % 2 or h % 2:  # yuv420p needs even dimensions
        return w // 2 * 2, h // 2 * 2, (w // 2 * 2, h // 2 * 2)
    return w, h, None


def output_fps(media: MediaInfo, settings: ExportSettings) -> Tuple[str, float]:
    if settings.fps and settings.fps > 0:
        if abs(settings.fps - 29.97) < 0.01:
            return "30000/1001", 29.97
        if abs(settings.fps - 59.94) < 0.01:
            return "60000/1001", 59.94
        if abs(settings.fps - 23.976) < 0.01:
            return "24000/1001", 23.976
        return _num(settings.fps), float(settings.fps)
    fps = media.fps if 1 <= media.fps <= 240 else 30.0
    frac = media.fps_fraction if 1 <= media.fps <= 240 else "30/1"
    return frac, fps


def export_video(media: MediaInfo, timeline: Timeline, auto: AutoEditSettings,
                 settings: ExportSettings, output_path: str,
                 progress: Optional[ProgressCallback] = None,
                 cancel: Optional[CancelToken] = None) -> ExportResult:
    cancel = cancel or CancelToken()
    prog = Progress(progress)
    edit = timeline.build_map(auto)
    if not edit.pieces or edit.out_duration <= 0.01:
        raise CutSenseiError("Nothing to export: every part of the video is cut.")
    if os.path.abspath(output_path) == os.path.abspath(media.path):
        raise CutSenseiError("The output file must not overwrite the source video.")
    out_dir = os.path.dirname(os.path.abspath(output_path))
    os.makedirs(out_dir, exist_ok=True)

    width, height, scale = output_geometry(media, settings)
    fps_str, fps_val = output_fps(media, settings)
    encoder, note = choose_encoder(settings.encoder)

    tmpdir = tempfile.mkdtemp(prefix="cutsensei-export-")
    audio_path = os.path.join(tmpdir, "audio.f32")
    script_path = os.path.join(tmpdir, "filter.txt")
    tmp_output = os.path.join(out_dir, f".{os.path.basename(output_path)}.part.mp4")
    sr = int(media.audio_rate) if media.audio_rate in (32000, 44100, 48000, 88200, 96000) \
        else 48000
    channels = 2 if media.audio_channels >= 2 else 1
    has_audio = media.has_audio
    audio_share = 0.15 if has_audio else 0.0
    try:
        if has_audio:
            prog(0.0, "audio")
            with open(audio_path, "wb") as fh:
                render_audio(media, edit, fh, sr, channels,
                             progress=lambda f: prog(audio_share * f, "audio"), cancel=cancel)
        cancel.check()
        frame_eps = 0.5 / max(1.0, media.fps)
        graph = build_video_filter(edit, fps_str, scale, frame_eps)
        with open(script_path, "w", encoding="utf-8") as fh:
            fh.write(graph)

        def run(enc: str, hw: bool) -> str:
            args: List[str] = ["-y"]
            args += ffmpeg.hwaccel_args(hw)
            args += ["-i", media.path]
            if has_audio:
                args += ["-f", "f32le", "-ar", str(sr), "-ac", str(channels), "-i", audio_path]
            args += ffmpeg.filter_complex_args(script_path)
            args += ["-map", "[vout]"]
            if has_audio:
                args += ["-map", "1:a", "-c:a", "aac", "-b:a", f"{int(settings.audio_bitrate)}k"]
            args += encoder_args(enc, settings.quality, width, height, fps_val)
            args += ["-t", f"{edit.out_duration:.6f}", "-movflags", "+faststart",
                     "-map_metadata", "-1", "-f", "mp4", tmp_output]
            return ffmpeg.run_with_progress(
                args, edit.out_duration,
                on_progress=lambda f: prog(audio_share + (1 - audio_share) * f, "video"),
                cancel=cancel)

        log = ""
        try:
            log = run(encoder, settings.hw_decode)
        except Cancelled:
            raise
        except FFmpegError as first:
            # retry: without hw decoding, then with the software encoder
            attempts = []
            if settings.hw_decode:
                attempts.append((encoder, False))
            if encoder not in software_fallbacks():
                sw = next((e for e in software_fallbacks()
                           if e in ffmpeg.available_encoders()), None)
                if sw:
                    attempts.append((sw, False))
            last: Exception = first
            for enc, hw in attempts:
                try:
                    log = run(enc, hw)
                    if enc != encoder:
                        note = f"{encoder} failed, used {enc}"
                        encoder = enc
                    break
                except Cancelled:
                    raise
                except FFmpegError as exc:
                    last = exc
            else:
                raise last
        os.replace(tmp_output, output_path)
        prog(1.0, "done")
        return ExportResult(output_path, edit.out_duration, encoder, note, log)
    finally:
        for p in (audio_path, script_path, tmp_output):
            try:
                os.unlink(p)
            except OSError:
                pass
        try:
            os.rmdir(tmpdir)
        except OSError:
            pass
