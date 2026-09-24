#!/usr/bin/env python3
"""Build a self-contained CutSensei application with PyInstaller.

    pip install -e .[dev] pillow
    python packaging/build.py
    python packaging/build.py --ffmpeg-dir /path/to/ffmpeg-build   # custom FFmpeg

The result is written to ``dist/`` as a zip (Windows, macOS) or tar.gz (Linux).
The FFmpeg binary of imageio-ffmpeg is bundled (Windows build: NVENC/QSV/AMF).
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BUILD = os.path.join(HERE, "build")
FFMPEG_DIR = os.path.join(HERE, "ffmpeg")

sys.path.insert(0, os.path.join(ROOT, "src"))
from cutsensei import __version__  # noqa: E402


def log(msg: str) -> None:
    print(f"[build] {msg}", flush=True)


def platform_tag() -> str:
    machine = platform.machine().lower()
    arch = {"amd64": "x64", "x86_64": "x64", "arm64": "arm64", "aarch64": "arm64"}.get(machine,
                                                                                       machine)
    if sys.platform.startswith("win"):
        return f"windows-{arch}"
    if sys.platform == "darwin":
        return f"macos-{arch}"
    return f"linux-{arch}"


def render_icons() -> None:
    os.makedirs(BUILD, exist_ok=True)
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QByteArray, QRectF, Qt
    from PySide6.QtGui import QGuiApplication, QImage, QPainter
    from PySide6.QtSvg import QSvgRenderer

    from cutsensei.ui.icons import APP_ICON_SVG

    app = QGuiApplication.instance() or QGuiApplication([])  # noqa: F841
    png = os.path.join(BUILD, "icon.png")
    img = QImage(1024, 1024, QImage.Format_ARGB32)
    img.fill(Qt.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)
    QSvgRenderer(QByteArray(APP_ICON_SVG.encode())).render(p, QRectF(0, 0, 1024, 1024))
    p.end()
    img.save(png)
    try:
        from PIL import Image

        im = Image.open(png)
        im.save(os.path.join(BUILD, "icon.ico"),
                sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
        if sys.platform == "darwin":
            im.save(os.path.join(BUILD, "icon.icns"))
    except Exception as exc:  # Pillow missing: PyInstaller uses its default icon
        log(f"icon conversion skipped ({exc})")


def prepare_ffmpeg(custom_dir: str = "") -> None:
    """Optionally bundle a custom FFmpeg directory (``--ffmpeg-dir``).

    By default the static FFmpeg of the ``imageio-ffmpeg`` package is bundled
    (collected by the spec file).  Its Windows build includes NVENC, Quick
    Sync and AMF; on Linux an FFmpeg installed on the system is preferred at
    runtime because distribution builds integrate with VAAPI/NVENC drivers.
    """
    shutil.rmtree(FFMPEG_DIR, ignore_errors=True)
    if not custom_dir:
        return
    shutil.copytree(custom_dir, FFMPEG_DIR)
    for root, _dirs, files in os.walk(FFMPEG_DIR):
        for f in files:
            log(f"ffmpeg: {os.path.relpath(os.path.join(root, f), FFMPEG_DIR)}")


def run_pyinstaller() -> str:
    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
           "--distpath", os.path.join(ROOT, "dist"), "--workpath", os.path.join(BUILD, "work"),
           os.path.join(HERE, "cutsensei.spec")]
    log(" ".join(cmd))
    subprocess.check_call(cmd, cwd=ROOT)
    if sys.platform == "darwin":
        return os.path.join(ROOT, "dist", "CutSensei.app")
    return os.path.join(ROOT, "dist", "CutSensei")


def archive(path: str) -> str:
    name = f"CutSensei-{__version__}-{platform_tag()}"
    dist = os.path.join(ROOT, "dist")
    if sys.platform == "darwin":
        out = os.path.join(dist, name + ".zip")
        subprocess.check_call(["ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", path, out])
        return out
    if sys.platform.startswith("win"):
        return shutil.make_archive(os.path.join(dist, name), "zip", os.path.dirname(path),
                                   os.path.basename(path))
    return shutil.make_archive(os.path.join(dist, name), "gztar", os.path.dirname(path),
                               os.path.basename(path))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ffmpeg-dir", default="",
                    help="bundle this FFmpeg directory (bin/ffmpeg[.exe], ...) instead of the "
                         "imageio-ffmpeg binary")
    ap.add_argument("--no-archive", action="store_true")
    args = ap.parse_args()
    render_icons()
    prepare_ffmpeg(args.ffmpeg_dir)
    out = run_pyinstaller()
    log(f"built {out}")
    if not args.no_archive:
        log(f"archive {archive(out)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
