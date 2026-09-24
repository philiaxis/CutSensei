import os
import subprocess
import sys
import wave

import numpy as np
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_LOGGING_RULES", "qt.multimedia.*=false")


@pytest.fixture(scope="session", autouse=True)
def _isolated_cache(tmp_path_factory):
    cache = tmp_path_factory.mktemp("cache")
    os.environ["CUTSENSEI_CACHE_DIR"] = str(cache)
    # never touch the real user settings (recent files, defaults) from tests
    os.environ["CUTSENSEI_SETTINGS"] = str(cache / "settings.ini")
    yield


@pytest.fixture(scope="session")
def ffmpeg_exe():
    from cutsensei.core import ffmpeg

    return ffmpeg.find_ffmpeg()


@pytest.fixture(scope="session")
def demo_video(tmp_path_factory):
    """The 60 s speech / 40 s writing / 20 s idle scenario of the design doc."""
    from cutsensei.demo import DemoSpec, generate_demo

    path = str(tmp_path_factory.mktemp("demo") / "lecture.mp4")
    spec = generate_demo(path, DemoSpec())
    return path, spec


@pytest.fixture(scope="session")
def short_video(tmp_path_factory):
    """A short demo (6 s speech, 8 s writing, 6 s idle) for quicker tests."""
    from cutsensei.demo import DemoSpec, Scene, generate_demo

    path = str(tmp_path_factory.mktemp("short") / "short.mp4")
    spec = DemoSpec(scenes=[Scene("speech", 6.0), Scene("writing", 8.0), Scene("idle", 6.0)],
                    width=320, height=180)
    generate_demo(path, spec)
    return path, spec


@pytest.fixture(scope="session")
def screen_demo_video(tmp_path_factory):
    """The 60 s / 40 s / 20 s scenario as a tablet screen recording, analysed."""
    from cutsensei.analysis.pipeline import analyze
    from cutsensei.core.media import probe
    from cutsensei.demo import generate_demo, screen_spec

    path = str(tmp_path_factory.mktemp("screen") / "notes.mp4")
    generate_demo(path, screen_spec())
    media = probe(path)
    return media, analyze(media, [])


@pytest.fixture(scope="session")
def screen_webcam_video(tmp_path_factory):
    """A screen recording with a webcam picture, laser pointer, scrolling, a page
    turn and a status bar clock that changes every 15 s."""
    from cutsensei.demo import Scene, generate_demo, screen_spec

    path = str(tmp_path_factory.mktemp("screen_mixed") / "notes_webcam.mp4")
    spec = screen_spec([Scene("speech", 10), Scene("speech_laser", 6), Scene("writing", 12),
                        Scene("idle", 10), Scene("speech_writing", 8), Scene("scroll", 3),
                        Scene("writing", 10), Scene("page", 2), Scene("writing", 6),
                        Scene("idle", 10), Scene("laser", 6), Scene("idle", 8),
                        Scene("speech", 6)], webcam=True, clock_period=15, width=800,
                       height=600)
    generate_demo(path, spec)
    return path, spec


def _frame_pattern(n: int, w: int, h: int) -> np.ndarray:
    """16 bit frame counter as 8x2 black/white blocks."""
    img = np.zeros((h, w, 3), np.uint8)
    img[:] = 60
    bw, bh = w // 8, h // 2
    for bit in range(16):
        on = (n >> bit) & 1
        x, y = (bit % 8) * bw, (bit // 8) * bh
        img[y + 4:y + bh - 4, x + 4:x + bw - 4] = 235 if on else 15
    return img


def decode_frame_pattern(img: np.ndarray) -> int:
    h, w = img.shape[:2]
    bw, bh = w // 8, h // 2
    gray = img.mean(axis=2) if img.ndim == 3 else img
    n = 0
    for bit in range(16):
        x, y = (bit % 8) * bw, (bit // 8) * bh
        v = gray[y + bh // 3:y + 2 * bh // 3, x + bw // 3:x + 2 * bw // 3].mean()
        if v > 128:
            n |= 1 << bit
    return n


SYNC_SR = 48000
SYNC_FPS = 30
SYNC_DUR = 24


def tone_freq(k: int) -> float:
    return 400.0 + 150.0 * k


@pytest.fixture(scope="session")
def sync_video(tmp_path_factory, ffmpeg_exe):
    """Frames carry their index; every second k has a 60 ms tone of tone_freq(k)."""
    d = tmp_path_factory.mktemp("sync")
    path = str(d / "sync.mp4")
    wav = str(d / "sync.wav")
    n = SYNC_DUR * SYNC_SR
    audio = np.zeros(n, np.float32)
    for k in range(SYNC_DUR):
        i0 = int(k * SYNC_SR)
        m = int(0.06 * SYNC_SR)
        t = np.arange(m) / SYNC_SR
        audio[i0:i0 + m] += 0.5 * np.sin(2 * np.pi * tone_freq(k) * t) * np.hanning(m)
    with wave.open(wav, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SYNC_SR)
        wf.writeframes((audio * 32767).astype("<i2").tobytes())
    w, h = 320, 96
    cmd = [ffmpeg_exe, "-hide_banner", "-loglevel", "error", "-y", "-f", "rawvideo",
           "-pix_fmt", "bgr24", "-s", f"{w}x{h}", "-r", str(SYNC_FPS), "-i", "pipe:0",
           "-i", wav, "-c:v", "libx264", "-preset", "ultrafast", "-crf", "18",
           "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-shortest", path]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    for i in range(SYNC_DUR * SYNC_FPS):
        proc.stdin.write(_frame_pattern(i, w, h).tobytes())
    proc.stdin.close()
    assert proc.wait() == 0
    return path
