import numpy as np
import pytest

from cutsensei.analysis.audio import (FEATURE_RATE, SR, AudioFeatures, _StreamingDsp,
                                      detect_clicks, summarize)
from cutsensei.demo import _chalk, _speech
from cutsensei.render.audio_render import wsola


def dominant_freq(x, sr):
    spec = np.abs(np.fft.rfft(x * np.hanning(len(x))))
    return np.fft.rfftfreq(len(x), 1 / sr)[np.argmax(spec)]


@pytest.mark.parametrize("speed", [1.5, 2.0, 4.0, 8.0])
def test_wsola_length_and_pitch(speed):
    sr = 48000
    t = np.arange(int(sr * 2.0)) / sr
    x = (0.5 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)[:, None]
    target = int(round(len(x) / speed))
    y = wsola(x, speed, sr, target)
    assert y.shape == (target, 1)
    mid = y[len(y) // 4: 3 * len(y) // 4, 0]
    assert abs(dominant_freq(mid, sr) - 440) < 15   # pitch preserved
    assert 0.2 < np.sqrt(np.mean(mid ** 2)) < 0.5   # no big level change


def test_wsola_exact_output_length():
    x = np.random.default_rng(0).normal(0, 0.1, (10007, 2)).astype(np.float32)
    for n in (1, 100, 2501, 5000):
        assert wsola(x, 4.0, 48000, n).shape == (n, 2)


def _features(signal: np.ndarray) -> AudioFeatures:
    dsp = _StreamingDsp()
    for i in range(0, len(signal), SR):
        dsp.feed(signal[i:i + SR].astype(np.float32))
    dsp.finish()
    cat = lambda parts, dt: np.concatenate(parts).astype(dt)  # noqa: E731
    return AudioFeatures(cat(dsp.energy, np.float32), cat(dsp.voicing, np.float32),
                         cat(dsp.voiced_ok, bool), cat(dsp.flatness, np.float32),
                         cat(dsp.env, np.float32), np.clip(cat(dsp.peaks, np.float32), 0, 1),
                         None)


def _resample(x48):
    # the demo generator works at 48 kHz; decimate to 16 kHz for the detector
    return x48[::3]


def test_dsp_separates_speech_from_chalk():
    rng = np.random.default_rng(3)
    speech = _resample(_speech(8.0, rng))
    chalk = _resample(_chalk(8.0, rng))
    noise = rng.normal(0, 0.001, len(speech) + len(chalk)).astype(np.float32)
    signal = np.concatenate([speech, chalk]) + noise
    feats = _features(signal)
    n = int(len(signal) / SR / 0.1)
    summ = summarize(feats, n, "dsp")
    half = n // 2
    assert summ.speech[: half].mean() > 0.5
    assert summ.speech[half + 5:].mean() < 0.1
    # clicks are only counted meaningfully during the chalk part
    assert summ.clicks[half:].mean() > 1.0


def test_detect_clicks_finds_isolated_impulses():
    env = np.full(5000, -70.0, np.float32)
    for pos in (1000, 2000, 3000):
        env[pos:pos + 5] = -30.0
        env[pos + 5:pos + 15] = np.linspace(-40, -70, 10)
    clicks = detect_clicks(env)
    found = np.flatnonzero(clicks)
    assert len(found) == 3
    assert all(abs(f - p) <= 5 for f, p in zip(found, (1000, 2000, 3000)))
    # a sustained loud sound is not a click
    env2 = np.full(5000, -70.0, np.float32)
    env2[1000:3000] = -30.0
    assert detect_clicks(env2).sum() <= 1
