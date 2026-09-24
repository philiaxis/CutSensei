"""Run the complete analysis (audio and video in parallel)."""

from __future__ import annotations

import os
import shutil
import threading
from typing import List, Optional, Sequence

import numpy as np

from ..core.jobs import CancelToken, Progress, ProgressCallback
from ..core.media import MediaInfo
from ..core.paths import thumbnails_dir
from ..core.settings import VadEngine
from . import audio as audio_mod
from . import video as video_mod
from .result import HOP, AnalysisResult


def analyze(media: MediaInfo, regions: Sequence[Sequence[float]] = (),
            engine: str = VadEngine.AUTO,
            progress: Optional[ProgressCallback] = None,
            cancel: Optional[CancelToken] = None,
            hw_decode: bool = False,
            make_thumbnails: bool = False,
            previous: Optional[AnalysisResult] = None,
            reuse_audio: bool = False) -> AnalysisResult:
    """Analyse ``media``.  With ``reuse_audio`` the audio features of
    ``previous`` are kept (e.g. when only the board region changed)."""
    cancel = cancel or CancelToken()
    prog = Progress(progress)
    n = AnalysisResult.frames_for(media.duration, HOP)
    res = AnalysisResult(duration=media.duration, hop=HOP)
    res.board_regions = [list(map(float, r)) for r in regions]

    fractions = {"audio": 0.0, "video": 0.0}
    weights = {"audio": 0.25, "video": 0.75}
    lock = threading.Lock()

    def report(part: str, value: float) -> None:
        with lock:
            fractions[part] = value
            total = sum(fractions[k] * weights[k] for k in fractions)
        prog(0.97 * total, "analyzing")

    errors: List[BaseException] = []
    audio_summary: List[audio_mod.AudioSummary] = []
    video_out: List = []

    can_reuse = (reuse_audio and previous is not None and previous.has_audio
                 and len(previous.speech) == n)

    def run_audio() -> None:
        try:
            if can_reuse:
                assert previous is not None
                audio_summary.append(audio_mod.AudioSummary(
                    previous.speech, previous.voicing, previous.loudness, previous.clicks,
                    previous.wave_peaks))
                res.vad_engine = previous.vad_engine
            elif media.has_audio:
                feats = audio_mod.extract_audio_features(
                    media, engine, progress=lambda f: report("audio", f), cancel=cancel,
                    use_gpu=hw_decode)
                audio_summary.append(audio_mod.summarize(feats, n, engine))
                res.vad_engine = "silero" if feats.silero is not None else "dsp"
            else:
                audio_summary.append(audio_mod.silent_summary(n, media.duration))
            report("audio", 1.0)
        except BaseException as exc:  # propagated below
            errors.append(exc)
            cancel.cancel()

    thumbs: Optional[str] = None
    if make_thumbnails:
        thumbs = str(thumbnails_dir(media.fingerprint()))
        shutil.rmtree(thumbs, ignore_errors=True)
        os.makedirs(thumbs, exist_ok=True)

    def run_video() -> None:
        try:
            video_out.append(video_mod.extract_video_features(
                media, regions, thumbs_dir=thumbs, progress=lambda f: report("video", f),
                cancel=cancel, hw_decode=hw_decode))
            report("video", 1.0)
        except BaseException as exc:
            errors.append(exc)
            cancel.cancel()

    threads = [threading.Thread(target=run_audio, daemon=True),
               threading.Thread(target=run_video, daemon=True)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    if errors:
        # a cancellation caused by another thread's error is not the root cause
        from ..core.errors import Cancelled

        real = [e for e in errors if not isinstance(e, Cancelled)]
        raise (real[0] if real else errors[0])
    cancel.check()

    au = audio_summary[0]
    res.speech, res.voicing, res.loudness, res.clicks = au.speech, au.voicing, au.loudness, \
        au.clicks
    res.wave_peaks = au.wave_peaks
    res.has_audio = media.has_audio

    vf, thumb_int, n_thumbs = video_out[0]
    res.ink = video_mod.resample_to_hop(np.maximum(vf.ink, 0), vf.fps, n, HOP)
    res.motion = video_mod.resample_to_hop(vf.motion, vf.fps, n, HOP)
    res.hand = video_mod.resample_to_hop(vf.hand, vf.fps, n, HOP)
    res.global_change = video_mod.resample_to_hop(vf.global_change, vf.fps, n, HOP)
    res.has_video_features = len(vf.ink) > 0
    res.thumb_interval = thumb_int
    res.thumb_count = n_thumbs
    res.thumb_fingerprint = media.fingerprint()
    res.meta = {"polarity": vf.polarity, "analysis_size": [vf.width, vf.height]}
    res.ensure_lengths()
    prog(1.0, "done")
    return res
