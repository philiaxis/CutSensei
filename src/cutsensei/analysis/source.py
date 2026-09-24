"""Tell a camera recording (blackboard / whiteboard in a classroom) from a
screen recording of a tablet or PC (GoodNotes, Notability, OneNote, a
digital whiteboard app, annotated slides, ...).

A handful of short frame pairs spread over the video are decoded at (almost)
native resolution with nearest-neighbour scaling, so that pixel statistics
are not smoothed:

* **Screen content** consists of exactly flat areas (paper, app background),
  sharp synthetic edges and one dominating colour; nothing changes between
  two frames unless something is drawn or moved.
* **Camera images** carry sensor noise and fine texture everywhere, even
  after compression, and the noise changes from frame to frame.

In a screen recording, regions that look like camera images *and* keep
changing (a picture-in-picture webcam of the lecturer, a playing video) are
reported so the writing detection can ignore them.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np

from ..core import ffmpeg
from ..core.jobs import CancelToken
from ..core.media import MediaInfo

CAMERA = "camera"
SCREEN = "screen"

DETECT_MAX = 960      # longest side of the sampled frames
BLOCK = 8             # statistics are gathered on 8x8 pixel blocks
SAMPLES = 10


@dataclass
class SourceDetection:
    kind: str = CAMERA
    confidence: float = 0.0
    live_rects: List[List[float]] = field(default_factory=list)  # normalized x, y, w, h
    stats: Dict[str, float] = field(default_factory=dict)

    def to_meta(self) -> Dict[str, object]:
        return {"source_detected": self.kind, "source_confidence": round(self.confidence, 3),
                "live_rects": [[round(v, 4) for v in r] for r in self.live_rects],
                "source_stats": {k: round(v, 4) for k, v in self.stats.items()}}


def _size(media: MediaInfo) -> Tuple[int, int]:
    w, h = max(2, media.width), max(2, media.height)
    scale = min(1.0, DETECT_MAX / max(w, h))
    return max(16, int(w * scale) // 2 * 2), max(16, int(h * scale) // 2 * 2)


def sample_times(duration: float, samples: int = SAMPLES) -> List[float]:
    if duration <= 2.0:
        return [0.0]
    k = int(min(samples, max(1, duration // 6)))
    return [min(duration * (i + 0.5) / k, max(0.0, duration - 1.0)) for i in range(k)]


def _grab_pair(media: MediaInfo, t: float, size: Tuple[int, int]) -> List[np.ndarray]:
    """Two frames 0.5 s apart starting at ``t`` (fewer if the file has none)."""
    w, h = size
    args = [ffmpeg.find_ffmpeg(), "-hide_banner", "-loglevel", "error", "-nostdin",
            "-ss", f"{t:.3f}", "-i", media.path, "-an", "-sn", "-dn", "-t", "0.8",
            "-vf", f"fps=2,scale={w}:{h}:flags=neighbor,format=gray", "-frames:v", "2",
            "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1"]
    try:
        data = ffmpeg.run(args, timeout=120, check=False).stdout
    except Exception:
        return []
    n = len(data) // (w * h)
    return [np.frombuffer(data[i * w * h:(i + 1) * w * h], np.uint8).reshape(h, w)
            for i in range(n)]


def _blocks(a: np.ndarray) -> np.ndarray:
    h, w = a.shape[0] // BLOCK * BLOCK, a.shape[1] // BLOCK * BLOCK
    return a[:h, :w].reshape(h // BLOCK, BLOCK, w // BLOCK, BLOCK)


@dataclass
class FrameStats:
    noisy: np.ndarray       # bool per block: camera-like noise / fine texture
    textured: np.ndarray    # bool per block: mostly non-flat (noise of any strength)
    flat: np.ndarray        # bool per block: exactly one value
    top_fraction: float     # share of the five most frequent grey values
    noisy_fraction: float
    flat_fraction: float


def frame_stats(g: np.ndarray) -> FrameStats:
    import cv2

    k = np.ones((3, 3), np.uint8)
    rng = cv2.dilate(g, k).astype(np.int16) - cv2.erode(g, k).astype(np.int16)
    lap = np.abs(cv2.Laplacian(g, cv2.CV_16S, ksize=1))
    subtle = (lap >= 1) & (lap <= 12)
    noisy = _blocks(subtle).mean(axis=(1, 3)) >= 0.5
    textured = _blocks(lap >= 1).mean(axis=(1, 3)) >= 0.5
    flat = _blocks(rng).max(axis=(1, 3)) == 0
    hist = np.bincount(g.ravel(), minlength=256)
    top = float(np.sort(hist)[-5:].sum()) / g.size
    return FrameStats(noisy, textured, flat, top, float(noisy.mean()), float(flat.mean()))


def screen_score(st: FrameStats, temporal_noise: Optional[float]) -> float:
    """0 = looks like a camera image, 1 = looks like screen content."""
    parts = [
        np.clip((0.30 - st.noisy_fraction) / 0.25, 0, 1),
        np.clip((st.flat_fraction - 0.10) / 0.30, 0, 1),
        np.clip((st.top_fraction - 0.40) / 0.35, 0, 1),
    ]
    if temporal_noise is not None:
        parts.append(np.clip((0.20 - temporal_noise) / 0.18, 0, 1))
    return float(np.mean(parts))


def _live_rects(live: np.ndarray, size: Tuple[int, int]) -> List[List[float]]:
    """Bounding rectangles (normalized, padded) of connected live blocks."""
    import cv2

    if not live.any():
        return []
    k = np.ones((3, 3), np.uint8)
    m = cv2.morphologyEx(live.astype(np.uint8), cv2.MORPH_CLOSE, k)
    n, _labels, stats, _ = cv2.connectedComponentsWithStats(m, 8)
    w, h = size
    min_blocks = max(12, int(0.004 * m.size))
    rects = []
    for i in range(1, n):
        x, y, bw, bh, area = stats[i]
        if area < min_blocks:
            continue
        x0, y0 = max(0, x - 2) * BLOCK, max(0, y - 2) * BLOCK
        x1 = min(w, (x + bw + 2) * BLOCK)
        y1 = min(h, (y + bh + 2) * BLOCK)
        rects.append([x0 / w, y0 / h, (x1 - x0) / w, (y1 - y0) / h])
    return rects


def detect_source(media: MediaInfo, cancel: Optional[CancelToken] = None,
                  samples: int = SAMPLES) -> SourceDetection:
    """Decide whether ``media`` is a camera or a screen recording."""
    if media.width <= 0 or media.height <= 0:
        return SourceDetection()
    size = _size(media)
    times = sample_times(media.duration, samples)
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(_grab_pair, media, t, size) for t in times]
        grabbed = []
        for f in futures:
            if cancel is not None and cancel.cancelled:
                for other in futures:
                    other.cancel()
                cancel.check()
            grabbed.append(f.result())

    scores: List[float] = []
    noisy_f, flat_f, top_f, tnoise_f = [], [], [], []
    live_count: Optional[np.ndarray] = None
    pairs = 0
    for frames in grabbed:
        if not frames:
            continue
        st = frame_stats(frames[0])
        tnoise = None
        if len(frames) >= 2:
            d = np.abs(frames[1].astype(np.int16) - frames[0].astype(np.int16))
            tnoise = float(((d >= 1) & (d <= 6)).mean())
            active = _blocks(d >= 1).mean(axis=(1, 3)) >= 0.3
            st1 = frame_stats(frames[1])
            live = st.textured & st1.textured & active
            live_count = live.astype(np.int32) if live_count is None else live_count + live
            pairs += 1
            tnoise_f.append(tnoise)
        scores.append(screen_score(st, tnoise))
        noisy_f.append(st.noisy_fraction)
        flat_f.append(st.flat_fraction)
        top_f.append(st.top_fraction)
    if not scores:
        return SourceDetection()

    score = float(np.median(scores))
    live_rects: List[List[float]] = []
    live_fraction = 0.0
    if live_count is not None and pairs >= 2:
        live = live_count >= max(2, int(np.ceil(0.5 * pairs)))
        live_fraction = float(live.mean())
        live_rects = _live_rects(live, size)
    top = float(np.median(top_f))
    # Screen content is dominated by a few exact colours (paper, background);
    # uneven lighting and texture spread a camera image over many values.
    kind = SCREEN if score >= 0.6 and top >= 0.55 and live_fraction < 0.5 else CAMERA
    stats = {"score": score, "noisy": float(np.median(noisy_f)),
             "flat": float(np.median(flat_f)), "top_colours": top,
             "temporal_noise": float(np.median(tnoise_f)) if tnoise_f else -1.0,
             "live": live_fraction, "samples": float(len(scores))}
    confidence = float(min(1.0, abs(score - 0.5) * 2.0))
    return SourceDetection(kind, confidence, live_rects if kind == SCREEN else [], stats)
