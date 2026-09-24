"""Turn analysis features into an edit decision list.

Rules (see the design document):

* speech            -> keep at 1x (speech wins over simultaneous writing)
* silent writing    -> speed up (n x)
* idle waiting      -> cut, gap closed
* ambiguous         -> keep at 1x and flag for review

Protective measures: margins around speech, short pauses are never cut,
the finished board is shown for a moment after writing, short writing bursts
are not sped up and short gaps between writing are merged so the speed does
not toggle rapidly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Tuple

import numpy as np

from ..core.settings import AutoEditSettings
from ..core.timeline import Action, Kind, Segment, Timeline, overlay_locked
from .result import AnalysisResult


class Reason:
    SPEECH = "speech"
    WRITING = "writing"
    SHORT_WRITING = "short_writing"
    IDLE_CUT = "idle_cut"
    SHORT_PAUSE = "short_pause"
    MARGIN = "margin"
    HOLD_AFTER_WRITING = "hold_after_writing"
    UNCERTAIN_SPEECH = "uncertain_speech"
    UNCERTAIN_WRITING = "uncertain_writing"
    UNCERTAIN_MOTION = "uncertain_motion"
    UNANALYZED = "unanalyzed"


# frame labels
_SPEECH, _WRITING, _IDLE, _UNC_SPEECH, _UNC_WRITING, _UNC_MOTION = range(6)
_UNCERTAIN = (_UNC_SPEECH, _UNC_WRITING, _UNC_MOTION)


def _smooth(x: np.ndarray, frames: int) -> np.ndarray:
    if frames <= 1 or len(x) == 0:
        return x.astype(np.float32)
    k = np.ones(frames, np.float64) / frames
    pad = frames // 2
    xp = np.pad(x.astype(np.float64), (pad, frames - 1 - pad), mode="edge")
    return np.convolve(xp, k, mode="valid").astype(np.float32)


def _runs(labels: np.ndarray) -> List[Tuple[int, int, int]]:
    """Return ``(label, start, end)`` runs (end exclusive)."""
    if len(labels) == 0:
        return []
    change = np.flatnonzero(np.diff(labels)) + 1
    starts = np.concatenate([[0], change])
    ends = np.concatenate([change, [len(labels)]])
    return [(int(labels[s]), int(s), int(e)) for s, e in zip(starts, ends)]


def _fill_gaps(mask: np.ndarray, max_gap: int) -> np.ndarray:
    """Set short False runs that lie between True runs to True."""
    out = mask.copy()
    for val, s, e in _runs(mask.astype(np.int8)):
        if val == 0 and s > 0 and e < len(mask) and (e - s) <= max_gap:
            out[s:e] = True
    return out


def _drop_short(mask: np.ndarray, min_len: int) -> np.ndarray:
    out = mask.copy()
    for val, s, e in _runs(mask.astype(np.int8)):
        if val == 1 and (e - s) < min_len:
            out[s:e] = False
    return out


def _dilate(mask: np.ndarray, before: int, after: int) -> np.ndarray:
    out = mask.copy()
    idx = np.flatnonzero(mask)
    if len(idx) == 0:
        return out
    for val, s, e in _runs(mask.astype(np.int8)):
        if val == 1:
            out[max(0, s - before):s] = True
            out[e:min(len(mask), e + after)] = True
    return out


@dataclass
class Scores:
    speech: np.ndarray
    writing: np.ndarray
    activity: np.ndarray


def compute_scores(res: AnalysisResult) -> Scores:
    fr = int(round(1.0 / res.hop))
    speech = np.clip(res.speech, 0, 1)
    ink = np.maximum(res.ink, 0)
    ink_score = 1.0 - np.exp(-_smooth(ink, 2 * fr) / 80.0)
    click_score = 1.0 - np.exp(-_smooth(res.clicks, 2 * fr) / 2.5)
    hand = _smooth(np.clip(res.hand, 0, 1), 2 * fr)
    if res.has_video_features:
        writing = ink_score + 0.35 * click_score + 0.1 * hand
    else:
        writing = 0.8 * click_score
    writing = np.clip(writing, 0, 1)
    # a sudden global change (lights, camera) is not writing
    if len(res.global_change):
        writing = np.where(_smooth(res.global_change, fr) > 0.3, 0.0, writing)
    activity = np.clip(_smooth(res.motion, fr) / 0.01, 0, 1)
    return Scores(speech.astype(np.float32), writing.astype(np.float32),
                  activity.astype(np.float32))


def frame_labels(res: AnalysisResult, st: AutoEditSettings,
                 scores: Optional[Scores] = None) -> np.ndarray:
    sc = scores or compute_scores(res)
    fr = 1.0 / res.hop
    n = len(sc.speech)
    th_s = st.eff_speech_threshold
    th_w = st.eff_writing_threshold
    band = st.eff_review_band

    # --- speech with hysteresis, gap filling, min length and margins -------
    speech = np.zeros(n, bool)
    on = False
    for i, p in enumerate(sc.speech):
        if on:
            on = p >= th_s - 0.15
        else:
            on = p >= th_s
        speech[i] = on
    speech = _fill_gaps(speech, int(round(st.eff_speech_gap_fill * fr)))
    speech = _drop_short(speech, max(1, int(round(0.2 * fr))))

    # --- labels ignoring speech ------------------------------------------------
    alt = np.full(n, _IDLE, np.int8)
    writing = sc.writing >= th_w
    maybe_writing = (sc.writing >= th_w - band) & ~writing
    moving = sc.activity >= 0.5
    alt[writing] = _WRITING
    alt[maybe_writing] = _UNC_WRITING
    if st.motion_is_uncertain:
        alt[(alt == _IDLE) & moving] = _UNC_MOTION
    # possible quiet speech that did not reach the threshold
    maybe_speech = (sc.speech >= th_s - band) & ~speech
    alt[maybe_speech] = _UNC_SPEECH

    # merge short gaps inside writing (not across speech)
    wmask = _fill_gaps(alt == _WRITING, int(round(st.eff_writing_gap_fill * fr)))
    alt[wmask & (alt != _UNC_SPEECH)] = _WRITING

    # The writing score rises and decays smoothly, so a writing run is usually
    # framed by short "possibly writing" runs.  These edges are not worth a
    # review: the rising edge belongs to the writing, the decaying edge is
    # handled like the pause after writing (the finished board is shown).
    edge = int(round(max(2.5, st.eff_hold_after_writing + 1.0) * fr))
    runs = _runs(alt)
    for k, (val, s, e) in enumerate(runs):
        if val != _UNC_WRITING or (e - s) > edge:
            continue
        prev = runs[k - 1][0] if k > 0 else None
        nxt = runs[k + 1][0] if k + 1 < len(runs) else None
        if prev == _WRITING:
            alt[s:e] = _IDLE
        elif nxt == _WRITING:
            alt[s:e] = _WRITING

    # very short uncertain islands follow their neighbours
    min_unc = int(round(1.0 * fr))
    min_unc_speech = int(round(0.5 * fr))
    runs = _runs(alt)
    for k, (val, s, e) in enumerate(runs):
        limit = min_unc_speech if val == _UNC_SPEECH else min_unc
        if val in _UNCERTAIN and (e - s) < limit:
            prev = runs[k - 1][0] if k > 0 else None
            nxt = runs[k + 1][0] if k + 1 < len(runs) else None
            alt[s:e] = _WRITING if _WRITING in (prev, nxt) else _IDLE

    # --- speech margins override everything ----------------------------------
    speech = _dilate(speech, int(round(st.pad_before * fr)), int(round(st.pad_after * fr)))
    labels = np.where(speech, _SPEECH, alt).astype(np.int8)
    return labels


def _kind_reason(label: int) -> Tuple[str, str]:
    return {
        _SPEECH: (Kind.SPEECH, Reason.SPEECH),
        _WRITING: (Kind.WRITING, Reason.WRITING),
        _IDLE: (Kind.IDLE, Reason.IDLE_CUT),
        _UNC_SPEECH: (Kind.UNCERTAIN, Reason.UNCERTAIN_SPEECH),
        _UNC_WRITING: (Kind.UNCERTAIN, Reason.UNCERTAIN_WRITING),
        _UNC_MOTION: (Kind.UNCERTAIN, Reason.UNCERTAIN_MOTION),
    }[label]


def labels_to_segments(labels: np.ndarray, hop: float, duration: float,
                       st: AutoEditSettings) -> List[Segment]:
    runs = _runs(labels)
    segs: List[Segment] = []
    min_cut = st.eff_min_cut
    margin = st.eff_cut_margin
    hold = st.eff_hold_after_writing
    for k, (val, s, e) in enumerate(runs):
        a = min(duration, s * hop)
        b = min(duration, e * hop) if k + 1 < len(runs) else duration
        if b - a <= 1e-6:
            continue
        kind, reason = _kind_reason(val)
        if val == _SPEECH:
            segs.append(Segment(a, b, kind, Action.KEEP, auto_action=Action.KEEP, reason=reason))
        elif val == _WRITING:
            if b - a >= st.eff_min_speed:
                segs.append(Segment(a, b, kind, Action.SPEED, auto_action=Action.SPEED,
                                    reason=reason))
            else:
                segs.append(Segment(a, b, kind, Action.KEEP, auto_action=Action.KEEP,
                                    reason=Reason.SHORT_WRITING))
        elif val == _IDLE:
            prev = runs[k - 1][0] if k > 0 else None
            nxt = runs[k + 1][0] if k + 1 < len(runs) else None
            lead = 0.0 if prev is None else (hold if prev == _WRITING else margin)
            trail = 0.0 if nxt is None else margin
            if (b - a) < min_cut or (b - a) - lead - trail < max(0.3, 0.5 * min_cut):
                segs.append(Segment(a, b, kind, Action.KEEP, auto_action=Action.KEEP,
                                    reason=Reason.SHORT_PAUSE))
                continue
            if lead > 0:
                segs.append(Segment(a, a + lead, kind, Action.KEEP, auto_action=Action.KEEP,
                                    reason=Reason.HOLD_AFTER_WRITING if prev == _WRITING
                                    else Reason.MARGIN))
            segs.append(Segment(a + lead, b - trail, kind, Action.CUT, auto_action=Action.CUT,
                                reason=Reason.IDLE_CUT))
            if trail > 0:
                segs.append(Segment(b - trail, b, kind, Action.KEEP, auto_action=Action.KEEP,
                                    reason=Reason.MARGIN))
        else:
            segs.append(Segment(a, b, kind, Action.KEEP, auto_action=Action.KEEP,
                                reason=reason, review=True))

    # anti-flicker: a short non-speech 1x island between two sped-up parts
    for i in range(1, len(segs) - 1):
        cur, prv, nxt = segs[i], segs[i - 1], segs[i + 1]
        if (cur.action == Action.KEEP and cur.kind != Kind.SPEECH and not cur.review
                and prv.action == Action.SPEED and nxt.action == Action.SPEED
                and cur.duration < max(1.0, st.eff_min_speed * 0.5)):
            cur.action = cur.auto_action = Action.SPEED
            cur.kind = Kind.WRITING
            cur.reason = Reason.WRITING
    segs = _merge_review_clusters(segs, st)
    tl = Timeline(duration, segs)
    tl.merge_equal_neighbors()
    return tl.segments


def _merge_review_clusters(segs: List[Segment], st: AutoEditSettings) -> List[Segment]:
    """Uncertain parts separated only by short kept pauses (or a very short
    speech blip) become one item to check instead of many small ones."""
    def joinable(seg: Segment) -> bool:
        if seg.action != Action.KEEP:
            return False
        if seg.review:
            return True
        if seg.kind == Kind.IDLE and seg.reason in (Reason.SHORT_PAUSE,):
            return True
        return seg.kind == Kind.SPEECH and seg.duration < 1.0

    out: List[Segment] = []
    i = 0
    n = len(segs)
    while i < n:
        if not segs[i].review:
            out.append(segs[i])
            i += 1
            continue
        j = i + 1
        last_review = i
        while j < n and joinable(segs[j]):
            if segs[j].review:
                last_review = j
            j += 1
        group = segs[i:last_review + 1]
        if len(group) > 1:
            reasons = [g.reason for g in group if g.review]
            reason = max(set(reasons), key=reasons.count)
            out.append(Segment(group[0].start, group[-1].end, Kind.UNCERTAIN, Action.KEEP,
                               auto_action=Action.KEEP, reason=reason, review=True))
        else:
            out.append(group[0])
        i = last_review + 1
    return out


def auto_segments(res: AnalysisResult, st: AutoEditSettings) -> List[Segment]:
    labels = frame_labels(res, st)
    segs = labels_to_segments(labels, res.hop, res.duration, st)
    scores = compute_scores(res)
    fr = 1.0 / res.hop
    for seg in segs:
        i0, i1 = int(seg.start * fr), max(int(seg.start * fr) + 1, int(seg.end * fr))
        if seg.kind == Kind.SPEECH:
            seg.confidence = float(np.clip(np.mean(scores.speech[i0:i1]) * 1.2, 0, 1))
        elif seg.kind == Kind.WRITING:
            seg.confidence = float(np.clip(np.mean(scores.writing[i0:i1]) * 1.2, 0, 1))
        elif seg.kind == Kind.IDLE:
            seg.confidence = float(np.clip(1.0 - np.mean(np.maximum(
                scores.speech[i0:i1], scores.writing[i0:i1])) * 1.5, 0, 1))
        else:
            seg.confidence = 0.5
    return segs


def regenerate(timeline: Timeline, res: AnalysisResult, st: AutoEditSettings,
               keep_locked: bool = True) -> Timeline:
    """New automatic edit that preserves manual/protected segments."""
    auto = auto_segments(res, st)
    if keep_locked:
        segs = overlay_locked(auto, timeline.segments, timeline.duration)
    else:
        segs = auto
    tl = Timeline(timeline.duration, segs)
    tl.merge_equal_neighbors()
    return tl
