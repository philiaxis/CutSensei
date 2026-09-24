# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for CutSensei.  Build with:  python packaging/build.py
import os
import sys

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

HERE = os.path.abspath(SPECPATH)
ROOT = os.path.dirname(HERE)
SRC = os.path.join(ROOT, "src")
sys.path.insert(0, SRC)

from cutsensei import __version__  # noqa: E402

ffmpeg_dir = os.path.join(HERE, "ffmpeg")
binaries = []
datas = [
    (os.path.join(SRC, "cutsensei", "models"), os.path.join("cutsensei", "models")),
    (os.path.join(SRC, "cutsensei", "resources"), os.path.join("cutsensei", "resources")),
    (os.path.join(ROOT, "LICENSE"), "."),
    (os.path.join(ROOT, "THIRD_PARTY_NOTICES.md"), "."),
]
# FFmpeg is copied verbatim (bin/ + lib/ layout, executable bits preserved)
if os.path.isdir(ffmpeg_dir):
    datas.append((ffmpeg_dir, "ffmpeg"))
datas += collect_data_files("onnxruntime")
if not os.path.isdir(ffmpeg_dir):
    datas += collect_data_files("imageio_ffmpeg", subdir="binaries")

hiddenimports = collect_submodules("cutsensei") + ["PySide6.QtMultimedia", "PySide6.QtSvg"]
excludes = [
    "tkinter", "matplotlib", "IPython", "pytest",
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineQuick",
    "PySide6.QtWebChannel", "PySide6.QtWebSockets", "PySide6.Qt3DCore", "PySide6.Qt3DRender",
    "PySide6.Qt3DExtras", "PySide6.Qt3DInput", "PySide6.Qt3DLogic", "PySide6.Qt3DAnimation",
    "PySide6.QtCharts", "PySide6.QtDataVisualization", "PySide6.QtGraphs", "PySide6.QtPdf",
    "PySide6.QtPdfWidgets", "PySide6.QtQuick3D", "PySide6.QtBluetooth", "PySide6.QtSensors",
    "PySide6.QtSerialPort", "PySide6.QtSerialBus", "PySide6.QtNfc", "PySide6.QtPositioning",
    "PySide6.QtLocation", "PySide6.QtRemoteObjects", "PySide6.QtScxml", "PySide6.QtSql",
    "PySide6.QtTextToSpeech", "PySide6.QtHelp", "PySide6.QtDesigner", "PySide6.QtUiTools",
    "PySide6.QtQuick", "PySide6.QtQml", "PySide6.QtQuickWidgets", "PySide6.QtQuickControls2",
    "PySide6.QtSpatialAudio", "PySide6.QtHttpServer", "PySide6.QtStateMachine",
]
if os.path.isdir(ffmpeg_dir):
    excludes.append("imageio_ffmpeg")   # a full FFmpeg is bundled instead

icon = os.path.join(HERE, "build", "icon.ico" if sys.platform.startswith("win") else
                    "icon.icns" if sys.platform == "darwin" else "icon.png")
icon = icon if os.path.isfile(icon) else None

a = Analysis(
    [os.path.join(HERE, "launcher.py")],
    pathex=[SRC],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=excludes,
    noarchive=False,
)
# Qt parts CutSensei does not use (PDF, virtual keyboard, EGLFS).  Note: the
# FFmpeg multimedia plugin links against Qt Quick/QML, so those must stay.
_DROP = ("Qt6Pdf", "Qt6VirtualKeyboard", "Qt6EglFs", "Qt6EglFSDeviceIntegration",
         "platforminputcontexts", "egldeviceintegrations", "imageformats/libqpdf",
         "imageformats\\qpdf", "Qt6ShaderTools", "Qt6LabsFolderListModel")
a.binaries = [b for b in a.binaries if not any(d in b[0].replace("\\", "/") or d in b[1]
                                               for d in _DROP)]
a.datas = [d for d in a.datas if not any(x in d[0] for x in ("Qt6Pdf", "qtwebengine",
                                                              "translations/qtwebengine"))]

pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="CutSensei",
    console=False,
    icon=icon,
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="CutSensei", upx=False)
if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="CutSensei.app",
        icon=icon,
        bundle_identifier="io.github.philiaxis.cutsensei",
        version=__version__,
        info_plist={
            "NSHighResolutionCapable": True,
            "CFBundleShortVersionString": __version__,
            "LSMinimumSystemVersion": "11.0",
            "CFBundleDocumentTypes": [{
                "CFBundleTypeName": "CutSensei Project",
                "CFBundleTypeExtensions": ["cutsensei"],
                "CFBundleTypeRole": "Editor",
            }],
        },
    )
