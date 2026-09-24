"""Video analysis: detect board writing and tell it apart from movement.

The frames are decoded at a low rate (4 fps) and resolution, restricted to
the board region(s) chosen by the user.  For every frame:

* **motion** - fraction of board pixels that changed since the last frame.
* **ink map** - thin, high contrast structures (chalk or marker strokes)
  extracted with a morphological top-hat (bright ink) or black-hat (dark ink).
* **person mask** - large blobs that differ from a slowly adapting
  background model.  Strokes are thin and are removed by a morphological
  opening; a lecturer standing in front of the board is not.

Board pixels that are visible (not moving, not covered by the person) are
tracked: when a pixel's ink state stays stable for a while and differs from
the previously confirmed state, the board content has changed there
(written or erased).  The change is attributed to the time span during which
the pixel was hidden or changing, so writing behind the lecturer's body is
credited to the moment it happened.  A lecturer who merely walks past the
board produces a lot of motion but no confirmed ink change.

Thumbnails for the timeline are written by the same ffmpeg process.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable, List, Optional, Sequence, Tuple

import numpy as np

from ..core import ffmpeg
from ..core.jobs import CancelToken
from ..core.media import MediaInfo

ANALYSIS_FPS = 4.0
MAX_WIDTH = 512
MAX_HEIGHT = 320
STABLE_FRAMES = 3          # frames an ink change must persist (0.75 s)
MAX_ATTRIBUTION_S = 20.0   # never spread one change over more than this
REVERT_WINDOW_S = 12.0     # a change undone within this time was not ink
EARLY_ATTRIBUTION_S = 2.0
THUMB_WIDTH = 192
BACKGROUND_WARMUP_S = 30.0  # frames used to estimate the empty board


@dataclass
class VideoFeatures:
    fps: float
    ink: np.ndarray            # ink change evidence per analysis frame (normalized)
    motion: np.ndarray         # fraction of board pixels in motion
    hand: np.ndarray           # small localized motion (writing hand) 0..1
    presence: np.ndarray       # fraction of board covered by a foreground blob
    global_change: np.ndarray  # 1 where lighting/camera changed abruptly
    width: int
    height: int
    polarity: int              # +1 bright ink (blackboard), -1 dark ink (whiteboard)


def union_rect(regions: Sequence[Sequence[float]]) -> Tuple[float, float, float, float]:
    if not regions:
        return 0.0, 0.0, 1.0, 1.0
    x0 = min(r[0] for r in regions)
    y0 = min(r[1] for r in regions)
    x1 = max(r[0] + r[2] for r in regions)
    y1 = max(r[1] + r[3] for r in regions)
    x0, y0 = max(0.0, x0), max(0.0, y0)
    x1, y1 = min(1.0, x1), min(1.0, y1)
    if x1 - x0 < 0.02 or y1 - y0 < 0.02:
        return 0.0, 0.0, 1.0, 1.0
    return x0, y0, x1 - x0, y1 - y0


def _even(v: float) -> int:
    return max(2, int(round(v / 2.0)) * 2)


def analysis_geometry(media: MediaInfo, regions: Sequence[Sequence[float]]):
    """Crop rectangle in source pixels and the analysis frame size."""
    x, y, w, h = union_rect(regions)
    W, H = max(2, media.width), max(2, media.height)
    cx, cy = int(round(x * W)), int(round(y * H))
    cw, ch = _even(w * W), _even(h * H)
    cw = min(cw, W - cx - (W - cx) % 2)
    ch = min(ch, H - cy - (H - cy) % 2)
    scale = min(1.0, MAX_WIDTH / cw, MAX_HEIGHT / ch)
    aw, ah = _even(cw * scale), _even(ch * scale)
    return (cx, cy, cw, ch), (aw, ah)


def region_mask(regions: Sequence[Sequence[float]], size: Tuple[int, int]) -> np.ndarray:
    aw, ah = size
    if not regions:
        return np.ones((ah, aw), bool)
    ux, uy, uw, uh = union_rect(regions)
    mask = np.zeros((ah, aw), bool)
    for r in regions:
        x0 = int(round((r[0] - ux) / uw * aw))
        y0 = int(round((r[1] - uy) / uh * ah))
        x1 = int(round((r[0] + r[2] - ux) / uw * aw))
        y1 = int(round((r[1] + r[3] - uy) / uh * ah))
        mask[max(0, y0):min(ah, y1), max(0, x0):min(aw, x1)] = True
    if mask.sum() < 16:
        mask[:] = True
    return mask


class BoardTracker:
    """Frame by frame state machine producing the per-frame features."""

    def __init__(self, size: Tuple[int, int], mask: np.ndarray, fps: float,
                 initial_background: Optional[np.ndarray] = None) -> None:
        import cv2

        self.cv2 = cv2
        self.aw, self.ah = size
        self.mask = mask
        self.area = float(mask.sum())
        self.fps = fps
        k = max(7, int(round(min(self.aw, self.ah) / 28)) | 1)
        self.k_ink = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))
        self.k_open = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7))
        self.k_person = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (15, 15))
        self.k_motion = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (9, 9))
        self.prev: Optional[np.ndarray] = None
        self.prev_motion: Optional[np.ndarray] = None
        self.background: Optional[np.ndarray] = None
        self._initial_bg = initial_background
        self._thr: Optional[float] = None
        self.polarity = 0
        self._pol_samples: List[float] = []
        self.ink_ref: Optional[np.ndarray] = None
        self.cand: Optional[np.ndarray] = None
        self.stable = np.zeros((self.ah, self.aw), np.int16)
        self.last_seen = np.zeros((self.ah, self.aw), np.int32)
        self.frame_index = 0
        self.events: List[Tuple[np.ndarray, np.ndarray, float]] = []
        self.motion: List[float] = []
        self.hand: List[float] = []
        self.presence: List[float] = []
        self.global_change: List[float] = []
        self.max_attr = int(MAX_ATTRIBUTION_S * fps)
        self.revert_frames = int(REVERT_WINDOW_S * fps)
        shape = (self.ah, self.aw)
        self.chg_time = np.full(shape, -1, np.int32)   # time of the last change
        self.chg_prev = np.zeros(shape, bool)          # state before that change
        self.chg_t0 = np.zeros(shape, np.int32)
        self.chg_t1 = np.zeros(shape, np.int32)
        self.chg_w = np.zeros(shape, np.float32)
        # the person mask is computed on a coarse grid for speed
        self._scale_small = 0.5

    # ------------------------------------------------------------------ helpers
    def _ink_map(self, g: np.ndarray, reference: Optional[np.ndarray] = None
                 ) -> Tuple[np.ndarray, float]:
        """Binary map of thin strokes.

        With a ``reference`` (the confirmed ink state) hysteresis is applied:
        a pixel becomes ink above 1.35x the threshold and stops being ink
        below 0.65x.  Stroke edges therefore do not flicker with sensor noise
        or with the compression artefacts of every key frame.
        """
        cv2 = self.cv2
        if self.polarity >= 0:
            th = cv2.morphologyEx(g, cv2.MORPH_TOPHAT, self.k_ink)
        else:
            th = cv2.morphologyEx(g, cv2.MORPH_BLACKHAT, self.k_ink)
        if self._thr is None or self.frame_index % 20 == 1:
            vals = th[self.mask]
            med = float(np.median(vals)) if vals.size else 0.0
            mad = float(np.median(np.abs(vals - med))) if vals.size else 1.0
            self._thr = max(18.0, med + 6.0 * 1.4826 * mad)
        thr = self._thr
        if reference is None:
            return th > thr, thr
        return np.where(reference, th > 0.65 * thr, th > 1.35 * thr), thr

    def _drop_specks(self, changed: np.ndarray, min_pixels: int = 4) -> np.ndarray:
        """Remove isolated changed pixels (noise); strokes are connected."""
        cv2 = self.cv2
        n, labels, stats, _ = cv2.connectedComponentsWithStats(changed.astype(np.uint8), 8)
        if n <= 1:
            return changed
        keep = stats[:, cv2.CC_STAT_AREA] >= min_pixels
        keep[0] = False
        return keep[labels]

    def _decide_polarity(self, g: np.ndarray) -> None:
        self._pol_samples.append(float(np.median(g[self.mask])))
        if len(self._pol_samples) >= 4 or self.polarity == 0:
            level = float(np.median(self._pol_samples))
            self.polarity = 1 if level < 130 else -1

    # ------------------------------------------------------------------ main
    def feed(self, frame: np.ndarray) -> None:
        cv2 = self.cv2
        t = self.frame_index
        self.frame_index += 1
        g = cv2.GaussianBlur(frame, (3, 3), 0)
        if len(self._pol_samples) < 4:
            self._decide_polarity(g)
        if self.prev is None:
            self.prev = g
            if self._initial_bg is not None and self._initial_bg.shape == g.shape:
                self.background = cv2.GaussianBlur(self._initial_bg, (3, 3), 0) \
                    .astype(np.float32)
            else:
                self.background = g.astype(np.float32)
            ink, _ = self._ink_map(g)
            self.ink_ref = ink & self.mask
            self.cand = self.ink_ref.copy()
            self.prev_motion = np.zeros_like(ink)
            self._append(0.0, 0.0, 0.0, 0.0)
            return

        diff = cv2.absdiff(g, self.prev)
        moving = diff > 14
        motion_frac = float(np.count_nonzero(moving & self.mask)) / self.area
        self.prev = g

        # abrupt global change (lights switched, camera bumped, auto exposure)
        bg = self.background
        assert bg is not None
        if motion_frac > 0.45:
            self.background = g.astype(np.float32)
            ink, _ = self._ink_map(g)
            self.ink_ref = ink & self.mask
            self.cand = self.ink_ref.copy()
            self.stable[:] = 0
            self.last_seen[:] = t
            self.prev_motion = moving
            self._append(motion_frac, 0.0, 0.0, 1.0)
            return

        moving_u8 = (moving | self.prev_motion).astype(np.uint8)
        self.prev_motion = moving
        motion_zone = cv2.dilate(moving_u8, self.k_motion).astype(bool)

        # person / foreground blobs: large areas that differ from the background
        fg = (cv2.absdiff(g.astype(np.float32), bg) > 28).astype(np.uint8)
        small = cv2.resize(fg, None, fx=self._scale_small, fy=self._scale_small,
                           interpolation=cv2.INTER_NEAREST)
        small = cv2.morphologyEx(small, cv2.MORPH_OPEN, self.k_open)
        small = cv2.dilate(small, self.k_person)
        person = cv2.resize(small, (self.aw, self.ah), interpolation=cv2.INTER_NEAREST) \
            .astype(bool)
        presence = float(np.count_nonzero(person & self.mask)) / self.area

        # background update where nothing moves; a lecturer standing still is
        # absorbed only very slowly so that his outline is not mistaken for ink
        still = ~motion_zone
        alpha = np.where(person, 0.001, 0.03).astype(np.float32)
        gf = g.astype(np.float32)
        bg[still] += alpha[still] * (gf[still] - bg[still])

        assert self.cand is not None and self.ink_ref is not None
        ink, _ = self._ink_map(g, self.ink_ref)
        observed = self.mask & still & ~person
        same = ink == self.cand
        self.stable = np.where(observed & same, self.stable + 1,
                               np.where(observed, 1, 0)).astype(np.int16)
        self.cand = np.where(observed, ink, self.cand)
        confirmed = self.stable >= STABLE_FRAMES
        changed = confirmed & (self.cand != self.ink_ref)
        unchanged = confirmed & ~changed
        self.last_seen[unchanged] = t
        if changed.any():
            changed = self._drop_specks(changed)

        n_changed = int(np.count_nonzero(changed))
        if n_changed:
            ys, xs = np.nonzero(changed)
            t1 = t - STABLE_FRAMES + 1
            # A change that is undone shortly afterwards was a transient object
            # (a hand held still, a pointing arm, the lecturer pausing in front
            # of the board): retract the evidence recorded for it.
            prev_t = self.chg_time[ys, xs]
            reverting = (prev_t >= 0) & (t - prev_t <= self.revert_frames) & \
                (self.cand[ys, xs] == self.chg_prev[ys, xs])
            if reverting.any():
                ry, rx = ys[reverting], xs[reverting]
                self.events.append((self.chg_t0[ry, rx].copy(),
                                    self.chg_t1[ry, rx].copy(),
                                    -self.chg_w[ry, rx].copy()))
                self.chg_time[ry, rx] = -1
            fresh = ~reverting
            fy, fx = ys[fresh], xs[fresh]
            if len(fy):
                t0 = np.maximum(self.last_seen[fy, fx] + 1, t1 - self.max_attr)
                t0 = np.minimum(t0, t1).astype(np.int32)
                added = self.cand[fy, fx]
                weight = np.where(added, 1.0, 0.5)
                # the longer a pixel was hidden, the less we know about when
                # (and whether) it was written: discount changes after long gaps
                span_s = (t1 - t0 + 1) / self.fps
                weight = weight * np.clip(3.0 / span_s, 0.25, 1.0)
                if n_changed > 0.12 * self.area:
                    weight = weight * 0.1   # suspiciously large change (object moved)
                weight = weight.astype(np.float32)
                t1a = np.full(len(t0), t1, np.int32)
                self.events.append((t0, t1a, weight))
                self.chg_time[fy, fx] = t
                self.chg_prev[fy, fx] = self.ink_ref[fy, fx]
                self.chg_t0[fy, fx] = t0
                self.chg_t1[fy, fx] = t1a
                self.chg_w[fy, fx] = weight
            self.ink_ref[changed] = self.cand[changed]
            self.last_seen[changed] = t

        # small localized motion (a writing hand) as opposed to walking around
        hand = 0.0
        if 0.0005 < motion_frac < 0.06:
            hand = min(1.0, motion_frac / 0.004) * min(1.0, (0.06 - motion_frac) / 0.03)
        self._append(motion_frac, hand, presence, 0.0)

    def _append(self, motion: float, hand: float, presence: float, glob: float) -> None:
        self.motion.append(motion)
        self.hand.append(hand)
        self.presence.append(presence)
        self.global_change.append(glob)

    def finish(self) -> VideoFeatures:
        n = self.frame_index
        diff = np.zeros(n + 2, np.float64)
        early = max(1, int(round(EARLY_ATTRIBUTION_S * self.fps)))
        for t0, t1, w in self.events:
            # A stroke is usually drawn right when the hand reaches the spot,
            # i.e. at the start of the time the pixel was hidden: put 60 % of
            # the evidence into the first seconds and spread the rest.
            span = (t1 - t0 + 1).astype(np.float64)
            per = 0.4 * w / span
            np.add.at(diff, t0, per)
            np.add.at(diff, t1 + 1, -per)
            e1 = np.minimum(t1, t0 + early - 1)
            per_e = 0.6 * w / (e1 - t0 + 1)
            np.add.at(diff, t0, per_e)
            np.add.at(diff, e1 + 1, -per_e)
        evidence = np.cumsum(diff)[:n]
        # pixels per second, normalized to a 512x288 board
        norm = (512 * 288) / max(1.0, self.area)
        ink = (evidence * self.fps * norm).astype(np.float32)
        return VideoFeatures(
            fps=self.fps, ink=ink,
            motion=np.asarray(self.motion, np.float32),
            hand=np.asarray(self.hand, np.float32),
            presence=np.asarray(self.presence, np.float32),
            global_change=np.asarray(self.global_change, np.float32),
            width=self.aw, height=self.ah, polarity=self.polarity,
        )


def thumbnail_interval(duration: float) -> float:
    if duration <= 600:
        return 1.0
    if duration <= 3600:
        return 2.0
    return 3.0


def extract_video_features(media: MediaInfo, regions: Sequence[Sequence[float]],
                           thumbs_dir: Optional[str] = None,
                           progress: Optional[Callable[[float], None]] = None,
                           cancel: Optional[CancelToken] = None,
                           hw_decode: bool = False,
                           fast_decode: bool = True) -> Tuple[VideoFeatures, float, int]:
    """Decode the video and run :class:`BoardTracker`.

    Returns the features, the thumbnail interval and number of thumbnails.
    """
    (cx, cy, cw, ch), (aw, ah) = analysis_geometry(media, regions)
    mask = region_mask(regions, (aw, ah))
    fps = ANALYSIS_FPS
    thumb_int = thumbnail_interval(media.duration)
    chain = (f"[0:v]fps={fps}:start_time=0,split=2[a][b];"
             f"[a]crop={cw}:{ch}:{cx}:{cy},scale={aw}:{ah}:flags=area,format=gray[ana];"
             f"[b]fps=1/{thumb_int}:start_time=0,scale={THUMB_WIDTH}:-2,format=yuvj420p[th]")
    if not thumbs_dir:
        chain = (f"[0:v]fps={fps}:start_time=0,crop={cw}:{ch}:{cx}:{cy},"
                 f"scale={aw}:{ah}:flags=area,format=gray[ana]")

    def build_args(skip: bool) -> List[str]:
        args: List[str] = []
        if skip:
            args += ["-skip_frame", "nonref"]
        args += ffmpeg.hwaccel_args(hw_decode)
        args += ["-i", media.path, "-an", "-sn", "-dn", "-filter_complex", chain,
                 "-map", "[ana]", "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1"]
        if thumbs_dir:
            args += ["-map", "[th]", "-c:v", "mjpeg", "-q:v", "6", "-start_number", "0",
                     "-f", "image2", "-y", os.path.join(thumbs_dir, "t_%06d.jpg")]
        return args

    frame_bytes = aw * ah
    expected = max(1, int(media.duration * fps))

    warmup = int(min(expected, BACKGROUND_WARMUP_S * fps))

    def run(skip: bool) -> BoardTracker:
        tracker: Optional[BoardTracker] = None
        buffered: List[np.ndarray] = []

        def start(frames: List[np.ndarray]) -> BoardTracker:
            # the median of the first frames is a background without the lecturer
            init = np.median(np.stack(frames[::2]), axis=0).astype(np.uint8) \
                if len(frames) >= 8 else None
            tr = BoardTracker((aw, ah), mask, fps, initial_background=init)
            for f in frames:
                tr.feed(f)
            return tr

        count = 0
        with ffmpeg.RawPipeReader(build_args(skip), cancel=cancel) as reader:
            for raw in reader.read_blocks(frame_bytes):
                if len(raw) < frame_bytes:
                    break
                frame = np.frombuffer(raw, np.uint8).reshape(ah, aw)
                count += 1
                if tracker is None:
                    buffered.append(frame)
                    if len(buffered) >= warmup:
                        tracker = start(buffered)
                        buffered = []
                else:
                    tracker.feed(frame)
                if progress is not None and count % 8 == 0:
                    progress(min(1.0, count / expected))
        if tracker is None:
            tracker = start(buffered)
        return tracker

    try:
        tracker = run(fast_decode)
        if tracker.frame_index < 0.5 * expected and fast_decode:
            raise RuntimeError("too few frames with frame skipping")
    except Exception as exc:  # retry without frame skipping / hw decoding
        from ..core.errors import Cancelled

        if isinstance(exc, Cancelled):
            raise
        hw_decode = False
        tracker = run(False)
    feats = tracker.finish()
    n_thumbs = 0
    if thumbs_dir and os.path.isdir(thumbs_dir):
        n_thumbs = len([f for f in os.listdir(thumbs_dir) if f.endswith(".jpg")])
    return feats, thumb_int, n_thumbs


def resample_to_hop(values: np.ndarray, fps: float, n_frames: int, hop: float,
                    reducer: str = "interp") -> np.ndarray:
    if len(values) == 0:
        return np.zeros(n_frames, np.float32)
    t_src = np.arange(len(values)) / fps
    t_dst = (np.arange(n_frames) + 0.5) * hop
    return np.interp(t_dst, t_src, values).astype(np.float32)
