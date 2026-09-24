"""Media probing (duration, resolution, frame rate, audio presence)."""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Any, Dict, Optional

from . import ffmpeg
from .errors import CutSenseiError, FFmpegError


@dataclass
class MediaInfo:
    path: str
    duration: float
    has_video: bool = True
    has_audio: bool = True
    width: int = 0          # display width (rotation applied)
    height: int = 0         # display height (rotation applied)
    fps: float = 30.0
    fps_fraction: str = "30/1"
    rotation: int = 0
    video_codec: str = ""
    audio_codec: str = ""
    audio_rate: int = 48000
    audio_channels: int = 2
    start_time: float = 0.0
    bit_rate: int = 0
    size: int = 0
    mtime: float = 0.0
    extra: Dict[str, Any] = field(default_factory=dict)

    @property
    def frame_duration(self) -> float:
        return 1.0 / self.fps if self.fps > 0 else 1.0 / 30.0

    def fingerprint(self) -> str:
        key = f"{os.path.basename(self.path)}|{self.size}|{int(self.mtime)}|{self.duration:.3f}"
        return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MediaInfo":
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known)


def _parse_rate(text: Optional[str]) -> Optional[Fraction]:
    if not text or text in ("0/0", "0"):
        return None
    try:
        fr = Fraction(text)
    except (ValueError, ZeroDivisionError):
        return None
    if fr <= 0 or fr > 1000:
        return None
    return fr


def _rotation_from_stream(stream: Dict[str, Any]) -> int:
    rot = 0
    tags = stream.get("tags") or {}
    if "rotate" in tags:
        try:
            rot = int(float(tags["rotate"]))
        except ValueError:
            rot = 0
    for side in stream.get("side_data_list") or []:
        if "rotation" in side:
            try:
                rot = -int(float(side["rotation"]))
            except (TypeError, ValueError):
                pass
    return rot % 360


def probe(path: str) -> MediaInfo:
    """Probe a media file with ffprobe, falling back to parsing ``ffmpeg -i``."""
    if not os.path.isfile(path):
        raise CutSenseiError(f"File not found: {path}")
    probe_exe = ffmpeg.find_ffprobe()
    info: Optional[MediaInfo] = None
    if probe_exe:
        try:
            info = _probe_ffprobe(probe_exe, path)
        except (FFmpegError, ValueError, KeyError, json.JSONDecodeError):
            info = None
    if info is None:
        info = _probe_ffmpeg(path)
    st = os.stat(path)
    info.size = st.st_size
    info.mtime = st.st_mtime
    if info.duration <= 0:
        raise CutSenseiError("Could not determine the duration of the media file.")
    if not info.has_video:
        raise CutSenseiError("The file does not contain a video stream.")
    return info


def _probe_ffprobe(exe: str, path: str) -> MediaInfo:
    out = ffmpeg.run([exe, "-v", "error", "-print_format", "json", "-show_format",
                      "-show_streams", path], timeout=60).stdout
    data = json.loads(out.decode("utf-8", "replace"))
    fmt = data.get("format", {})
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"
                  and not (s.get("disposition") or {}).get("attached_pic")), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    duration = float(fmt.get("duration") or 0.0)
    if duration <= 0 and video is not None:
        duration = float(video.get("duration") or 0.0)
    info = MediaInfo(path=os.path.abspath(path), duration=duration)
    info.start_time = float(fmt.get("start_time") or 0.0)
    info.bit_rate = int(fmt.get("bit_rate") or 0)
    info.has_video = video is not None
    info.has_audio = audio is not None
    if video is not None:
        rate = _parse_rate(video.get("avg_frame_rate")) or _parse_rate(video.get("r_frame_rate"))
        if rate is None:
            rate = Fraction(30, 1)
        info.fps = float(rate)
        info.fps_fraction = f"{rate.numerator}/{rate.denominator}"
        info.rotation = _rotation_from_stream(video)
        w, h = int(video.get("width") or 0), int(video.get("height") or 0)
        if info.rotation in (90, 270):
            w, h = h, w
        info.width, info.height = w, h
        info.video_codec = video.get("codec_name", "")
    if audio is not None:
        info.audio_codec = audio.get("codec_name", "")
        info.audio_rate = int(audio.get("sample_rate") or 48000)
        info.audio_channels = int(audio.get("channels") or 2)
    return info


_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_START_RE = re.compile(r"start:\s*(-?\d+(?:\.\d+)?)")
_VIDEO_RE = re.compile(r"Stream #\d+:\d+.*?: Video: (\w+).*?, (\d{2,5})x(\d{2,5})")
_FPS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:fps|tbr)")
_AUDIO_RE = re.compile(r"Stream #\d+:\d+.*?: Audio: (\w+).*?, (\d+) Hz, ([^,]+)")
_ROTATE_RE = re.compile(r"(?:rotate\s*:\s*(-?\d+))|(?:rotation of (-?\d+(?:\.\d+)?) degrees)")


def _probe_ffmpeg(path: str) -> MediaInfo:
    proc = ffmpeg.run([ffmpeg.find_ffmpeg(), "-hide_banner", "-i", path], timeout=60,
                      check=False)
    text = proc.stderr.decode("utf-8", "replace")
    return parse_ffmpeg_banner(text, path)


def parse_ffmpeg_banner(text: str, path: str) -> MediaInfo:
    info = MediaInfo(path=os.path.abspath(path), duration=0.0, has_video=False,
                     has_audio=False)
    m = _DURATION_RE.search(text)
    if m:
        h, mnt, s = m.groups()
        info.duration = int(h) * 3600 + int(mnt) * 60 + float(s)
    m = _START_RE.search(text)
    if m:
        info.start_time = float(m.group(1))
    for line in text.splitlines():
        if not info.has_video and ": Video:" in line and "attached pic" not in line:
            vm = _VIDEO_RE.search(line)
            if vm:
                info.has_video = True
                info.video_codec = vm.group(1)
                info.width, info.height = int(vm.group(2)), int(vm.group(3))
                fm = _FPS_RE.search(line)
                if fm:
                    info.fps = float(fm.group(1))
                    fr = Fraction(info.fps).limit_denominator(1001)
                    info.fps_fraction = f"{fr.numerator}/{fr.denominator}"
        elif not info.has_audio and ": Audio:" in line:
            am = _AUDIO_RE.search(line)
            if am:
                info.has_audio = True
                info.audio_codec = am.group(1)
                info.audio_rate = int(am.group(2))
                layout = am.group(3).strip()
                if layout == "mono":
                    info.audio_channels = 1
                elif layout == "stereo":
                    info.audio_channels = 2
                else:
                    cm = re.match(r"(\d+)", layout)
                    info.audio_channels = int(cm.group(1)) if cm else 2
    rm = _ROTATE_RE.search(text)
    if rm:
        value = rm.group(1) if rm.group(1) is not None else rm.group(2)
        rot = int(float(value))
        # "displaymatrix: rotation of -90.00 degrees" means rotate by 90 clockwise
        if rm.group(2) is not None:
            rot = -rot
        info.rotation = rot % 360
        if info.rotation in (90, 270):
            info.width, info.height = info.height, info.width
    return info


VIDEO_EXTENSIONS = (".mp4", ".mov", ".m4v", ".mkv", ".avi", ".webm", ".mts", ".m2ts",
                    ".ts", ".wmv", ".flv", ".mpg", ".mpeg", ".3gp")


def is_probably_video(path: str) -> bool:
    return Path(path).suffix.lower() in VIDEO_EXTENSIONS
