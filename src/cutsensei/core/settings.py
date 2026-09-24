"""User adjustable settings for automatic editing and export."""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from typing import Any, Dict, Optional


def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


class SpeedAudio:
    """What to do with the audio of sped-up (board writing) segments."""
    KEEP = "keep"        # keep at full volume (time-stretched, pitch preserved)
    VOLUME = "volume"    # keep but at a reduced volume
    MUTE = "mute"

    ALL = (KEEP, VOLUME, MUTE)


class SourceType:
    """What kind of recording the lecture is."""
    AUTO = "auto"        # detected from the video
    CAMERA = "camera"    # camera filming a blackboard / whiteboard / electronic board
    SCREEN = "screen"    # screen recording of digital notes (GoodNotes, OneNote, ...)

    ALL = (AUTO, CAMERA, SCREEN)


class VadEngine:
    AUTO = "auto"        # Silero (neural) when available, otherwise DSP
    SILERO = "silero"
    DSP = "dsp"

    ALL = (AUTO, SILERO, DSP)


@dataclass
class AutoEditSettings:
    # --- main settings -----------------------------------------------------
    writing_speed: float = 4.0           # playback speed of silent board writing
    aggressiveness: int = 50             # 0 = conservative ... 100 = aggressive
    pad_before: float = 0.25             # protective margin before speech (s)
    pad_after: float = 0.40              # protective margin after speech (s)
    speed_audio: str = SpeedAudio.KEEP
    speed_audio_volume: float = 0.4      # used with SpeedAudio.VOLUME
    source_type: str = SourceType.AUTO   # camera or screen recording (needs re-analysis)

    # --- advanced settings (None = derived from aggressiveness) -------------
    min_cut: Optional[float] = None          # shortest idle span that is removed (s)
    min_speed: Optional[float] = None        # shortest writing span that is sped up (s)
    speech_gap_fill: Optional[float] = None  # pauses inside speech kept as speech (s)
    writing_gap_fill: Optional[float] = None # pauses inside writing merged (s)
    hold_after_writing: Optional[float] = None  # show the finished board (s)
    cut_margin: Optional[float] = None       # idle kept at both sides of a cut (s)
    speech_threshold: Optional[float] = None  # 0..1, speech probability threshold
    writing_threshold: Optional[float] = None  # 0..1, writing score threshold
    review_band: Optional[float] = None      # width of the "uncertain" zone
    motion_is_uncertain: bool = True         # silent movement without writing -> review
    vad_engine: str = VadEngine.AUTO

    # --- derived values ------------------------------------------------------
    @property
    def _t(self) -> float:
        return min(100, max(0, int(self.aggressiveness))) / 100.0

    def _pick(self, value: Optional[float], conservative: float, aggressive: float) -> float:
        if value is not None:
            return float(value)
        return _lerp(conservative, aggressive, self._t)

    @property
    def eff_min_cut(self) -> float:
        return self._pick(self.min_cut, 5.0, 0.8)

    @property
    def eff_min_speed(self) -> float:
        return self._pick(self.min_speed, 3.0, 1.2)

    @property
    def eff_speech_gap_fill(self) -> float:
        return self._pick(self.speech_gap_fill, 1.2, 0.5)

    @property
    def eff_writing_gap_fill(self) -> float:
        return self._pick(self.writing_gap_fill, 3.0, 1.5)

    @property
    def eff_hold_after_writing(self) -> float:
        return self._pick(self.hold_after_writing, 2.0, 0.5)

    @property
    def eff_cut_margin(self) -> float:
        return self._pick(self.cut_margin, 0.5, 0.1)

    @property
    def eff_speech_threshold(self) -> float:
        return self._pick(self.speech_threshold, 0.40, 0.55)

    @property
    def eff_writing_threshold(self) -> float:
        return self._pick(self.writing_threshold, 0.30, 0.45)

    @property
    def eff_review_band(self) -> float:
        return self._pick(self.review_band, 0.18, 0.06)

    def speed_segment_volume(self) -> float:
        if self.speed_audio == SpeedAudio.MUTE:
            return 0.0
        if self.speed_audio == SpeedAudio.VOLUME:
            return max(0.0, min(2.0, float(self.speed_audio_volume)))
        return 1.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "AutoEditSettings":
        obj = cls()
        if not data:
            return obj
        for f in fields(cls):
            if f.name in data:
                setattr(obj, f.name, data[f.name])
        obj.writing_speed = max(1.0, min(64.0, float(obj.writing_speed)))
        if obj.speed_audio not in SpeedAudio.ALL:
            obj.speed_audio = SpeedAudio.KEEP
        if obj.vad_engine not in VadEngine.ALL:
            obj.vad_engine = VadEngine.AUTO
        if obj.source_type not in SourceType.ALL:
            obj.source_type = SourceType.AUTO
        return obj


class Encoder:
    AUTO = "auto"
    X264 = "libx264"
    X265 = "libx265"
    NVENC_H264 = "h264_nvenc"
    NVENC_HEVC = "hevc_nvenc"
    QSV_H264 = "h264_qsv"
    AMF_H264 = "h264_amf"
    VT_H264 = "h264_videotoolbox"
    VT_HEVC = "hevc_videotoolbox"

    HARDWARE_H264 = (NVENC_H264, QSV_H264, AMF_H264, VT_H264)
    ALL = (AUTO, X264, X265, NVENC_H264, NVENC_HEVC, QSV_H264, AMF_H264, VT_H264, VT_HEVC)


@dataclass
class ExportSettings:
    height: int = 0            # 0 = keep source resolution
    fps: float = 0.0           # 0 = keep source frame rate
    quality: int = 2           # 0 = small file ... 3 = best
    encoder: str = Encoder.AUTO
    audio_bitrate: int = 192   # kbit/s
    hw_decode: bool = False    # use -hwaccel auto for decoding
    last_dir: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "ExportSettings":
        obj = cls()
        if not data:
            return obj
        for f in fields(cls):
            if f.name in data:
                setattr(obj, f.name, data[f.name])
        if obj.encoder not in Encoder.ALL:
            obj.encoder = Encoder.AUTO
        obj.quality = max(0, min(3, int(obj.quality)))
        return obj
