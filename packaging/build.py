#!/usr/bin/env python3
"""Build a self-contained CutSensei application with PyInstaller.

    pip install -e .[dev] pillow
    python packaging/build.py            # downloads a GPU capable FFmpeg build
    python packaging/build.py --no-ffmpeg   # use FFmpeg from PATH at runtime

The result is written to ``dist/`` as a zip (Windows, macOS) or tar.gz (Linux).
FFmpeg builds: BtbN/FFmpeg-Builds (Windows, Linux; include NVENC/QSV/AMF) and the
imageio-ffmpeg binary on macOS (includes VideoToolbox).
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BUILD = os.path.join(HERE, "build")
FFMPEG_DIR = os.path.join(HERE, "ffmpeg")
BTBN = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/"

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


def download(url: str, dest: str) -> None:
    log(f"downloading {url}")
    with urllib.request.urlopen(url) as resp, open(dest, "wb") as fh:
        shutil.copyfileobj(resp, fh)


def fetch_ffmpeg() -> None:
    shutil.rmtree(FFMPEG_DIR, ignore_errors=True)
    os.makedirs(FFMPEG_DIR)
    tmp = tempfile.mkdtemp()
    try:
        if sys.platform.startswith("win"):
            archive = os.path.join(tmp, "ffmpeg.zip")
            download(BTBN + "ffmpeg-master-latest-win64-gpl.zip", archive)
            with zipfile.ZipFile(archive) as zf:
                for name in zf.namelist():
                    base = os.path.basename(name)
                    if base in ("ffmpeg.exe", "ffprobe.exe", "LICENSE.txt"):
                        with zf.open(name) as src, open(os.path.join(FFMPEG_DIR, base), "wb") as d:
                            shutil.copyfileobj(src, d)
        elif sys.platform.startswith("linux") and platform.machine() in ("x86_64", "AMD64"):
            archive = os.path.join(tmp, "ffmpeg.tar.xz")
            download(BTBN + "ffmpeg-master-latest-linux64-gpl.tar.xz", archive)
            with tarfile.open(archive) as tf:
                for member in tf.getmembers():
                    base = os.path.basename(member.name)
                    if base in ("ffmpeg", "ffprobe", "LICENSE.txt") and member.isfile():
                        src = tf.extractfile(member)
                        assert src is not None
                        with open(os.path.join(FFMPEG_DIR, base), "wb") as d:
                            shutil.copyfileobj(src, d)
                        os.chmod(os.path.join(FFMPEG_DIR, base), 0o755)
        else:
            import imageio_ffmpeg

            exe = imageio_ffmpeg.get_ffmpeg_exe()
            shutil.copy2(exe, os.path.join(FFMPEG_DIR, "ffmpeg"))
            os.chmod(os.path.join(FFMPEG_DIR, "ffmpeg"), 0o755)
            with open(os.path.join(FFMPEG_DIR, "FFMPEG_SOURCE.txt"), "w") as fh:
                fh.write("FFmpeg binary from the imageio-ffmpeg package "
                         "(https://github.com/imageio/imageio-ffmpeg).\n"
                         "FFmpeg is licensed under the LGPL/GPL, see https://ffmpeg.org/legal.html\n")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    log(f"ffmpeg files: {os.listdir(FFMPEG_DIR)}")


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
    ap.add_argument("--no-ffmpeg", action="store_true", help="do not bundle FFmpeg")
    ap.add_argument("--no-archive", action="store_true")
    args = ap.parse_args()
    render_icons()
    if args.no_ffmpeg:
        shutil.rmtree(FFMPEG_DIR, ignore_errors=True)
    else:
        fetch_ffmpeg()
    out = run_pyinstaller()
    log(f"built {out}")
    if not args.no_archive:
        log(f"archive {archive(out)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
