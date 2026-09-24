"""The edit decision list: contiguous segments covering the source video.

Every segment has an *action* (keep at 1x, speed up, cut) and a *kind*
(what the automatic analysis believes is happening).  Editing is
non-destructive: the source is never modified, the timeline is just a list
of decisions that is rendered on export and interpreted by the preview.
"""

from __future__ import annotations

import bisect
import copy
import math
import uuid
from dataclasses import asdict, dataclass, field, fields
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np

from .settings import AutoEditSettings

MIN_SEGMENT = 0.05     # seconds; shorter segments are merged away
TIME_EPS = 1e-6


class Kind:
    SPEECH = "speech"        # 説明: the lecturer is talking
    WRITING = "writing"      # 板書: silent board writing
    IDLE = "idle"            # 待機: nothing useful is happening
    UNCERTAIN = "uncertain"  # 判定が曖昧

    ALL = (SPEECH, WRITING, IDLE, UNCERTAIN)


class Action:
    KEEP = "keep"    # normal speed
    SPEED = "speed"  # sped up
    CUT = "cut"      # removed

    ALL = (KEEP, SPEED, CUT)


def new_id() -> str:
    return uuid.uuid4().hex[:10]


@dataclass
class Segment:
    start: float
    end: float
    kind: str = Kind.SPEECH
    action: str = Action.KEEP
    speed: Optional[float] = None    # explicit speed for SPEED (None -> settings)
    volume: Optional[float] = None   # explicit gain (None -> default for the action)
    protected: bool = False          # "always keep" - never cut, never regenerated
    manual: bool = False             # edited by the user - never regenerated
    review: bool = False             # flagged for the user to check
    reviewed: bool = False           # the user has checked it
    auto_action: str = ""            # what the automatic editor decided
    reason: str = ""                 # short machine readable reason code
    confidence: float = 1.0
    id: str = field(default_factory=new_id)

    @property
    def duration(self) -> float:
        return self.end - self.start

    @property
    def locked(self) -> bool:
        return self.protected or self.manual

    def speed_value(self, settings: AutoEditSettings) -> float:
        if self.action == Action.CUT:
            return math.inf
        if self.action == Action.KEEP:
            return 1.0
        return float(self.speed) if self.speed else float(settings.writing_speed)

    def volume_value(self, settings: AutoEditSettings) -> float:
        if self.volume is not None:
            return float(self.volume)
        if self.action == Action.SPEED:
            return settings.speed_segment_volume()
        return 1.0

    def output_duration(self, settings: AutoEditSettings) -> float:
        if self.action == Action.CUT:
            return 0.0
        return self.duration / self.speed_value(settings)

    def same_decision(self, other: "Segment") -> bool:
        return (self.kind == other.kind and self.action == other.action
                and self.speed == other.speed and self.volume == other.volume
                and self.protected == other.protected and self.manual == other.manual
                and self.review == other.review and self.reviewed == other.reviewed
                and self.auto_action == other.auto_action and self.reason == other.reason)

    def copy(self, **changes: Any) -> "Segment":
        seg = copy.copy(self)
        seg.id = new_id()
        for key, value in changes.items():
            setattr(seg, key, value)
        return seg

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["start"] = round(self.start, 6)
        data["end"] = round(self.end, 6)
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Segment":
        known = {f.name for f in fields(cls)}
        seg = cls(**{k: v for k, v in data.items() if k in known})
        if seg.kind not in Kind.ALL:
            seg.kind = Kind.UNCERTAIN
        if seg.action not in Action.ALL:
            seg.action = Action.KEEP
        return seg


@dataclass
class Piece:
    """A kept (non-cut) span of the source and where it lands in the output."""
    src_start: float
    src_end: float
    out_start: float
    out_end: float
    speed: float
    volume: float
    index: int          # index of the segment in the timeline

    @property
    def src_duration(self) -> float:
        return self.src_end - self.src_start

    @property
    def out_duration(self) -> float:
        return self.out_end - self.out_start


class EditMap:
    """Mapping between source time and output (edited) time."""

    def __init__(self, pieces: List[Piece], source_duration: float) -> None:
        self.pieces = pieces
        self.source_duration = source_duration
        self._src_starts = [p.src_start for p in pieces]
        self._out_starts = [p.out_start for p in pieces]
        self.out_duration = pieces[-1].out_end if pieces else 0.0

    def piece_index_at_src(self, t: float) -> int:
        """Index of the piece containing source time ``t`` or -1 (cut)."""
        i = bisect.bisect_right(self._src_starts, t + TIME_EPS) - 1
        if 0 <= i < len(self.pieces) and t < self.pieces[i].src_end - TIME_EPS:
            return i
        return -1

    def next_piece_index(self, t: float) -> int:
        """First piece whose start is at or after source time ``t`` (or -1)."""
        i = bisect.bisect_left(self._src_starts, t - TIME_EPS)
        return i if i < len(self.pieces) else -1

    def src_to_out(self, t: float) -> float:
        i = self.piece_index_at_src(t)
        if i >= 0:
            p = self.pieces[i]
            return p.out_start + (t - p.src_start) / p.speed
        j = self.next_piece_index(t)
        return self.pieces[j].out_start if j >= 0 else self.out_duration

    def out_to_src(self, t: float) -> float:
        if not self.pieces:
            return 0.0
        t = min(max(0.0, t), self.out_duration)
        i = bisect.bisect_right(self._out_starts, t) - 1
        i = min(max(i, 0), len(self.pieces) - 1)
        p = self.pieces[i]
        return min(p.src_end, p.src_start + (t - p.out_start) * p.speed)

    def out_to_src_array(self, t: "np.ndarray") -> "np.ndarray":
        """Vectorised :meth:`out_to_src` (used for drawing)."""
        if not self.pieces:
            return np.zeros_like(t)
        outs = np.asarray(self._out_starts)
        i = np.clip(np.searchsorted(outs, t, side="right") - 1, 0, len(self.pieces) - 1)
        src0 = np.asarray([p.src_start for p in self.pieces])[i]
        src1 = np.asarray([p.src_end for p in self.pieces])[i]
        speed = np.asarray([p.speed for p in self.pieces])[i]
        return np.minimum(src1, src0 + (np.clip(t, 0, self.out_duration) - outs[i]) * speed)

    def piece_index_array(self, t_out: "np.ndarray") -> "np.ndarray":
        if not self.pieces:
            return np.zeros(len(t_out), int)
        outs = np.asarray(self._out_starts)
        return np.clip(np.searchsorted(outs, t_out, side="right") - 1, 0, len(self.pieces) - 1)

    def piece_index_at_out(self, t: float) -> int:
        if not self.pieces:
            return -1
        i = bisect.bisect_right(self._out_starts, t) - 1
        return min(max(i, 0), len(self.pieces) - 1)


class Timeline:
    """Ordered, contiguous list of segments covering ``[0, duration]``."""

    def __init__(self, duration: float, segments: Optional[Iterable[Segment]] = None) -> None:
        self.duration = float(duration)
        if segments is None:
            segments = [Segment(0.0, self.duration, kind=Kind.UNCERTAIN, action=Action.KEEP,
                                auto_action=Action.KEEP, reason="unanalyzed")]
        self.segments: List[Segment] = list(segments)
        self.normalize()

    # ------------------------------------------------------------------ basics
    def __len__(self) -> int:
        return len(self.segments)

    def __iter__(self):
        return iter(self.segments)

    def __getitem__(self, i: int) -> Segment:
        return self.segments[i]

    def clone(self) -> "Timeline":
        return Timeline(self.duration, [copy.copy(s) for s in self.segments])

    def to_list(self) -> List[Dict[str, Any]]:
        return [s.to_dict() for s in self.segments]

    @classmethod
    def from_list(cls, duration: float, data: Sequence[Dict[str, Any]]) -> "Timeline":
        return cls(duration, [Segment.from_dict(d) for d in data])

    def normalize(self) -> None:
        """Sort, clamp and make the segments contiguous without gaps/overlaps."""
        segs = sorted((s for s in self.segments if s.end > s.start + TIME_EPS),
                      key=lambda s: (s.start, s.end))
        out: List[Segment] = []
        cursor = 0.0
        for seg in segs:
            start = max(seg.start, cursor)
            end = min(seg.end, self.duration)
            if end <= start + TIME_EPS:
                continue
            if start > cursor + TIME_EPS:
                if out:
                    out[-1].end = start       # extend previous across a gap
                else:
                    start = 0.0
            seg.start, seg.end = start, end
            if out:
                seg.start = out[-1].end
            out.append(seg)
            cursor = seg.end
        if not out:
            out = [Segment(0.0, self.duration, kind=Kind.UNCERTAIN, auto_action=Action.KEEP)]
        out[0].start = 0.0
        out[-1].end = self.duration
        # swallow slivers into a neighbour
        i = 0
        while i < len(out) and len(out) > 1:
            if out[i].duration < MIN_SEGMENT * 0.5:
                if i > 0:
                    out[i - 1].end = out[i].end
                else:
                    out[i + 1].start = out[i].start
                del out[i]
                continue
            i += 1
        self.segments = out

    def merge_equal_neighbors(self) -> None:
        if not self.segments:
            return
        merged = [self.segments[0]]
        for seg in self.segments[1:]:
            if merged[-1].same_decision(seg):
                merged[-1].end = seg.end
                merged[-1].confidence = min(merged[-1].confidence, seg.confidence)
            else:
                merged.append(seg)
        self.segments = merged

    # ------------------------------------------------------------------ lookup
    def index_at(self, t: float) -> int:
        starts = [s.start for s in self.segments]
        i = bisect.bisect_right(starts, t + TIME_EPS) - 1
        return min(max(i, 0), len(self.segments) - 1)

    def segment_at(self, t: float) -> Segment:
        return self.segments[self.index_at(t)]

    def index_of(self, seg_id: str) -> int:
        for i, s in enumerate(self.segments):
            if s.id == seg_id:
                return i
        return -1

    def boundaries(self) -> List[float]:
        return [s.start for s in self.segments[1:]]

    # ------------------------------------------------------------------ edits
    def split_at(self, t: float, min_len: float = MIN_SEGMENT) -> Optional[Tuple[int, int]]:
        i = self.index_at(t)
        seg = self.segments[i]
        if t - seg.start < min_len or seg.end - t < min_len:
            return None
        right = seg.copy(start=t)
        seg.end = t
        self.segments.insert(i + 1, right)
        return i, i + 1

    def move_boundary(self, index: int, t: float, min_len: float = MIN_SEGMENT,
                      mark_manual: bool = True) -> float:
        """Move the boundary between ``index-1`` and ``index`` to ``t``.

        Returns the boundary position actually applied after clamping.
        """
        if index <= 0 or index >= len(self.segments):
            raise IndexError("boundary index out of range")
        left, right = self.segments[index - 1], self.segments[index]
        t = min(max(t, left.start + min_len), right.end - min_len)
        left.end = t
        right.start = t
        if mark_manual:
            left.manual = True
            right.manual = True
        return t

    def set_action(self, indices: Iterable[int], action: str,
                   speed: Optional[float] = None) -> None:
        for i in indices:
            seg = self.segments[i]
            if action == Action.CUT and seg.protected:
                continue
            seg.action = action
            if action == Action.SPEED:
                seg.speed = speed if speed else seg.speed
            elif action == Action.KEEP:
                seg.speed = None
            seg.manual = True

    def set_speed(self, indices: Iterable[int], speed: float) -> None:
        for i in indices:
            seg = self.segments[i]
            if seg.action == Action.CUT:
                continue
            if abs(speed - 1.0) < 1e-6:
                seg.action = Action.KEEP
                seg.speed = None
            else:
                seg.action = Action.SPEED
                seg.speed = float(speed)
            seg.manual = True

    def set_volume(self, indices: Iterable[int], volume: Optional[float]) -> None:
        for i in indices:
            self.segments[i].volume = volume
            self.segments[i].manual = True

    def set_protected(self, indices: Iterable[int], protected: bool) -> None:
        for i in indices:
            seg = self.segments[i]
            seg.protected = protected
            if protected and seg.action == Action.CUT:
                seg.action = Action.KEEP
                seg.speed = None

    def restore(self, indices: Iterable[int]) -> None:
        """Bring back cut segments (at the automatic action, never cut)."""
        for i in indices:
            seg = self.segments[i]
            if seg.action != Action.CUT:
                continue
            target = seg.auto_action if seg.auto_action in (Action.KEEP, Action.SPEED) \
                else Action.KEEP
            seg.action = target
            if target == Action.KEEP:
                seg.speed = None
            seg.manual = True

    def unlock(self, indices: Iterable[int]) -> None:
        """Release manual edits so the automatic editor may change them again."""
        for i in indices:
            seg = self.segments[i]
            seg.manual = False
            seg.protected = False
            if seg.auto_action in Action.ALL:
                seg.action = seg.auto_action
            seg.speed = None
            seg.volume = None

    def delete_and_close(self, indices: Iterable[int]) -> None:
        self.set_action(indices, Action.CUT)

    # ------------------------------------------------------------------ mapping
    def build_map(self, settings: AutoEditSettings) -> EditMap:
        pieces: List[Piece] = []
        out = 0.0
        for i, seg in enumerate(self.segments):
            if seg.action == Action.CUT:
                continue
            speed = seg.speed_value(settings)
            dur = seg.duration / speed
            vol = seg.volume_value(settings)
            # one piece per kept segment so that Piece.index maps back 1:1
            pieces.append(Piece(seg.start, seg.end, out, out + dur, speed, vol, i))
            out += dur
        return EditMap(pieces, self.duration)

    def stats(self, settings: AutoEditSettings) -> Dict[str, float]:
        kept = sped_src = sped_out = cut = 0.0
        review = review_open = 0
        for seg in self.segments:
            if seg.action == Action.CUT:
                cut += seg.duration
            elif seg.action == Action.SPEED:
                sped_src += seg.duration
                sped_out += seg.output_duration(settings)
            else:
                kept += seg.duration
            if seg.review:
                review += 1
                if not seg.reviewed:
                    review_open += 1
        out = kept + sped_out
        return {
            "source": self.duration,
            "output": out,
            "kept": kept,
            "sped_source": sped_src,
            "sped_output": sped_out,
            "saved_by_speed": sped_src - sped_out,
            "cut": cut,
            "review": review,
            "review_open": review_open,
            "reduction": (1.0 - out / self.duration) if self.duration > 0 else 0.0,
        }


def overlay_locked(auto: Sequence[Segment], previous: Sequence[Segment],
                   duration: float) -> List[Segment]:
    """Combine freshly generated segments with the locked ones of an older
    timeline.  Locked (manual/protected) segments win over automatic ones."""
    locked = sorted((copy.copy(s) for s in previous if s.locked), key=lambda s: s.start)
    if not locked:
        return [copy.copy(s) for s in auto]
    result: List[Segment] = list(locked)
    for seg in auto:
        pieces = [(seg.start, seg.end)]
        for lk in locked:
            if lk.end <= seg.start or lk.start >= seg.end:
                continue
            nxt = []
            for a, b in pieces:
                if lk.end <= a or lk.start >= b:
                    nxt.append((a, b))
                    continue
                if lk.start > a:
                    nxt.append((a, lk.start))
                if lk.end < b:
                    nxt.append((lk.end, b))
            pieces = nxt
        for a, b in pieces:
            if b - a > TIME_EPS:
                result.append(seg.copy(start=a, end=b))
    result.sort(key=lambda s: s.start)
    tl = Timeline(duration, result)
    return tl.segments
