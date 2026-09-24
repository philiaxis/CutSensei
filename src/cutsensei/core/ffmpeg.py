"""Locating and running FFmpeg in a platform independent way.

Lookup order for the ``ffmpeg`` executable:

1. a path configured at runtime with :func:`set_ffmpeg_path`
2. the ``CUTSENSEI_FFMPEG`` environment variable
3. a binary shipped next to a frozen (PyInstaller) build
4. ``ffmpeg`` on ``PATH``
5. the static binary bundled with the ``imageio-ffmpeg`` package

``ffprobe`` is optional; when it is missing, media information is parsed
from ``ffmpeg -i`` output instead (see :mod:`cutsensei.core.media`).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import threading
from functools import lru_cache
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from .errors import Cancelled, FFmpegError, FFmpegNotFoundError
from .jobs import CancelToken

IS_WINDOWS = sys.platform.startswith("win")
IS_MACOS = sys.platform == "darwin"
EXE_SUFFIX = ".exe" if IS_WINDOWS else ""

_configured_ffmpeg: Optional[str] = None


def set_ffmpeg_path(path: Optional[str]) -> None:
    """Override the ffmpeg executable (``None`` restores automatic lookup)."""
    global _configured_ffmpeg
    _configured_ffmpeg = path or None
    find_ffmpeg.cache_clear()
    find_ffprobe.cache_clear()
    ffmpeg_version.cache_clear()
    available_encoders.cache_clear()
    _working_encoder_cache.clear()


def _frozen_dirs() -> List[Path]:
    dirs: List[Path] = []
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        dirs += [base / "ffmpeg", base, Path(sys.executable).parent]
    return dirs


def _is_executable(path: Optional[str]) -> bool:
    return bool(path) and os.path.isfile(path) and os.access(path, os.X_OK)


@lru_cache(maxsize=1)
def find_ffmpeg() -> str:
    candidates: List[Optional[str]] = [_configured_ffmpeg, os.environ.get("CUTSENSEI_FFMPEG")]
    candidates += [str(d / f"ffmpeg{EXE_SUFFIX}") for d in _frozen_dirs()]
    candidates.append(shutil.which("ffmpeg"))
    for cand in candidates:
        if _is_executable(cand):
            return str(cand)
    try:
        import imageio_ffmpeg  # type: ignore

        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if _is_executable(exe):
            return exe
    except Exception:  # pragma: no cover - depends on the environment
        pass
    raise FFmpegNotFoundError(
        "FFmpeg was not found. Install FFmpeg and add it to PATH, set the "
        "CUTSENSEI_FFMPEG environment variable, or `pip install imageio-ffmpeg`."
    )


@lru_cache(maxsize=1)
def find_ffprobe() -> Optional[str]:
    candidates: List[Optional[str]] = [os.environ.get("CUTSENSEI_FFPROBE")]
    try:
        ffmpeg_dir = Path(find_ffmpeg()).parent
        candidates.append(str(ffmpeg_dir / f"ffprobe{EXE_SUFFIX}"))
    except FFmpegNotFoundError:
        pass
    candidates += [str(d / f"ffprobe{EXE_SUFFIX}") for d in _frozen_dirs()]
    candidates.append(shutil.which("ffprobe"))
    for cand in candidates:
        if _is_executable(cand):
            return str(cand)
    return None


def _creation_flags() -> int:
    # Avoid flashing console windows when running from the GUI on Windows.
    if IS_WINDOWS:
        return getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    return 0


def popen(args: Sequence[str], **kwargs) -> subprocess.Popen:
    kwargs.setdefault("creationflags", _creation_flags())
    return subprocess.Popen(list(args), **kwargs)


def run(args: Sequence[str], timeout: Optional[float] = None,
        check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        list(args), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL, timeout=timeout, creationflags=_creation_flags(),
    )
    if check and proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace")
        raise FFmpegError(f"{Path(args[0]).name} failed (exit {proc.returncode})", err)
    return proc


@lru_cache(maxsize=4)
def ffmpeg_version(exe: Optional[str] = None) -> Tuple[int, int]:
    exe = exe or find_ffmpeg()
    try:
        out = run([exe, "-hide_banner", "-version"], timeout=20).stdout.decode("utf-8", "replace")
    except Exception:
        return (0, 0)
    m = re.search(r"ffmpeg version\s+n?(\d+)\.(\d+)", out)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = re.search(r"ffmpeg version\s+n?(\d+)", out)
    if m:
        return int(m.group(1)), 0
    # Git snapshot builds ("ffmpeg version N-112345-g...") are recent.
    return (99, 0)


def filter_complex_args(script_path: str) -> List[str]:
    """Arguments loading a filter graph from a file.

    ``-filter_complex_script`` is deprecated since FFmpeg 7 in favour of the
    ``-/filter_complex`` syntax, so pick the right one for the binary in use.
    """
    if ffmpeg_version() >= (7, 0):
        return ["-/filter_complex", script_path]
    return ["-filter_complex_script", script_path]


@lru_cache(maxsize=1)
def available_encoders() -> Dict[str, str]:
    """Return ``{name: description}`` of the video/audio encoders compiled in."""
    out = run([find_ffmpeg(), "-hide_banner", "-encoders"], timeout=30).stdout.decode(
        "utf-8", "replace")
    encoders: Dict[str, str] = {}
    for line in out.splitlines():
        m = re.match(r"\s*([VAS][A-Z.]{5})\s+(\S+)\s+(.*)", line)
        if m:
            encoders[m.group(2)] = m.group(3).strip()
    return encoders


@lru_cache(maxsize=1)
def available_hwaccels() -> List[str]:
    try:
        out = run([find_ffmpeg(), "-hide_banner", "-hwaccels"], timeout=30).stdout.decode(
            "utf-8", "replace")
    except Exception:
        return []
    lines = [ln.strip() for ln in out.splitlines()]
    if "Hardware acceleration methods:" in lines:
        lines = lines[lines.index("Hardware acceleration methods:") + 1:]
    return [ln for ln in lines if ln]


_working_encoder_cache: Dict[str, bool] = {}
_encoder_lock = threading.Lock()


def encoder_works(name: str) -> bool:
    """Check that an encoder can actually open (a GPU encoder may be compiled
    in but unusable because no GPU/driver is present)."""
    with _encoder_lock:
        if name in _working_encoder_cache:
            return _working_encoder_cache[name]
    ok = False
    if name in available_encoders():
        args = [find_ffmpeg(), "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                "-i", "color=c=black:s=256x144:r=30:d=0.2", "-frames:v", "3",
                "-pix_fmt", "yuv420p" if "videotoolbox" not in name else "nv12",
                "-c:v", name, "-f", "null", "-"]
        try:
            ok = run(args, timeout=40, check=False).returncode == 0
        except Exception:
            ok = False
    with _encoder_lock:
        _working_encoder_cache[name] = ok
    return ok


def parse_progress_time(line: str) -> Optional[float]:
    """Parse ``out_time_us``/``out_time_ms``/``out_time`` lines of ``-progress``."""
    if line.startswith("out_time_us=") or line.startswith("out_time_ms="):
        # Both are microseconds (out_time_ms is a historical misnomer).
        value = line.split("=", 1)[1].strip()
        if value.lstrip("-").isdigit():
            return max(0.0, int(value) / 1_000_000.0)
    elif line.startswith("out_time="):
        value = line.split("=", 1)[1].strip()
        m = re.match(r"(-?\d+):(\d+):(\d+(?:\.\d+)?)", value)
        if m:
            h, mnt, s = m.groups()
            return max(0.0, int(h) * 3600 + int(mnt) * 60 + float(s))
    return None


def run_with_progress(args: Sequence[str], total_seconds: float,
                      on_progress: Optional[Callable[[float], None]] = None,
                      cancel: Optional[CancelToken] = None) -> str:
    """Run ffmpeg with ``-progress pipe:1`` and report a 0..1 fraction.

    ``args`` must not contain the executable; ``-progress`` is added here.
    Returns the collected stderr (useful for diagnostics).
    """
    cmd = [find_ffmpeg(), "-hide_banner", "-nostdin", "-progress", "pipe:1", "-nostats",
           *args]
    proc = popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                 stdin=subprocess.DEVNULL)
    stderr_lines: List[str] = []

    def _drain_stderr() -> None:
        assert proc.stderr is not None
        for raw in iter(proc.stderr.readline, b""):
            stderr_lines.append(raw.decode("utf-8", "replace"))
            if len(stderr_lines) > 400:
                del stderr_lines[:200]

    t = threading.Thread(target=_drain_stderr, daemon=True)
    t.start()
    cancelled = False
    try:
        assert proc.stdout is not None
        for raw in iter(proc.stdout.readline, b""):
            if cancel is not None and cancel.cancelled:
                cancelled = True
                break
            sec = parse_progress_time(raw.decode("utf-8", "replace").strip())
            if sec is not None and on_progress is not None and total_seconds > 0:
                on_progress(min(1.0, sec / total_seconds))
    finally:
        if cancelled or (cancel is not None and cancel.cancelled):
            terminate(proc)
        proc.wait()
        t.join(timeout=5)
    stderr = "".join(stderr_lines)
    if cancelled or (cancel is not None and cancel.cancelled):
        raise Cancelled("cancelled")
    if proc.returncode != 0:
        raise FFmpegError(f"ffmpeg failed (exit {proc.returncode})", stderr)
    return stderr


def terminate(proc: subprocess.Popen) -> None:
    """Stop a child process, politely first."""
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


class RawPipeReader:
    """Stream fixed size raw frames (video or audio blocks) from ffmpeg."""

    def __init__(self, args: Sequence[str], cancel: Optional[CancelToken] = None) -> None:
        self.cmd = [find_ffmpeg(), "-hide_banner", "-nostdin", "-loglevel", "error", *args]
        self.cancel = cancel
        self.proc: Optional[subprocess.Popen] = None
        self._stderr: List[bytes] = []
        self._thread: Optional[threading.Thread] = None
        self._stopped = False

    def __enter__(self) -> "RawPipeReader":
        self.proc = popen(self.cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          stdin=subprocess.DEVNULL, bufsize=1 << 20)

        def _drain() -> None:
            assert self.proc is not None and self.proc.stderr is not None
            for raw in iter(self.proc.stderr.readline, b""):
                if len(self._stderr) < 200:
                    self._stderr.append(raw)

        self._thread = threading.Thread(target=_drain, daemon=True)
        self._thread.start()
        return self

    def read_blocks(self, block_size: int) -> Iterable[bytes]:
        assert self.proc is not None and self.proc.stdout is not None
        stream = self.proc.stdout
        while True:
            if self.cancel is not None and self.cancel.cancelled:
                raise Cancelled("cancelled")
            data = stream.read(block_size)
            if not data:
                return
            while len(data) < block_size:
                more = stream.read(block_size - len(data))
                if not more:
                    break
                data += more
            yield data
            if len(data) < block_size:
                return

    @property
    def stderr(self) -> str:
        return b"".join(self._stderr).decode("utf-8", "replace")

    def stop(self) -> None:
        """Stop decoding early (the remaining output is not needed)."""
        self._stopped = True
        if self.proc is not None:
            terminate(self.proc)

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.proc is None:
            return
        if exc_type is not None or self._stopped:
            terminate(self.proc)
        else:
            try:
                if self.proc.stdout:
                    while self.proc.stdout.read(1 << 20):
                        pass
            except Exception:
                pass
        self.proc.wait()
        if self._thread is not None:
            self._thread.join(timeout=5)
        if exc_type is None and not self._stopped and self.proc.returncode not in (0, None):
            raise FFmpegError(f"ffmpeg failed (exit {self.proc.returncode})", self.stderr)


def hwaccel_args(enabled: bool) -> List[str]:
    """Input options enabling hardware decoding (CUDA/VAAPI/VideoToolbox...)."""
    if not enabled:
        return []
    return ["-hwaccel", "auto"]
