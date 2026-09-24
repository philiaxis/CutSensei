#!/usr/bin/env python3
"""Render documentation screenshots with the offscreen Qt platform.

    python tools/make_screenshots.py docs/images [--lang ja]

A demo lecture is generated and analysed first (takes ~20 s).
"""

import argparse
import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_LOGGING_RULES", "qt.multimedia.*=false")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("outdir")
    ap.add_argument("--lang", default="ja")
    args = ap.parse_args()
    os.makedirs(args.outdir, exist_ok=True)
    tmp = tempfile.mkdtemp()
    os.environ["CUTSENSEI_SETTINGS"] = os.path.join(tmp, "settings.ini")

    from cutsensei.analysis.classifier import regenerate
    from cutsensei.analysis.pipeline import analyze
    from cutsensei.core.media import probe
    from cutsensei.core.project import Project
    from cutsensei.demo import DemoSpec, Scene, board_region, generate_demo, screen_spec

    def build(name, spec, regions):
        video = os.path.join(tmp, name + ".mp4")
        generate_demo(video, spec)
        media = probe(video)
        proj = Project(media)
        proj.name = name
        proj.board_regions = regions
        proj.analysis = analyze(media, proj.board_regions)
        proj.timeline = regenerate(proj.timeline, proj.analysis, proj.settings)
        proj.auto_applied = True
        path = os.path.join(tmp, name + ".cutsensei")
        proj.save(path)
        return path

    spec = DemoSpec(scenes=[Scene("speech", 15), Scene("speech_writing", 15),
                            Scene("writing", 20), Scene("walk", 8), Scene("idle", 12),
                            Scene("speech", 10), Scene("writing", 10), Scene("idle", 6),
                            Scene("speech", 8)], width=960, height=540)
    proj_path = build("lecture", spec, [board_region(spec)])
    sspec = screen_spec([Scene("speech", 12), Scene("speech_laser", 8), Scene("writing", 15),
                         Scene("idle", 10), Scene("speech_writing", 10), Scene("scroll", 3),
                         Scene("writing", 12), Scene("page", 2), Scene("idle", 12),
                         Scene("laser", 6), Scene("speech", 10), Scene("writing", 10),
                         Scene("idle", 8)], webcam=True, width=1024, height=768)
    screen_path = build("notes", sspec, [])

    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication

    from cutsensei.ui import i18n, theme

    app = QApplication([])
    i18n.set_language(args.lang)
    theme.apply_theme(app)
    from cutsensei.ui.dialogs import BoardRegionDialog, ExportDialog
    from cutsensei.ui.main_window import MainWindow

    win = MainWindow()
    win.resize(1600, 940)
    win._restore_window()
    win.resize(1600, 940)
    win.show()
    win.open_project(proj_path)
    suffix = f"_{args.lang}"

    def shots() -> None:
        win.engine.seek(38.0)
        tl = win.ctrl.timeline
        win.ctrl.select_index(tl.index_at(38.0))
        QTimer.singleShot(1200, finish_main)

    def finish_main() -> None:
        win.grab().save(os.path.join(args.outdir, f"main{suffix}.png"))
        p = win.ctrl.project
        dlg = BoardRegionDialog(p.media, p.board_regions, 40.0, win)
        dlg.resize(900, 640)
        dlg.show()
        dlg._load_frame()
        app.processEvents()
        dlg.grab().save(os.path.join(args.outdir, f"board_region{suffix}.png"))
        dlg.close()
        m = win.ctrl.edit_map()
        ex = ExportDialog(p.media, p.export, m.out_duration, "/home/user/Videos/lecture_edited.mp4",
                          win)
        ex.show()
        app.processEvents()
        ex.grab().save(os.path.join(args.outdir, f"export{suffix}.png"))
        ex.close()
        win.ctrl.project.dirty = False
        win.open_project(screen_path)
        win.props.setCurrentIndex(1)
        # source time, while writing after the scroll (once the new video has loaded)
        QTimer.singleShot(1200, lambda: win.engine.seek(64.0))
        QTimer.singleShot(2800, finish_screen)

    def finish_screen() -> None:
        win.grab().save(os.path.join(args.outdir, f"main_screen{suffix}.png"))
        p = win.ctrl.project
        dlg = BoardRegionDialog(p.media, p.board_regions, 62.0, win,
                                excluded=p.analysis.live_rects)
        dlg.resize(900, 700)
        dlg.show()
        dlg._load_frame()
        app.processEvents()
        dlg.grab().save(os.path.join(args.outdir, f"board_region_screen{suffix}.png"))
        dlg.close()
        win.ctrl.project.dirty = False
        app.quit()

    QTimer.singleShot(2500, shots)
    app.exec()
    print("screenshots written to", args.outdir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
