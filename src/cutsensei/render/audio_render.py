"""Render the edited audio track.

The audio is decoded once, sequentially.  Every kept piece of the edit is
copied (1x) or time-stretched with pitch preserved (WSOLA) so that its length
matches the video exactly - sample counts are derived from the cumulative
output time, so audio and video never drift apart.  Short fades are applied
at every discontinuity (cut or speed change) to avoid clicks.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import BinaryIO, Callable, Iterator, List, Optional

import numpy as np

from ..core import ffmpeg
from ..core.jobs import CancelToken
from ..core.media import MediaInfo
from ..core.timeline import EditMap, Piece

FADE_S = 0.004
MAX_STRETCH_CHUNK_S = 60.0


def wsola(x: np.ndarray, speed: float, sr: int, out_len: Optional[int] = None) -> np.ndarray:
    """Time-scale modification with pitch preservation.

    ``x`` has shape ``(n, channels)``; the result has ``out_len`` samples
    (default ``round(n / speed)``).
    """
    n, ch = x.shape
    if out_len is None:
        out_len = int(round(n / speed))
    if out_len <= 0:
        return np.zeros((0, ch), np.float32)
    if abs(speed - 1.0) < 1e-4 or n < 64:
        return _fit_length(x, out_len)
    frame = max(64, int(0.030 * sr) // 2 * 2)
    hop_out = frame // 2
    hop_in = hop_out * speed
    tol = int(0.010 * sr)
    window = (0.5 - 0.5 * np.cos(2 * np.pi * np.arange(frame) / frame)).astype(np.float32)
    pad = frame + 2 * tol + int(hop_in) + 8
    xp = np.concatenate([np.zeros((tol, ch), np.float32), x.astype(np.float32),
                         np.zeros((pad, ch), np.float32)])
    dec = 4 if sr >= 32000 else 2
    mono = xp.mean(axis=1)[::dec].astype(np.float32)
    n_frames = out_len // hop_out + 2
    out = np.zeros((n_frames * hop_out + frame, ch), np.float32)
    prev = tol  # position (in xp) of the previously used frame
    f_d = frame // dec
    tol_d = tol // dec
    for k in range(n_frames):
        nominal = tol + int(round(k * hop_in))
        if k == 0:
            best = nominal
        else:
            natural = prev + hop_out
            t0 = natural // dec
            template = mono[t0:t0 + f_d]
            s0 = max(0, nominal // dec - tol_d)
            region = mono[s0:s0 + f_d + 2 * tol_d]
            if len(template) == f_d and len(region) >= f_d and np.any(template):
                corr = np.correlate(region, template, mode="valid")
                best = (s0 + int(np.argmax(corr))) * dec
                best = min(max(best, nominal - tol), nominal + tol)
            else:
                best = nominal
        seg = xp[best:best + frame]
        if len(seg) < frame:
            seg = np.concatenate([seg, np.zeros((frame - len(seg), ch), np.float32)])
        out[k * hop_out:k * hop_out + frame] += seg * window[:, None]
        prev = best
    return out[:out_len]


def _fit_length(x: np.ndarray, n: int) -> np.ndarray:
    if len(x) >= n:
        return x[:n].astype(np.float32, copy=False)
    return np.concatenate([x, np.zeros((n - len(x), x.shape[1]), np.float32)])


def _fade(y: np.ndarray, sr: int, fade_in: bool, fade_out: bool) -> None:
    m = min(len(y) // 2, int(FADE_S * sr))
    if m <= 1:
        return
    ramp = np.linspace(0.0, 1.0, m, dtype=np.float32)[:, None]
    if fade_in:
        y[:m] *= ramp
    if fade_out:
        y[-m:] *= ramp[::-1]


@dataclass
class _Job:
    src0: int
    src1: int
    out_len: int
    speed: float
    volume: float
    fade_in: bool
    fade_out: bool


def plan_audio(edit: EditMap, sr: int) -> List[_Job]:
    """Sample exact jobs for every piece (long sped-up pieces are split)."""
    jobs: List[_Job] = []
    pieces = edit.pieces
    for i, p in enumerate(pieces):
        prev: Optional[Piece] = pieces[i - 1] if i > 0 else None
        nxt: Optional[Piece] = pieces[i + 1] if i + 1 < len(pieces) else None
        cont_prev = prev is not None and abs(prev.src_end - p.src_start) < 1e-6 and \
            prev.speed == p.speed == 1.0 and prev.volume == p.volume
        cont_next = nxt is not None and abs(p.src_end - nxt.src_start) < 1e-6 and \
            nxt.speed == p.speed == 1.0 and nxt.volume == p.volume
        # split long stretched pieces so memory use stays bounded
        n_sub = 1
        if p.speed != 1.0:
            n_sub = max(1, int(np.ceil(p.src_duration / MAX_STRETCH_CHUNK_S)))
        for j in range(n_sub):
            a = p.src_start + p.src_duration * j / n_sub
            b = p.src_start + p.src_duration * (j + 1) / n_sub
            oa = p.out_start + p.out_duration * j / n_sub
            ob = p.out_start + p.out_duration * (j + 1) / n_sub
            jobs.append(_Job(
                src0=int(round(a * sr)), src1=int(round(b * sr)),
                out_len=int(round(ob * sr)) - int(round(oa * sr)),
                speed=p.speed, volume=p.volume,
                fade_in=(j == 0 and not cont_prev), fade_out=(j == n_sub - 1 and not cont_next)))
    return jobs


class _SourceReader:
    """Sequential access to decoded samples with a sliding buffer."""

    def __init__(self, blocks: Iterator[bytes], channels: int) -> None:
        self.blocks = blocks
        self.ch = channels
        self.buf = np.zeros((0, channels), np.float32)
        self.buf_start = 0
        self.eof = False

    def _fill(self, until: int) -> None:
        parts = [self.buf]
        have = self.buf_start + len(self.buf)
        while have < until and not self.eof:
            try:
                raw = next(self.blocks)
            except StopIteration:
                self.eof = True
                break
            arr = np.frombuffer(raw, np.float32)
            arr = arr[: len(arr) // self.ch * self.ch].reshape(-1, self.ch)
            parts.append(arr)
            have += len(arr)
        if len(parts) > 1:
            self.buf = np.concatenate(parts)

    def read(self, a: int, b: int) -> np.ndarray:
        """Samples [a, b); everything before ``a`` is discarded."""
        self._fill(b)
        if a > self.buf_start:
            drop = min(len(self.buf), a - self.buf_start)
            self.buf = self.buf[drop:]
            self.buf_start += drop
        lo = a - self.buf_start
        hi = b - self.buf_start
        out = self.buf[max(0, lo):max(0, hi)]
        if lo < 0:  # should not happen - pieces are sequential
            out = np.concatenate([np.zeros((-lo, self.ch), np.float32), out])
        if len(out) < b - a:
            out = np.concatenate([out, np.zeros((b - a - len(out), self.ch), np.float32)])
        return out


def render_audio(media: MediaInfo, edit: EditMap, out: BinaryIO, sr: int, channels: int,
                 progress: Optional[Callable[[float], None]] = None,
                 cancel: Optional[CancelToken] = None) -> int:
    """Write interleaved float32 PCM for the edited timeline to ``out``.

    Returns the number of sample frames written.
    """
    jobs = plan_audio(edit, sr)
    total_out = sum(j.out_len for j in jobs)
    args = ["-i", media.path, "-vn", "-sn", "-dn", "-map", "0:a:0",
            "-af", f"aresample={sr}:async=1:first_pts=0", "-ac", str(channels),
            "-f", "f32le", "pipe:1"]
    written = 0
    block = sr * channels * 4 * 2  # ~2 s per read
    with ffmpeg.RawPipeReader(args, cancel=cancel) as reader:
        src = _SourceReader(iter(reader.read_blocks(block)), channels)
        for job in jobs:
            if cancel is not None:
                cancel.check()
            if job.out_len <= 0:
                continue
            if job.speed == 1.0:
                # stream long 1x pieces in chunks
                pos = job.src0
                remaining = job.out_len
                first = True
                while remaining > 0:
                    n = min(remaining, sr * 20)
                    y = src.read(pos, pos + n).copy()
                    y *= job.volume
                    _fade(y, sr, job.fade_in and first, job.fade_out and remaining == n)
                    out.write(np.clip(y, -1.0, 1.0).astype("<f4").tobytes())
                    pos += n
                    remaining -= n
                    written += n
                    first = False
                    if progress is not None and total_out:
                        progress(written / total_out)
            else:
                x = src.read(job.src0, job.src1)
                if job.volume == 0.0:
                    y = np.zeros((job.out_len, channels), np.float32)
                else:
                    y = wsola(x, job.speed, sr, job.out_len)
                    y *= job.volume
                    _fade(y, sr, job.fade_in, job.fade_out)
                out.write(np.clip(y, -1.0, 1.0).astype("<f4").tobytes())
                written += job.out_len
                if progress is not None and total_out:
                    progress(written / total_out)
        reader.stop()  # the rest of the source (a trailing cut) is not needed
    return written
