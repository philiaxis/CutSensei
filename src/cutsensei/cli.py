"""Command line interface (``cutsensei-cli``).

Examples::

    cutsensei-cli info
    cutsensei-cli auto lecture.mp4 -o lecture_edited.mp4 --speed 4
    cutsensei-cli analyze lecture.mp4 -o lecture.cutsensei --board 0.05,0.1,0.9,0.6
    cutsensei-cli export lecture.cutsensei -o lecture_edited.mp4 --encoder h264_nvenc
    cutsensei-cli demo demo_lecture.mp4
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import List, Optional

from . import __version__
from .core.errors import Cancelled, CutSenseiError, FFmpegError


def _fmt_time(sec: float) -> str:
    sec = max(0.0, sec)
    h = int(sec // 3600)
    m = int(sec % 3600 // 60)
    s = sec % 60
    return f"{h}:{m:02d}:{s:05.2f}" if h else f"{m}:{s:05.2f}"


class _Bar:
    def __init__(self, label: str, quiet: bool = False) -> None:
        self.label = label
        self.quiet = quiet or not sys.stderr.isatty()
        self.last = -1
        self.start = time.time()

    def __call__(self, fraction: float, _msg: str = "") -> None:
        pct = int(fraction * 100)
        if pct == self.last:
            return
        self.last = pct
        if self.quiet:
            if pct % 10 == 0:
                print(f"{self.label}: {pct}%", file=sys.stderr, flush=True)
            return
        width = 30
        filled = int(width * fraction)
        bar = "#" * filled + "-" * (width - filled)
        print(f"\r{self.label} [{bar}] {pct:3d}%", end="", file=sys.stderr, flush=True)
        if pct >= 100:
            print(file=sys.stderr)


def _parse_board(values: Optional[List[str]]) -> List[List[float]]:
    regions = []
    for v in values or []:
        parts = [float(x) for x in v.replace(" ", "").split(",")]
        if len(parts) != 4 or not all(0.0 <= x <= 1.0 for x in parts):
            raise SystemExit(f"--board expects x,y,w,h as fractions (0..1): {v}")
        regions.append(parts)
    return regions


def _add_auto_args(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("automatic editing")
    g.add_argument("--speed", type=float, default=None, help="board writing speed (default 4)")
    g.add_argument("--aggressiveness", type=int, default=None,
                   help="0 (conservative) .. 100 (aggressive), default 50")
    g.add_argument("--pad-before", type=float, default=None, help="margin before speech (s)")
    g.add_argument("--pad-after", type=float, default=None, help="margin after speech (s)")
    g.add_argument("--speed-audio", choices=["keep", "volume", "mute"], default=None,
                   help="audio of sped-up parts")
    g.add_argument("--min-cut", type=float, default=None,
                   help="shortest idle time that is removed (s)")
    g.add_argument("--vad", choices=["auto", "silero", "dsp"], default=None,
                   help="speech detector")
    g.add_argument("--board", action="append", metavar="X,Y,W,H",
                   help="board region as fractions of the frame (repeatable)")
    g.add_argument("--hw-decode", action="store_true", help="use GPU video decoding")


def _add_export_args(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("export")
    g.add_argument("--height", type=int, default=0, help="output height (0 = source)")
    g.add_argument("--fps", type=float, default=0.0, help="output frame rate (0 = source)")
    g.add_argument("--quality", type=int, default=2, choices=[0, 1, 2, 3],
                   help="0 small file .. 3 best (default 2)")
    g.add_argument("--encoder", default="auto",
                   help="auto, libx264, libx265, h264_nvenc, hevc_nvenc, h264_qsv, "
                        "h264_amf, h264_videotoolbox")
    g.add_argument("--audio-bitrate", type=int, default=192, help="AAC bitrate (kbit/s)")


def _apply_auto_args(settings, args) -> None:
    if args.speed is not None:
        settings.writing_speed = max(1.0, args.speed)
    if args.aggressiveness is not None:
        settings.aggressiveness = max(0, min(100, args.aggressiveness))
    if args.pad_before is not None:
        settings.pad_before = max(0.0, args.pad_before)
    if args.pad_after is not None:
        settings.pad_after = max(0.0, args.pad_after)
    if args.speed_audio is not None:
        settings.speed_audio = args.speed_audio
    if args.min_cut is not None:
        settings.min_cut = max(0.1, args.min_cut)
    if args.vad is not None:
        settings.vad_engine = args.vad


def _print_summary(project) -> None:
    st = project.timeline.stats(project.settings)
    print(f"source      : {_fmt_time(st['source'])}")
    print(f"edited      : {_fmt_time(st['output'])}  (-{st['reduction'] * 100:.1f}%)")
    print(f"sped up     : {_fmt_time(st['sped_source'])} -> {_fmt_time(st['sped_output'])}")
    print(f"removed     : {_fmt_time(st['cut'])}")
    print(f"to review   : {int(st['review'])}")


def _analyze(input_path: str, args, quiet: bool):
    from .analysis.classifier import regenerate
    from .analysis.pipeline import analyze
    from .core.media import probe
    from .core.project import Project

    media = probe(input_path)
    project = Project(media)
    _apply_auto_args(project.settings, args)
    project.board_regions = _parse_board(args.board)
    project.export.hw_decode = bool(args.hw_decode)
    project.analysis = analyze(media, project.board_regions, project.settings.vad_engine,
                               progress=_Bar("analyze", quiet), hw_decode=args.hw_decode)
    project.timeline = regenerate(project.timeline, project.analysis, project.settings)
    project.auto_applied = True
    return project


def _export(project, output: str, args, quiet: bool) -> None:
    from .core.settings import ExportSettings
    from .render.exporter import export_video

    ex = ExportSettings(height=args.height, fps=args.fps, quality=args.quality,
                        encoder=args.encoder, audio_bitrate=args.audio_bitrate,
                        hw_decode=bool(getattr(args, "hw_decode", False)))
    res = export_video(project.media, project.timeline, project.settings, ex, output,
                       progress=_Bar("export", quiet))
    note = f" ({res.fallback_reason})" if res.fallback_reason else ""
    print(f"exported    : {res.path}  [{_fmt_time(res.duration)}, {res.encoder}{note}]")


def cmd_info(_args) -> int:
    from .analysis.audio import silero_available
    from .core import ffmpeg
    from .render.exporter import detect_available_encoders

    print(f"CutSensei {__version__}  (Python {sys.version.split()[0]}, {sys.platform})")
    try:
        exe = ffmpeg.find_ffmpeg()
        print(f"ffmpeg      : {exe}  (version {'.'.join(map(str, ffmpeg.ffmpeg_version()))})")
    except CutSenseiError as exc:
        print(f"ffmpeg      : NOT FOUND - {exc}")
        return 1
    print(f"ffprobe     : {ffmpeg.find_ffprobe() or 'not found (using ffmpeg fallback)'}")
    print(f"hwaccels    : {', '.join(ffmpeg.available_hwaccels()) or '-'}")
    print(f"encoders    : {', '.join(detect_available_encoders())}")
    print(f"silero VAD  : {'available' if silero_available() else 'not available (DSP fallback)'}")
    try:
        import onnxruntime as ort

        print(f"onnxruntime : {ort.__version__} {ort.get_available_providers()}")
    except Exception:
        pass
    return 0


def cmd_analyze(args) -> int:
    project = _analyze(args.input, args, args.quiet)
    out = args.output or os.path.splitext(args.input)[0] + ".cutsensei"
    project.save(out)
    _print_summary(project)
    print(f"project     : {project.path}")
    return 0


def cmd_export(args) -> int:
    from .core.project import Project

    project = Project.load(args.project, media_override=args.media)
    if not project.media_exists:
        raise CutSenseiError(f"Source video not found: {project.media.path} (use --media)")
    _export(project, args.output, args, args.quiet)
    return 0


def cmd_auto(args) -> int:
    project = _analyze(args.input, args, args.quiet)
    _print_summary(project)
    if args.save_project:
        project.save(args.save_project)
        print(f"project     : {project.path}")
    out = args.output or os.path.splitext(args.input)[0] + "_edited.mp4"
    _export(project, out, args, args.quiet)
    return 0


def cmd_segments(args) -> int:
    from .core.project import Project

    project = Project.load(args.project)
    if args.json:
        json.dump(project.timeline.to_list(), sys.stdout, indent=1)
        print()
        return 0
    for seg in project.timeline:
        speed = seg.speed_value(project.settings)
        sp = "cut" if seg.action == "cut" else f"{speed:g}x"
        flags = "".join(f for f, on in (("P", seg.protected), ("M", seg.manual),
                                        ("R", seg.review)) if on)
        print(f"{_fmt_time(seg.start):>10} - {_fmt_time(seg.end):>10}  {seg.kind:9s} "
              f"{sp:>5}  {seg.reason:18s} {flags}")
    _print_summary(project)
    return 0


def cmd_demo(args) -> int:
    from .demo import DemoSpec, Scene, generate_demo

    spec = DemoSpec()
    if args.scenario == "mixed":
        spec = DemoSpec(scenes=[Scene("speech", 15), Scene("speech_writing", 15),
                                Scene("writing", 20), Scene("walk", 8), Scene("idle", 12),
                                Scene("speech", 10), Scene("writing", 10), Scene("idle", 6),
                                Scene("speech", 8)])
    generate_demo(args.output, spec)
    print(f"demo video  : {args.output}  ({_fmt_time(spec.duration)})")
    for kind, a, b in spec.scene_ranges():
        print(f"  {_fmt_time(a):>8} - {_fmt_time(b):>8}  {kind}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="cutsensei-cli",
                                description="Automatic editing for lecture videos.")
    p.add_argument("--version", action="version", version=f"CutSensei {__version__}")
    p.add_argument("--ffmpeg", help="path of the ffmpeg executable")
    p.add_argument("-q", "--quiet", action="store_true", help="less progress output")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("info", help="show FFmpeg / GPU / encoder information")
    s.set_defaults(func=cmd_info)

    s = sub.add_parser("analyze", help="analyse a video and save a project")
    s.add_argument("input")
    s.add_argument("-o", "--output", help="project file (.cutsensei)")
    _add_auto_args(s)
    s.set_defaults(func=cmd_analyze)

    s = sub.add_parser("export", help="export a project to MP4")
    s.add_argument("project")
    s.add_argument("-o", "--output", required=True)
    s.add_argument("--media", help="new location of the source video")
    s.add_argument("--hw-decode", action="store_true")
    _add_export_args(s)
    s.set_defaults(func=cmd_export)

    s = sub.add_parser("auto", help="analyse and export in one step")
    s.add_argument("input")
    s.add_argument("-o", "--output")
    s.add_argument("--save-project", metavar="FILE", help="also save the project")
    _add_auto_args(s)
    _add_export_args(s)
    s.set_defaults(func=cmd_auto)

    s = sub.add_parser("segments", help="list the segments of a project")
    s.add_argument("project")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_segments)

    s = sub.add_parser("demo", help="generate a synthetic lecture video for testing")
    s.add_argument("output")
    s.add_argument("--scenario", choices=["standard", "mixed"], default="standard")
    s.set_defaults(func=cmd_demo)
    return p


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.ffmpeg:
        from .core import ffmpeg

        ffmpeg.set_ffmpeg_path(args.ffmpeg)
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        print("\ncancelled", file=sys.stderr)
        return 130
    except Cancelled:
        print("cancelled", file=sys.stderr)
        return 130
    except FFmpegError as exc:
        print(f"error: {exc}\n{exc.stderr[-2000:]}", file=sys.stderr)
        return 2
    except CutSenseiError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
