"""Container for the per-frame analysis features of a video."""

from __future__ import annotations

import base64
import io
import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import numpy as np

ANALYSIS_VERSION = 1
HOP = 0.1  # seconds per analysis frame (10 Hz)

_ARRAY_FIELDS = ("speech", "voicing", "loudness", "clicks", "ink", "motion", "hand",
                 "global_change", "wave_peaks")


@dataclass
class AnalysisResult:
    duration: float
    hop: float = HOP
    # audio features (length = n_frames)
    speech: np.ndarray = field(default_factory=lambda: np.zeros(0, np.float32))
    voicing: np.ndarray = field(default_factory=lambda: np.zeros(0, np.float32))
    loudness: np.ndarray = field(default_factory=lambda: np.zeros(0, np.float32))
    clicks: np.ndarray = field(default_factory=lambda: np.zeros(0, np.float32))
    # video features (length = n_frames)
    ink: np.ndarray = field(default_factory=lambda: np.zeros(0, np.float32))
    motion: np.ndarray = field(default_factory=lambda: np.zeros(0, np.float32))
    hand: np.ndarray = field(default_factory=lambda: np.zeros(0, np.float32))
    global_change: np.ndarray = field(default_factory=lambda: np.zeros(0, np.float32))
    # waveform overview for the timeline (uint8, ``wave_rate`` values per second)
    wave_peaks: np.ndarray = field(default_factory=lambda: np.zeros(0, np.uint8))
    wave_rate: int = 100
    has_audio: bool = True
    has_video_features: bool = True
    board_regions: List[List[float]] = field(default_factory=list)
    vad_engine: str = ""
    thumb_interval: float = 0.0
    thumb_count: int = 0
    thumb_fingerprint: str = ""
    version: int = ANALYSIS_VERSION
    meta: Dict[str, Any] = field(default_factory=dict)

    @property
    def n_frames(self) -> int:
        return int(len(self.speech))

    @staticmethod
    def frames_for(duration: float, hop: float = HOP) -> int:
        return max(1, int(np.ceil(duration / hop)))

    def times(self) -> np.ndarray:
        return (np.arange(self.n_frames, dtype=np.float64) + 0.5) * self.hop

    def ensure_lengths(self) -> None:
        n = self.frames_for(self.duration, self.hop)
        for name in _ARRAY_FIELDS:
            if name == "wave_peaks":
                continue
            arr = getattr(self, name)
            if len(arr) < n:
                arr = np.concatenate([arr, np.zeros(n - len(arr), np.float32)])
            setattr(self, name, np.asarray(arr[:n], dtype=np.float32))

    # ---------------------------------------------------------------- storage
    def to_bytes(self) -> bytes:
        meta = {
            "duration": self.duration, "hop": self.hop, "wave_rate": self.wave_rate,
            "has_audio": self.has_audio, "has_video_features": self.has_video_features,
            "board_regions": self.board_regions, "vad_engine": self.vad_engine,
            "thumb_interval": self.thumb_interval, "thumb_count": self.thumb_count,
            "thumb_fingerprint": self.thumb_fingerprint, "version": self.version,
            "meta": self.meta,
        }
        buf = io.BytesIO()
        arrays = {name: getattr(self, name) for name in _ARRAY_FIELDS}
        arrays = {k: (v.astype(np.float16) if v.dtype == np.float32 else v)
                  for k, v in arrays.items()}
        np.savez_compressed(buf, __meta__=np.frombuffer(json.dumps(meta).encode("utf-8"),
                                                        dtype=np.uint8), **arrays)
        return buf.getvalue()

    @classmethod
    def from_bytes(cls, data: bytes) -> "AnalysisResult":
        with np.load(io.BytesIO(data), allow_pickle=False) as npz:
            meta = json.loads(bytes(npz["__meta__"]).decode("utf-8"))
            arrays = {name: np.array(npz[name]) for name in _ARRAY_FIELDS if name in npz}
        res = cls(duration=float(meta["duration"]))
        for key in ("hop", "wave_rate", "has_audio", "has_video_features", "board_regions",
                    "vad_engine", "thumb_interval", "thumb_count", "thumb_fingerprint",
                    "version", "meta"):
            if key in meta:
                setattr(res, key, meta[key])
        for name, arr in arrays.items():
            if name == "wave_peaks":
                setattr(res, name, arr.astype(np.uint8))
            else:
                setattr(res, name, arr.astype(np.float32))
        res.ensure_lengths()
        return res

    def to_b64(self) -> str:
        return base64.b64encode(self.to_bytes()).decode("ascii")

    @classmethod
    def from_b64(cls, text: str) -> "AnalysisResult":
        return cls.from_bytes(base64.b64decode(text.encode("ascii")))

    def region_signature(self) -> str:
        return json.dumps([[round(v, 4) for v in r] for r in self.board_regions])


def regions_signature(regions: Optional[List[List[float]]]) -> str:
    return json.dumps([[round(v, 4) for v in r] for r in (regions or [])])
