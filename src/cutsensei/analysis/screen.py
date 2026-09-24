"""Writing detection for screen recordings of digital notes.

Screen recordings of GoodNotes, Notability, OneNote, a digital whiteboard or
annotated slides are noise free and nobody stands in front of the page, so
every pixel that changes and then *stays* changed is content that was
written, erased, pasted or moved.  Things that only pass by are ignored:

* **laser pointer, cursor, pen hover, pop-up menus** - they disappear again.
  A change must stay unchanged for 1.5 s before it counts, and a change that
  is undone within 12 s (the laser dot resting on a word, a menu that was
  open for a while) is retracted.
* **scrolling, zooming, page turns** - they move or replace the whole page.
  They are reported separately as *navigation* and the page seen afterwards
  becomes the new reference, so the old content is not counted again.
* **the clock of the status bar** and similar small indicators that change
  on their own - isolated tiny changes that repeat at the same place are
  dropped.
* a **camera picture-in-picture** of the lecturer or a playing video - found
  by :mod:`.source` and masked out; a change that keeps going for several
  seconds is treated as movement, not as navigation.

Written content is counted in 4x4 pixel cells, which makes the amount of
"ink" depend on the length of the strokes rather than on the pen width.
"""

from __future__ import annotations

from typing import List, Optional, Tuple

import numpy as np

SCREEN_MAX = 960            # longest side of the analysed frames
STABLE_FRAMES = 6           # 1.5 s at 4 fps
CHANGE_THRESHOLD = 60       # |dB| + |dG| + |dR|
SETTLE_THRESHOLD = 12       # smaller steps do not interrupt "unchanged for 1.5 s"
CELL = 4
GRID = 12                   # coarse grid measuring how spread out a change is
NAV_FRACTION = 0.05         # share of the page changing at once
NAV_SPREAD = 0.3            # share of the coarse grid touched at once
REVERT_WINDOW_S = 12.0
MAX_ATTRIBUTION_S = 3.5
ISOLATION_S = 4.0
TINY_CELLS = 12             # isolated changes smaller than this are ignored
TICKER_EVENTS = 2           # isolated changes repeating at a place: an indicator
SUSTAINED_S = 4.0           # longer continuous change: movement (a video), not navigation
LIVE_BLOCK = 16             # blocks that change all the time (a webcam picture-in-picture)
LIVE_ON = 0.6
LIVE_OFF = 0.3
LIVE_RATE = 0.03            # ~8 s of continuous change before an area counts as live
INK_PER_CELL = 4.0          # scales cells/s to the ink scale of the board tracker


def _sum_absdiff(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Per pixel |dB| + |dG| + |dR| (saturating at 255)."""
    import cv2

    return cv2.transform(cv2.absdiff(a, b), np.ones((1, 3), np.float32))


class ScreenTracker:
    """Frame by frame state machine for screen recordings (BGR frames)."""

    def __init__(self, size: Tuple[int, int], mask: np.ndarray, fps: float) -> None:
        import cv2

        self.cv2 = cv2
        self.aw, self.ah = size
        self.mask = mask
        self.area = max(1.0, float(mask.sum()))
        self.fps = fps
        shape = (self.ah, self.aw)
        self.prev: Optional[np.ndarray] = None
        self.canvas: Optional[np.ndarray] = None
        self.since = np.zeros(shape, np.int32)          # last frame the pixel changed
        self.chg_time = np.full(shape, -1, np.int32)    # frame of the last accepted change
        self.chg_prev = np.zeros(shape + (3,), np.uint8)  # value before that change
        self.cw = (self.aw + CELL - 1) // CELL
        self.ch = (self.ah + CELL - 1) // CELL
        self.n_cells = self.cw * self.ch
        cell_mask = np.zeros((self.ch * CELL, self.cw * CELL), bool)
        cell_mask[:self.ah, :self.aw] = mask
        self.mask_cells = max(1, int(cell_mask.reshape(self.ch, CELL, self.cw, CELL)
                                     .any(axis=(1, 3)).sum()))
        self.cell_credit = np.full(self.n_cells, -1, np.int32)
        self.cell_weight = np.zeros(self.n_cells, np.float32)
        gy = (np.arange(self.ah) * GRID) // self.ah
        gx = (np.arange(self.aw) * GRID) // self.aw
        self.grid_index = (gy[:, None] * GRID + gx[None, :]).astype(np.int32)
        self.grid_cells = max(1, int((np.bincount(self.grid_index[mask], minlength=GRID * GRID)
                                      > 0).sum()))
        self.revert_frames = int(REVERT_WINDOW_S * fps)
        self.max_attr = int(MAX_ATTRIBUTION_S * fps)
        self.ev_frames: List[np.ndarray] = []
        self.ev_cells: List[np.ndarray] = []
        self.ev_weights: List[np.ndarray] = []
        self.nav: List[float] = []
        self.motion: List[float] = []
        self.nav_active = False
        self.calm = 0
        self.frame_index = 0
        # areas that change all the time (a webcam, a video) are ignored
        self.lw = max(1, int(round(self.aw / LIVE_BLOCK)))
        self.lh = max(1, int(round(self.ah / LIVE_BLOCK)))
        by = (np.arange(self.ah) * self.lh) // self.ah
        bx = (np.arange(self.aw) * self.lw) // self.aw
        self.block_index = (by[:, None] * self.lw + bx[None, :]).astype(np.int32)
        self.block_pixels = np.maximum(1, np.bincount(self.block_index.ravel(),
                                                      minlength=self.lh * self.lw))
        self.live_ema = np.zeros(self.lh * self.lw, np.float32)
        self.live_blocks = np.zeros(self.lh * self.lw, bool)
        self.ever_live = np.zeros(self.lh * self.lw, bool)
        self.active = mask
        # part of the frame that may differ from the reference page
        self.dirty: Optional[Tuple[int, int, int, int]] = None

    # ------------------------------------------------------------------ helpers
    def _bbox(self, m: np.ndarray, ox: int = 0, oy: int = 0
              ) -> Optional[Tuple[int, int, int, int]]:
        x, y, w, h = self.cv2.boundingRect(m.view(np.uint8) if m.dtype == bool else m)
        if w == 0 or h == 0:
            return None
        return ox + x, oy + y, ox + x + w, oy + y + h

    def _drop_specks(self, changed: np.ndarray, min_pixels: int = 3) -> np.ndarray:
        cv2 = self.cv2
        n, labels, stats, _ = cv2.connectedComponentsWithStats(changed.view(np.uint8), 8)
        if n <= 1:
            return changed
        keep = stats[:, cv2.CC_STAT_AREA] >= min_pixels
        keep[0] = False
        return keep[labels]

    def _scattered(self, changed: np.ndarray) -> bool:
        """Many separate solid pieces (words, drawings) changed at once - not a
        single stroke, a laser trail or thin compression edges."""
        cv2 = self.cv2
        n, _labels, stats, _ = cv2.connectedComponentsWithStats(changed.view(np.uint8), 8)
        if n <= 6:
            return False
        st = stats[1:]
        solid = (st[:, cv2.CC_STAT_AREA] >= 20) & (st[:, cv2.CC_STAT_WIDTH] >= 2) & \
            (st[:, cv2.CC_STAT_HEIGHT] >= 2)
        return int(solid.sum()) >= 6

    def _scrolled(self, prev: np.ndarray, cur: np.ndarray,
                  box: Tuple[int, int, int, int]) -> bool:
        """True when the change is explained by moving the image (scroll/pan)."""
        cv2 = self.cv2
        x0, y0, x1, y1 = box
        # scrolling moves the whole page, not a small area of it
        if x1 - x0 < 0.4 * self.aw and y1 - y0 < 0.4 * self.ah:
            return False
        pad = 16
        y0, y1 = max(0, y0 - pad), min(self.ah, y1 + pad)
        x0, x1 = max(0, x0 - pad), min(self.aw, x1 + pad)
        a = cv2.cvtColor(prev[y0:y1, x0:x1], cv2.COLOR_BGR2GRAY)
        b = cv2.cvtColor(cur[y0:y1, x0:x1], cv2.COLOR_BGR2GRAY)
        h, w = a.shape
        if h < 16 or w < 16:
            return False
        scale = 2 if min(h, w) >= 64 else 1
        a_s = cv2.resize(a, (w // scale, h // scale), interpolation=cv2.INTER_AREA) \
            if scale > 1 else a
        b_s = cv2.resize(b, (w // scale, h // scale), interpolation=cv2.INTER_AREA) \
            if scale > 1 else b
        win = cv2.createHanningWindow((a_s.shape[1], a_s.shape[0]), cv2.CV_32F)
        (dx, dy), response = cv2.phaseCorrelate(a_s.astype(np.float32), b_s.astype(np.float32),
                                                win)
        sx, sy = int(round(dx * scale)), int(round(dy * scale))
        if response < 0.03 or (sx == 0 and sy == 0) or abs(sx) >= w - 8 or abs(sy) >= h - 8:
            return False
        before = np.count_nonzero(cv2.absdiff(a, b) > 30)
        if before == 0:
            return False
        best = before
        for ox, oy in ((sx, sy), (-sx, -sy)):
            # compare b[y, x] with a[y - oy, x - ox] on the overlapping part
            ya0, ya1 = max(0, -oy), min(h, h - oy)
            xa0, xa1 = max(0, -ox), min(w, w - ox)
            if ya1 - ya0 < 8 or xa1 - xa0 < 8:
                continue
            part_a = a[ya0:ya1, xa0:xa1]
            part_b = b[ya0 + oy:ya1 + oy, xa0 + ox:xa1 + ox]
            resid = np.count_nonzero(cv2.absdiff(part_a, part_b) > 30)
            # scale to the full area so that a shrinking overlap is not rewarded
            resid = resid * (h * w) / max(1, part_a.size)
            best = min(best, resid)
        return best < 0.3 * before

    # ------------------------------------------------------------------ main
    def feed(self, frame: np.ndarray) -> None:
        cv2 = self.cv2
        t = self.frame_index
        self.frame_index += 1
        if self.prev is None:
            self.prev = frame
            self.canvas = frame.copy()
            self._append(0.0, 0.0)
            return
        prev = self.prev
        self.prev = frame
        d3 = cv2.absdiff(frame, prev)
        # most frames of a screen recording change in a small area or not at
        # all: find it cheaply, then work on that part only
        box = self._bbox(cv2.threshold(cv2.cvtColor(d3, cv2.COLOR_BGR2GRAY), 0, 255,
                                       cv2.THRESH_BINARY)[1])
        n = 0
        nav = False
        if box is not None:
            x0, y0, x1, y1 = box
            d = cv2.transform(d3[y0:y1, x0:x1], np.ones((1, 3), np.float32))
            # even a small step counts as "still changing" (anti-aliased stroke
            # edges darken over several frames while the pen passes)
            moving = d > SETTLE_THRESHOLD
            self.since[y0:y1, x0:x1][moving] = t
            changed = (d > CHANGE_THRESHOLD) & self.active[y0:y1, x0:x1]
            n = int(np.count_nonzero(changed))
            if n:
                per_grid = np.bincount(self.grid_index[y0:y1, x0:x1][changed],
                                       minlength=GRID * GRID)
                hit = int(np.count_nonzero(per_grid >= 3))
                nav = max(n / self.area / NAV_FRACTION, hit / self.grid_cells / NAV_SPREAD) >= 1.0
                if not nav and n >= 40 and hit >= 2:
                    cbox = self._bbox(changed, x0, y0)
                    # a sparse page replaced at once (page turn, "clear page"):
                    # changes all over the page in the same instant, whereas the
                    # pen and the eraser work at one place at a time
                    nav = cbox is not None and hit >= 8 and \
                        cbox[2] - cbox[0] >= 0.5 * self.aw and \
                        cbox[3] - cbox[1] >= 0.4 * self.ah and self._scattered(changed)
                    if not nav and cbox is not None:
                        nav = self._scrolled(prev, frame, cbox)
            if not nav:
                self._update_live(moving, box)
                self.dirty = box if self.dirty is None else (
                    min(self.dirty[0], x0), min(self.dirty[1], y0),
                    max(self.dirty[2], x1), max(self.dirty[3], y1))
        frac = n / self.area
        if nav:
            self.nav_active = True
            self.calm = 0
            self._append(0.0, 1.0)
            return
        if self.nav_active:
            # the page seen once the navigation has settled is the new reference
            self.calm += 1
            if self.calm >= 2:
                assert self.canvas is not None
                self.canvas[:] = frame
                self.dirty = None
                self.chg_time[:] = -1
                self.cell_credit[:] = -1
                self.nav_active = False
            self._append(frac, 0.0)
            return
        self._commit(frame, t)
        self._append(frac, 0.0)

    def _update_live(self, moving: np.ndarray, box: Tuple[int, int, int, int]) -> None:
        """Track blocks that change in nearly every frame (camera noise, a
        moving person, a playing video) - a spot of the page only changes
        while it is written on.  Exact duplicate frames carry no information.
        (Compression keeps nudging pixels next to fresh strokes by a few
        levels for seconds; that is below the threshold of ``moving``.)"""
        cv2 = self.cv2
        x0, y0, x1, y1 = box
        hits = np.bincount(self.block_index[y0:y1, x0:x1][moving],
                           minlength=self.lh * self.lw)
        active = (hits / self.block_pixels) >= 0.016
        self.live_ema += LIVE_RATE * (active.astype(np.float32) - self.live_ema)
        live = np.where(self.live_blocks, self.live_ema >= LIVE_OFF, self.live_ema >= LIVE_ON)
        if np.array_equal(live, self.live_blocks):
            return
        self.live_blocks = live
        self.ever_live |= live
        grown = cv2.dilate(live.reshape(self.lh, self.lw).astype(np.uint8),
                           np.ones((3, 3), np.uint8)).ravel().astype(bool)
        self.active = self.mask & ~grown[self.block_index]

    def _threshold(self, box: Tuple[int, int, int, int]) -> np.ndarray:
        """Per pixel change threshold: higher in textured parts of the page
        (a photo on a slide), where compression alters pixels at every key
        frame, than on plain paper."""
        cv2 = self.cv2
        assert self.canvas is not None
        x0, y0, x1, y1 = box
        ex0, ey0 = max(0, x0 - 1), max(0, y0 - 1)
        ex1, ey1 = min(self.aw, x1 + 1), min(self.ah, y1 + 1)
        sub = self.canvas[ey0:ey1, ex0:ex1]
        k = np.ones((3, 3), np.uint8)
        rng = cv2.subtract(cv2.dilate(sub, k), cv2.erode(sub, k))
        local = cv2.transform(rng, np.ones((1, 3), np.float32))
        local = local[y0 - ey0:y0 - ey0 + (y1 - y0), x0 - ex0:x0 - ex0 + (x1 - x0)]
        return np.clip(local, CHANGE_THRESHOLD, 200).astype(np.uint8)

    def _commit(self, frame: np.ndarray, t: int) -> None:
        canvas = self.canvas
        assert canvas is not None
        if self.dirty is None:
            return
        x0, y0, x1, y1 = self.dirty
        diff = _sum_absdiff(frame[y0:y1, x0:x1], canvas[y0:y1, x0:x1])
        pending = (diff > self._threshold(self.dirty)) & self.active[y0:y1, x0:x1]
        if not pending.any():
            self.dirty = None
            return
        ready_all = pending & (self.since[y0:y1, x0:x1] <= t - (STABLE_FRAMES - 1))
        if not ready_all.any():
            return
        ready = self._drop_specks(ready_all)
        # settled specks (compression residue) join the reference silently
        specks = ready_all & ~ready
        if specks.any():
            canvas[y0:y1, x0:x1][specks] = frame[y0:y1, x0:x1][specks]
        rest = self._bbox(pending & ~ready_all, x0, y0)
        ys, xs = np.nonzero(ready)
        if len(ys):
            self._accept(frame, t, ys + y0, xs + x0)
        self.dirty = rest

    def _accept(self, frame: np.ndarray, t: int, ys: np.ndarray, xs: np.ndarray) -> None:
        """Pixels that changed and stayed changed: credit them as writing,
        or retract earlier credit when they only went back to what they were."""
        canvas = self.canvas
        assert canvas is not None
        new = frame[ys, xs]
        old = canvas[ys, xs]
        ct = self.chg_time[ys, xs]
        back = np.abs(new.astype(np.int16) - self.chg_prev[ys, xs].astype(np.int16)) \
            .sum(axis=1) <= CHANGE_THRESHOLD
        # undone shortly after it appeared: a laser dot, a cursor, a menu
        rev = (ct >= 0) & (t - ct <= self.revert_frames) & back
        cells = (ys // CELL) * self.cw + xs // CELL
        uc, inv = np.unique(cells, return_inverse=True)
        count = np.bincount(inv)
        cell_rev = np.bincount(inv, weights=rev.astype(np.float64)) * 2 >= count
        rc = uc[cell_rev]
        rc = rc[self.cell_credit[rc] >= 0]
        if len(rc):
            self._record(self.cell_credit[rc].copy(), rc, -self.cell_weight[rc])
            self.cell_credit[rc] = -1
        fresh = ~cell_rev
        if fresh.any():
            attr = np.full(len(uc), t, np.int64)
            np.minimum.at(attr, inv, self.since[ys, xs])
            attr = np.clip(attr, t - self.max_attr, t)[fresh].astype(np.int32)
            fc = uc[fresh]
            # a big block appearing at once (a pasted image, a panel) is not handwriting
            weight = 1.0 if len(fc) <= 0.08 * self.mask_cells else 0.2
            w = np.full(len(fc), weight, np.float32)
            self._record(attr, fc, w)
            self.cell_credit[fc] = attr
            self.cell_weight[fc] = w
        keep = ~rev
        self.chg_prev[ys[keep], xs[keep]] = old[keep]
        self.chg_time[ys[keep], xs[keep]] = t
        self.chg_time[ys[rev], xs[rev]] = -1
        canvas[ys, xs] = new

    def _record(self, frames: np.ndarray, cells: np.ndarray, weights: np.ndarray) -> None:
        self.ev_frames.append(np.asarray(frames, np.int32))
        self.ev_cells.append(np.asarray(cells, np.int32))
        self.ev_weights.append(np.asarray(weights, np.float32))

    def _append(self, motion: float, nav: float) -> None:
        self.motion.append(motion)
        self.nav.append(nav)

    # ------------------------------------------------------------------ result
    def ink_cells(self) -> np.ndarray:
        """Net written cells per frame after removing indicators and specks."""
        n = self.frame_index
        if not self.ev_frames or n == 0:
            return np.zeros(n, np.float64)
        frames = np.concatenate(self.ev_frames).astype(np.int64)
        cells = np.concatenate(self.ev_cells).astype(np.int64)
        weights = np.concatenate(self.ev_weights).astype(np.float64)
        key = frames * self.n_cells + cells
        uk, inv = np.unique(key, return_inverse=True)
        net = np.bincount(inv, weights=weights)
        ok = net > 1e-6
        uk, net = uk[ok], net[ok]
        f = (uk // self.n_cells).astype(np.int64)
        c = (uk % self.n_cells).astype(np.int64)
        if self.ever_live.any():
            # credited before the area was recognised as a camera image / video
            grown = self.cv2.dilate(self.ever_live.reshape(self.lh, self.lw).astype(np.uint8),
                                    np.ones((3, 3), np.uint8))
            by = np.minimum(self.lh - 1, (c // self.cw) * CELL * self.lh // self.ah)
            bx = np.minimum(self.lw - 1, (c % self.cw) * CELL * self.lw // self.aw)
            keep = grown[by, bx] == 0
            f, c, net = f[keep], c[keep], net[keep]
        win = max(1, int(round(ISOLATION_S * self.fps)))

        def isolated(per_frame: np.ndarray) -> np.ndarray:
            cs = np.concatenate([[0.0], np.cumsum(per_frame)])
            idx = np.arange(n)
            around = cs[np.minimum(n, idx + win + 1)] - cs[np.maximum(0, idx - win)] - per_frame
            return (per_frame > 0) & (around < 3.0)

        per_frame = np.bincount(f, weights=net, minlength=n)
        iso = isolated(per_frame)
        # small changes that keep coming back at the same place on their own:
        # a clock, a battery or recording indicator, a blinking icon
        small_iso = iso & (per_frame < 60)
        hits = np.bincount(c[small_iso[f]], minlength=self.n_cells)
        ticker = (hits >= TICKER_EVENTS).reshape(self.ch, self.cw).astype(np.uint8)
        if ticker.any():
            ticker = self.cv2.dilate(ticker, np.ones((3, 3), np.uint8)).astype(bool).ravel()
            keep = ~ticker[c]
            f, net = f[keep], net[keep]
        per_frame = np.bincount(f, weights=net, minlength=n)
        tiny = isolated(per_frame) & (per_frame < TINY_CELLS)
        per_frame[tiny] = 0.0
        return per_frame

    def finish(self):
        from .video import VideoFeatures

        n = self.frame_index
        cells = self.ink_cells()
        scale = max(0.5, SCREEN_MAX / max(self.aw, self.ah))
        ink = (cells * self.fps * INK_PER_CELL * scale).astype(np.float32)
        nav = np.asarray(self.nav, np.float32)
        motion = np.asarray(self.motion, np.float32)
        # continuous change for several seconds (a video playing, an animation,
        # dragging something around) is movement rather than navigation
        busy = nav > 0
        if busy.any():
            gap = 2
            idx = np.flatnonzero(busy)
            starts = [idx[0]]
            ends = []
            for a, b in zip(idx[:-1], idx[1:]):
                if b - a > gap + 1:
                    ends.append(a + 1)
                    starts.append(b)
            ends.append(idx[-1] + 1)
            limit = int(SUSTAINED_S * self.fps)
            for s, e in zip(starts, ends):
                if e - s > limit:
                    nav[s:e] = 0.0
                    motion[s:e] = np.maximum(motion[s:e], 0.05)
        zeros = np.zeros(n, np.float32)
        return VideoFeatures(fps=self.fps, ink=ink, motion=motion, hand=zeros.copy(),
                             presence=zeros.copy(), global_change=zeros.copy(),
                             width=self.aw, height=self.ah, polarity=0, nav=nav)
