"""Dark colour scheme, palette and style sheet."""

from __future__ import annotations

from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import QApplication, QStyleFactory

from ..core.paths import resource_path

BG0 = "#141518"      # timeline / video background
BG1 = "#1b1c20"      # window
BG2 = "#222429"      # panels
BG3 = "#2b2e34"      # inputs / hover
BG4 = "#353940"      # pressed / selected
BORDER = "#34373e"
TEXT = "#d8dadf"
TEXT_DIM = "#8d929b"
TEXT_FAINT = "#5f646d"
ACCENT = "#4f8cff"
ACCENT_DIM = "#2f5fb8"

# segment colours (always accompanied by a text label and the speed)
KEEP = "#3a9477"      # 等速
SPEED = "#d39a35"     # 倍速
CUT = "#b8474f"       # 削除
REVIEW = "#a07ae6"    # 確認対象
PLAYHEAD = "#ff5a5f"
WAVE = "#8fa3bf"

KIND_COLORS = {
    "speech": "#5aa7e0",
    "writing": SPEED,
    "idle": "#8a8f98",
    "uncertain": REVIEW,
}


def color(name: str, alpha: int = 255) -> QColor:
    c = QColor(name)
    c.setAlpha(alpha)
    return c


def apply_theme(app: QApplication) -> None:
    app.setStyle(QStyleFactory.create("Fusion"))
    pal = QPalette()
    pal.setColor(QPalette.Window, QColor(BG1))
    pal.setColor(QPalette.WindowText, QColor(TEXT))
    pal.setColor(QPalette.Base, QColor(BG2))
    pal.setColor(QPalette.AlternateBase, QColor(BG3))
    pal.setColor(QPalette.ToolTipBase, QColor(BG3))
    pal.setColor(QPalette.ToolTipText, QColor(TEXT))
    pal.setColor(QPalette.PlaceholderText, QColor(TEXT_FAINT))
    pal.setColor(QPalette.Text, QColor(TEXT))
    pal.setColor(QPalette.Button, QColor(BG3))
    pal.setColor(QPalette.ButtonText, QColor(TEXT))
    pal.setColor(QPalette.BrightText, QColor("#ffffff"))
    pal.setColor(QPalette.Highlight, QColor(ACCENT_DIM))
    pal.setColor(QPalette.HighlightedText, QColor("#ffffff"))
    pal.setColor(QPalette.Link, QColor(ACCENT))
    for group in (QPalette.Disabled,):
        pal.setColor(group, QPalette.WindowText, QColor(TEXT_FAINT))
        pal.setColor(group, QPalette.Text, QColor(TEXT_FAINT))
        pal.setColor(group, QPalette.ButtonText, QColor(TEXT_FAINT))
    app.setPalette(pal)
    font = app.font()
    if font.pointSizeF() < 9.5:
        font.setPointSizeF(9.5)
    app.setFont(font)
    app.setStyleSheet(STYLE_SHEET)


CHECK_SVG = str(resource_path("resources", "check.svg")).replace("\\", "/")

STYLE_SHEET = f"""
QMainWindow, QDialog {{ background: {BG1}; }}
QWidget {{ color: {TEXT}; }}
QToolTip {{ background: {BG3}; color: {TEXT}; border: 1px solid {BORDER}; padding: 4px 6px; }}
QToolBar {{ background: {BG1}; border: none; border-bottom: 1px solid {BORDER};
            spacing: 2px; padding: 3px 6px; }}
QToolBar::separator {{ background: {BORDER}; width: 1px; margin: 5px 6px; }}
QToolButton {{ background: transparent; border: 1px solid transparent; border-radius: 4px;
               padding: 4px 6px; color: {TEXT}; }}
QToolButton:hover {{ background: {BG3}; border-color: {BORDER}; }}
QToolButton:pressed, QToolButton:checked {{ background: {BG4}; }}
QToolButton:disabled {{ color: {TEXT_FAINT}; }}
QToolButton#primary {{ background: {ACCENT_DIM}; border-color: {ACCENT_DIM}; color: white; }}
QToolButton#primary:hover {{ background: {ACCENT}; }}
QPushButton {{ background: {BG3}; border: 1px solid {BORDER}; border-radius: 4px;
               padding: 5px 12px; }}
QPushButton:hover {{ background: {BG4}; }}
QPushButton:pressed {{ background: {BG2}; }}
QPushButton:checked {{ background: {ACCENT_DIM}; border-color: {ACCENT}; color: white; }}
QPushButton:disabled {{ color: {TEXT_FAINT}; background: {BG2}; }}
QPushButton#primary {{ background: {ACCENT_DIM}; border-color: {ACCENT}; color: white;
                       font-weight: 600; }}
QPushButton#primary:hover {{ background: {ACCENT}; }}
QPushButton#segment {{ border-radius: 0px; padding: 5px 10px; }}
QPushButton#segment:checked {{ background: {BG4}; border-color: {ACCENT}; color: white; }}
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QPlainTextEdit {{
    background: {BG2}; border: 1px solid {BORDER}; border-radius: 4px; padding: 3px 5px;
    selection-background-color: {ACCENT_DIM}; }}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{ border-color: {ACCENT}; }}
QComboBox QAbstractItemView {{ background: {BG2}; border: 1px solid {BORDER};
                               selection-background-color: {ACCENT_DIM}; }}
QTabWidget::pane {{ border: none; border-top: 1px solid {BORDER}; }}
QTabBar::tab {{ background: transparent; color: {TEXT_DIM}; padding: 6px 9px;
                border: none; border-bottom: 2px solid transparent; }}
QTabBar::tab:selected {{ color: {TEXT}; border-bottom: 2px solid {ACCENT}; }}
QTabBar::tab:hover {{ color: {TEXT}; }}
QSplitter::handle {{ background: {BORDER}; }}
QSplitter::handle:horizontal {{ width: 1px; }}
QSplitter::handle:vertical {{ height: 1px; }}
QSplitter::handle:hover {{ background: {ACCENT}; }}
QScrollBar:horizontal {{ background: {BG1}; height: 12px; margin: 0; }}
QScrollBar:vertical {{ background: {BG1}; width: 12px; margin: 0; }}
QScrollBar::handle {{ background: {BG4}; border-radius: 4px; min-width: 24px; min-height: 24px;
                      margin: 2px; }}
QScrollBar::handle:hover {{ background: #4a4f58; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: none; }}
QSlider::groove:horizontal {{ height: 4px; background: {BG4}; border-radius: 2px; }}
QSlider::sub-page:horizontal {{ background: {ACCENT_DIM}; border-radius: 2px; }}
QSlider::handle:horizontal {{ background: {TEXT}; width: 12px; height: 12px; margin: -5px 0;
                              border-radius: 6px; }}
QSlider::handle:horizontal:hover {{ background: white; }}
QTreeWidget, QListWidget, QTableWidget {{ background: {BG2}; border: none;
    alternate-background-color: {BG3}; outline: 0; }}
QTreeWidget::item, QListWidget::item {{ padding: 3px 2px; }}
QTreeWidget::item:selected, QListWidget::item:selected {{ background: {ACCENT_DIM}; color: white; }}
QHeaderView::section {{ background: {BG1}; color: {TEXT_DIM}; border: none;
                        border-bottom: 1px solid {BORDER}; padding: 4px 6px; }}
QGroupBox {{ border: none; border-top: 1px solid {BORDER}; margin-top: 14px; padding-top: 8px; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 0; padding: 0 4px 0 0; color: {TEXT_DIM}; }}
QCheckBox {{ spacing: 7px; }}
QCheckBox::indicator {{ width: 14px; height: 14px; border: 1px solid #5c616b; border-radius: 3px;
                       background: {BG2}; }}
QCheckBox::indicator:hover {{ border-color: {ACCENT}; }}
QCheckBox::indicator:checked {{ background: {ACCENT_DIM}; border-color: {ACCENT};
                               image: url("{CHECK_SVG}"); }}
QCheckBox::indicator:disabled {{ border-color: {BORDER}; background: {BG1}; }}
QStatusBar {{ background: {BG1}; border-top: 1px solid {BORDER}; color: {TEXT_DIM}; }}
QStatusBar QLabel {{ color: {TEXT_DIM}; padding: 0 6px; }}
QMenuBar {{ background: {BG1}; border-bottom: 1px solid {BORDER}; }}
QMenuBar::item {{ padding: 4px 10px; background: transparent; }}
QMenuBar::item:selected {{ background: {BG3}; }}
QMenu {{ background: {BG2}; border: 1px solid {BORDER}; padding: 4px; }}
QMenu::item {{ padding: 5px 24px 5px 22px; border-radius: 3px; }}
QMenu::item:selected {{ background: {ACCENT_DIM}; color: white; }}
QMenu::item:disabled {{ color: {TEXT_FAINT}; }}
QMenu::separator {{ height: 1px; background: {BORDER}; margin: 4px 6px; }}
QProgressBar {{ background: {BG2}; border: 1px solid {BORDER}; border-radius: 4px; height: 14px;
                text-align: center; }}
QProgressBar::chunk {{ background: {ACCENT_DIM}; border-radius: 3px; }}
QLabel#heading {{ color: {TEXT}; font-weight: 600; }}
QLabel#dim {{ color: {TEXT_DIM}; }}
QLabel#faint {{ color: {TEXT_FAINT}; }}
QLabel#statValue {{ color: {TEXT}; font-weight: 600; }}
QLabel#chip {{ border-radius: 3px; padding: 1px 6px; color: #101114; font-weight: 600; }}
QFrame#panel {{ background: {BG2}; }}
QFrame#hline {{ background: {BORDER}; max-height: 1px; min-height: 1px; }}
QWidget#transport {{ background: {BG1}; border-top: 1px solid {BORDER}; }}
QWidget#timelineBar {{ background: {BG1}; border-bottom: 1px solid {BORDER}; }}
QWidget#hint {{ background: {BG3}; border: 1px solid {BORDER}; border-radius: 4px; }}
"""


def mono_font(size: float = 9.5) -> QFont:
    f = QFont("Menlo")
    f.setStyleHint(QFont.Monospace)
    f.setFamilies(["JetBrains Mono", "Cascadia Mono", "Consolas", "Menlo", "DejaVu Sans Mono",
                   "Noto Sans Mono", "monospace"])
    f.setPointSizeF(size)
    return f
