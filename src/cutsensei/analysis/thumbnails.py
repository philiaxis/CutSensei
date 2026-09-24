"""Fast timeline thumbnails (key frames only)."""

from __future__ import annotations

import json
import os
import shutil
from typing import Optional

from ..core import ffmpeg
from ..core.jobs import CancelToken
from ..core.media import MediaInfo
from ..core.paths import thumbnails_dir

THUMB_HEIGHT = 108
_META = "thumbs.json"


def thumbnail_interval(duration: float) -> float:
    if duration <= 600:
        return 1.0
    if duration <= 3600:
        return 2.0
    return 3.0


def thumbnail_info(media: MediaInfo) -> Optional[dict]:
    """Return ``{"dir", "interval", "count"}`` when complete thumbnails exist."""
    d = thumbnails_dir(media.fingerprint())
    meta = os.path.join(d, _META)
    if not os.path.isfile(meta):
        return None
    try:
        with open(meta, "r", encoding="utf-8") as fh:
            info = json.load(fh)
    except (OSError, ValueError):
        return None
    info["dir"] = str(d)
    return info


def thumbnail_path(directory: str, index: int) -> str:
    return os.path.join(directory, f"t_{index:06d}.jpg")


def generate_thumbnails(media: MediaInfo, cancel: Optional[CancelToken] = None) -> dict:
    """Decode only key frames (very fast) and write small JPEG thumbnails."""
    d = str(thumbnails_dir(media.fingerprint()))
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d, exist_ok=True)
    interval = thumbnail_interval(media.duration)
    vf = f"fps=1/{interval}:start_time=0,scale=-2:{THUMB_HEIGHT}:flags=bilinear"

    def run(skip: bool) -> None:
        args = [ffmpeg.find_ffmpeg(), "-hide_banner", "-nostdin", "-loglevel", "error", "-y"]
        if skip:
            args += ["-skip_frame", "nokey"]
        args += ["-i", media.path, "-an", "-sn", "-dn", "-vf", vf, "-q:v", "6",
                 "-start_number", "0", "-f", "image2", os.path.join(d, "t_%06d.jpg")]
        proc = ffmpeg.popen(args, stdout=ffmpeg.subprocess.DEVNULL,
                            stderr=ffmpeg.subprocess.PIPE, stdin=ffmpeg.subprocess.DEVNULL)
        while True:
            try:
                proc.wait(timeout=0.2)
                break
            except ffmpeg.subprocess.TimeoutExpired:
                if cancel is not None and cancel.cancelled:
                    ffmpeg.terminate(proc)
                    cancel.check()
        if proc.returncode != 0:
            err = proc.stderr.read().decode("utf-8", "replace") if proc.stderr else ""
            raise ffmpeg.FFmpegError("thumbnail generation failed", err)

    try:
        run(True)
    except ffmpeg.FFmpegError:
        run(False)
    count = len([f for f in os.listdir(d) if f.endswith(".jpg")])
    if count == 0:
        run(False)
        count = len([f for f in os.listdir(d) if f.endswith(".jpg")])
    info = {"interval": interval, "count": count}
    with open(os.path.join(d, _META), "w", encoding="utf-8") as fh:
        json.dump(info, fh)
    info["dir"] = d
    return info
