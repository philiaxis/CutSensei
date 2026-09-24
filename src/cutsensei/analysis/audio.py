"""Audio analysis: speech detection that is robust to chalk/pen noise.

Two speech detectors are available:

* **Silero VAD** (bundled ONNX model, MIT licence) - a small neural network
  that is very good at telling speech from noise.
* **DSP detector** - voicing (periodicity in the 70-400 Hz pitch range) in
  combination with the signal-to-noise ratio.  Chalk "tap tap tap" sounds are
  short broadband impulses without pitch, so they are not voiced.

Independently of speech, short impulsive clicks (typical for chalk and pen
strokes) are counted; they are later used as additional evidence for board
writing during silent parts.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable, List, Optional

import numpy as np

from ..core import ffmpeg
from ..core.jobs import CancelToken
from ..core.media import MediaInfo
from ..core.paths import resource_path
from ..core.settings import VadEngine

SR = 16000
HOP = 160           # 10 ms feature frames
WIN = 512           # 32 ms analysis window
NFFT = 1024
ENV_HOP = 16        # 1 ms envelope for click detection
FEATURE_RATE = SR // HOP  # 100 Hz

_MIN_LAG = 40       # 400 Hz
_MAX_LAG = 228      # ~70 Hz
_OCTAVE_LAG = 16    # 1 kHz - used to reject tonal squeaks


def silero_model_path() -> str:
    return str(resource_path("models", "silero_vad_16k.onnx"))


def silero_available() -> bool:
    try:
        import onnxruntime  # noqa: F401
    except Exception:
        return False
    return os.path.isfile(silero_model_path())


class SileroVad:
    """Streaming wrapper around the Silero VAD v5 ONNX model (16 kHz)."""

    CHUNK = 512
    CONTEXT = 64

    def __init__(self, use_gpu: bool = False) -> None:
        import onnxruntime as ort

        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 1
        opts.inter_op_num_threads = 1
        opts.log_severity_level = 3
        providers = ["CPUExecutionProvider"]
        if use_gpu and "CUDAExecutionProvider" in ort.get_available_providers():
            providers.insert(0, "CUDAExecutionProvider")
        self.session = ort.InferenceSession(silero_model_path(), sess_options=opts,
                                            providers=providers)
        self.state = np.zeros((2, 1, 128), np.float32)
        self.context = np.zeros((1, self.CONTEXT), np.float32)
        self.sr = np.array(SR, dtype=np.int64)
        self._pending = np.zeros(0, np.float32)
        self.probs: List[float] = []

    def feed(self, samples: np.ndarray) -> None:
        buf = np.concatenate([self._pending, samples.astype(np.float32, copy=False)])
        n = len(buf) // self.CHUNK
        for i in range(n):
            chunk = buf[i * self.CHUNK:(i + 1) * self.CHUNK][None, :]
            x = np.concatenate([self.context, chunk], axis=1)
            out, self.state = self.session.run(None, {"input": x, "state": self.state,
                                                      "sr": self.sr})
            self.context = x[:, -self.CONTEXT:]
            self.probs.append(float(out[0, 0]))
        self._pending = buf[n * self.CHUNK:]

    def finish(self) -> np.ndarray:
        if len(self._pending):
            pad = np.zeros(self.CHUNK - len(self._pending), np.float32)
            self.feed(pad)
        return np.asarray(self.probs, np.float32)


@dataclass
class AudioFeatures:
    """Raw features at 100 Hz plus the 1 kHz high-pass envelope."""
    energy_db: np.ndarray
    voicing: np.ndarray
    voiced_ok: np.ndarray      # pitch in speech range (not a tonal squeak)
    flatness: np.ndarray
    env_db: np.ndarray         # 1 kHz high frequency envelope
    peaks: np.ndarray          # 100 Hz waveform peak (0..1)
    silero: Optional[np.ndarray]  # 31.25 Hz speech probabilities


class _StreamingDsp:
    def __init__(self) -> None:
        # pad so that frame k is centred at (k + 0.5) * HOP
        self._buf = np.zeros(WIN // 2 - HOP // 2, np.float32)
        self._win = np.hanning(WIN).astype(np.float32)
        w_spec = np.abs(np.fft.rfft(self._win, NFFT)) ** 2
        w_ac = np.fft.irfft(w_spec, NFFT)[:_MAX_LAG + 1]
        self._w_ac = (w_ac / w_ac[0]).astype(np.float32)
        freqs = np.fft.rfftfreq(NFFT, 1.0 / SR)
        self._band = (freqs >= 60) & (freqs <= 1500)
        self._flat_band = (freqs >= 100) & (freqs <= 4000)
        self.energy: List[np.ndarray] = []
        self.voicing: List[np.ndarray] = []
        self.voiced_ok: List[np.ndarray] = []
        self.flatness: List[np.ndarray] = []
        # envelope / peaks operate on unpadded samples
        self._env_rest = np.zeros(0, np.float32)
        self._last_sample = np.float32(0.0)
        self.env: List[np.ndarray] = []
        self._peak_rest = np.zeros(0, np.float32)
        self.peaks: List[np.ndarray] = []

    def feed(self, x: np.ndarray) -> None:
        self._feed_frames(x)
        self._feed_envelope(x)
        self._feed_peaks(x)

    def _feed_frames(self, x: np.ndarray) -> None:
        buf = np.concatenate([self._buf, x])
        if len(buf) < WIN:
            self._buf = buf
            return
        n = (len(buf) - WIN) // HOP + 1
        view = np.lib.stride_tricks.sliding_window_view(buf, WIN)[::HOP][:n]
        for s in range(0, n, 4096):
            self._process(np.ascontiguousarray(view[s:s + 4096]))
        self._buf = buf[n * HOP:]

    def _process(self, frames: np.ndarray) -> None:
        energy = np.mean(frames.astype(np.float64) ** 2, axis=1)
        self.energy.append((10 * np.log10(energy + 1e-10)).astype(np.float32))
        spec = np.fft.rfft(frames * self._win, NFFT)
        power = (spec.real ** 2 + spec.imag ** 2).astype(np.float32)
        fb = power[:, self._flat_band] + 1e-12
        flat = np.exp(np.mean(np.log(fb), axis=1)) / np.mean(fb, axis=1)
        self.flatness.append(flat.astype(np.float32))
        banded = power * self._band
        ac = np.fft.irfft(banded, NFFT)[:, :_MAX_LAG + 1].astype(np.float32)
        r0 = ac[:, :1] + 1e-9
        rn = ac / r0 / np.maximum(self._w_ac, 1e-3)
        seg = rn[:, _MIN_LAG:_MAX_LAG + 1]
        best = seg.max(axis=1)
        # octave check: a strong peak at a lag shorter than the pitch range means
        # a high pitched tone (marker squeak), not a voice.
        short = rn[:, _OCTAVE_LAG:_MIN_LAG]
        local_max = (short[:, 1:-1] > short[:, :-2]) & (short[:, 1:-1] >= short[:, 2:])
        short_peak = np.where(local_max, short[:, 1:-1], -1.0).max(axis=1)
        ok = short_peak < 0.9 * best
        self.voicing.append(np.clip(best, 0, 1).astype(np.float32))
        self.voiced_ok.append(ok)

    def _feed_envelope(self, x: np.ndarray) -> None:
        # first difference = simple high-pass emphasising clicks (>2 kHz)
        hp = np.diff(np.concatenate([[self._last_sample], x]))
        self._last_sample = x[-1] if len(x) else self._last_sample
        buf = np.concatenate([self._env_rest, hp.astype(np.float32)])
        n = len(buf) // ENV_HOP
        if n:
            blocks = buf[:n * ENV_HOP].reshape(n, ENV_HOP)
            env = 10 * np.log10(np.mean(blocks.astype(np.float64) ** 2, axis=1) + 1e-12)
            self.env.append(env.astype(np.float32))
        self._env_rest = buf[n * ENV_HOP:]

    def _feed_peaks(self, x: np.ndarray) -> None:
        buf = np.concatenate([self._peak_rest, x])
        n = len(buf) // HOP
        if n:
            self.peaks.append(np.abs(buf[:n * HOP]).reshape(n, HOP).max(axis=1))
        self._peak_rest = buf[n * HOP:]

    def finish(self) -> None:
        tail = np.zeros(WIN, np.float32)
        self._feed_frames(tail[: WIN - HOP])
        if len(self._peak_rest):
            self.peaks.append(np.array([np.abs(self._peak_rest).max()], np.float32))
            self._peak_rest = np.zeros(0, np.float32)


def _cat(parts: List[np.ndarray], dtype) -> np.ndarray:
    if not parts:
        return np.zeros(0, dtype)
    return np.concatenate(parts).astype(dtype)


def extract_audio_features(media: MediaInfo, engine: str = VadEngine.AUTO,
                           progress: Optional[Callable[[float], None]] = None,
                           cancel: Optional[CancelToken] = None,
                           use_gpu: bool = False) -> AudioFeatures:
    """Decode the audio track at 16 kHz mono and compute raw features."""
    use_silero = engine in (VadEngine.AUTO, VadEngine.SILERO) and silero_available()
    if engine == VadEngine.SILERO and not use_silero:
        raise RuntimeError("Silero VAD is not available (onnxruntime missing).")
    vad = SileroVad(use_gpu=use_gpu) if use_silero else None
    dsp = _StreamingDsp()
    args = ["-i", media.path, "-vn", "-sn", "-dn", "-map", "0:a:0",
            "-af", f"aresample={SR}:async=1:first_pts=0", "-ac", "1",
            "-f", "f32le", "pipe:1"]
    block = SR * 10  # 10 seconds per read
    done = 0
    total = max(1, int(media.duration * SR))
    with ffmpeg.RawPipeReader(args, cancel=cancel) as reader:
        for raw in reader.read_blocks(block * 4):
            x = np.frombuffer(raw, dtype=np.float32)
            dsp.feed(x)
            if vad is not None:
                vad.feed(x)
            done += len(x)
            if progress is not None:
                progress(min(1.0, done / total))
    dsp.finish()
    silero = vad.finish() if vad is not None else None
    peaks = _cat(dsp.peaks, np.float32)
    return AudioFeatures(
        energy_db=_cat(dsp.energy, np.float32),
        voicing=_cat(dsp.voicing, np.float32),
        voiced_ok=_cat(dsp.voiced_ok, bool),
        flatness=_cat(dsp.flatness, np.float32),
        env_db=_cat(dsp.env, np.float32),
        peaks=np.clip(peaks, 0, 1),
        silero=silero,
    )


# --------------------------------------------------------------------------
# post processing
# --------------------------------------------------------------------------

def moving_average(x: np.ndarray, width: int) -> np.ndarray:
    if width <= 1 or len(x) == 0:
        return x.astype(np.float32)
    kernel = np.ones(width, np.float64) / width
    pad = width // 2
    xp = np.pad(x.astype(np.float64), (pad, width - 1 - pad), mode="edge")
    return np.convolve(xp, kernel, mode="valid").astype(np.float32)


def rolling_percentile(x: np.ndarray, rate: float, window_s: float, q: float,
                       step_s: float = 1.0) -> np.ndarray:
    """Coarse sliding percentile (computed every ``step_s`` and interpolated)."""
    n = len(x)
    if n == 0:
        return x
    step = max(1, int(step_s * rate))
    half = max(1, int(window_s * rate / 2))
    centers = np.arange(0, n, step)
    vals = np.empty(len(centers), np.float32)
    for i, c in enumerate(centers):
        vals[i] = np.percentile(x[max(0, c - half):min(n, c + half)], q)
    return np.interp(np.arange(n), centers, vals).astype(np.float32)


def detect_clicks(env_db: np.ndarray) -> np.ndarray:
    """Return a boolean array (1 kHz) marking impulsive click onsets."""
    n = len(env_db)
    if n < 64:
        return np.zeros(n, bool)
    floor = rolling_percentile(env_db, 1000.0, 20.0, 20.0, step_s=1.0)
    e = env_db
    # background just before the event: minimum over the preceding 3..30 ms
    pre = np.full(n, np.inf, np.float32)
    for lag in range(3, 31, 3):
        pre[lag:] = np.minimum(pre[lag:], e[:-lag])
    # level shortly after: the burst should have decayed within ~25 ms
    post = np.full(n, np.inf, np.float32)
    post[:-25] = e[25:]
    # local maximum within +-8 ms
    local = np.ones(n, bool)
    for lag in range(1, 9):
        local[lag:] &= e[lag:] > e[:-lag]      # strictly above what came before
        local[:-lag] &= e[:-lag] >= e[lag:]
    return local & (e - pre > 12.0) & (e - post > 8.0) & (e - floor > 15.0)


@dataclass
class AudioSummary:
    """Features resampled to the analysis hop (0.1 s)."""
    speech: np.ndarray
    voicing: np.ndarray
    loudness: np.ndarray
    clicks: np.ndarray
    wave_peaks: np.ndarray   # uint8 at 100 Hz


def _to_hop(x100: np.ndarray, n: int, reducer: str = "mean") -> np.ndarray:
    per = int(round(0.1 * FEATURE_RATE))
    need = n * per
    if len(x100) < need:
        pad_val = x100[-1] if len(x100) else 0.0
        x100 = np.concatenate([x100, np.full(need - len(x100), pad_val, x100.dtype)])
    blocks = x100[:need].reshape(n, per)
    if reducer == "max":
        return blocks.max(axis=1).astype(np.float32)
    return blocks.mean(axis=1).astype(np.float32)


def summarize(features: AudioFeatures, n_frames: int, engine: str) -> AudioSummary:
    energy = features.energy_db
    floor = rolling_percentile(energy, FEATURE_RATE, 30.0, 10.0)
    snr = energy - floor
    # absolute gate: digital silence / very quiet recordings
    loud_enough = energy > -65.0
    voiced = (features.voicing > 0.45) & features.voiced_ok & (snr > 6.0) & loud_enough \
        & (features.flatness < 0.45)
    vfrac = moving_average(voiced.astype(np.float32), 30)  # 300 ms
    dsp_speech = np.clip((vfrac - 0.06) / 0.22, 0.0, 1.0)

    speech100 = dsp_speech
    if features.silero is not None and len(features.silero) and engine != VadEngine.DSP:
        # silero produces one value per 32 ms chunk
        t_sil = (np.arange(len(features.silero)) + 0.5) * (SileroVad.CHUNK / SR)
        t100 = (np.arange(len(energy)) + 0.5) / FEATURE_RATE
        sil = np.interp(t100, t_sil, features.silero).astype(np.float32)
        # voicing nearby (+-0.4 s) strengthens the neural estimate; isolated
        # clicks that fool the network get damped.
        near_voiced = moving_average(voiced.astype(np.float32), 80) > 0.02
        speech100 = np.where(near_voiced, np.maximum(sil, 0.6 * dsp_speech), sil * 0.75)
        speech100 = speech100.astype(np.float32)

    clicks1k = detect_clicks(features.env_db).astype(np.float32)
    # clicks per second, evaluated per 10 ms then per hop
    n100 = len(energy)
    need = n100 * 10
    c = clicks1k[:need]
    if len(c) < need:
        c = np.concatenate([c, np.zeros(need - len(c), np.float32)])
    clicks100 = c.reshape(n100, 10).sum(axis=1) * FEATURE_RATE

    peaks = features.peaks
    wave = np.clip(np.sqrt(peaks) * 255.0, 0, 255).astype(np.uint8)

    return AudioSummary(
        speech=_to_hop(speech100, n_frames),
        voicing=_to_hop(voiced.astype(np.float32), n_frames),
        loudness=_to_hop(energy, n_frames),
        clicks=_to_hop(clicks100.astype(np.float32), n_frames),
        wave_peaks=wave,
    )


def silent_summary(n_frames: int, duration: float) -> AudioSummary:
    z = np.zeros(n_frames, np.float32)
    return AudioSummary(z, z.copy(), np.full(n_frames, -90.0, np.float32), z.copy(),
                        np.zeros(int(duration * FEATURE_RATE) + 1, np.uint8))

