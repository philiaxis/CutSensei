"""Small built-in line icon set (drawn for CutSensei, 24x24 SVG)."""

from __future__ import annotations

from functools import lru_cache

from PySide6.QtCore import QByteArray, QRectF, Qt
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

from . import theme

_S = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" '
      'stroke="{c}" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">{b}</svg>')

_ICONS = {
    "import": '<rect x="3" y="5" width="14" height="14" rx="2"/><path d="M17 10l4-2.5v9L17 14"/>'
              '<path d="M10 9v6M7 12h6"/>',
    "open": '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
    "save": '<path d="M5 3h11l5 5v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2z"/>'
            '<path d="M7 3v5h8V3M7 21v-7h10v7"/>',
    "undo": '<path d="M9 14L4 9l5-5"/><path d="M4 9h11a5 5 0 0 1 0 10h-3"/>',
    "redo": '<path d="M15 14l5-5-5-5"/><path d="M20 9H9a5 5 0 0 0 0 10h3"/>',
    "auto": '<path d="M4 20L15 9"/><path d="M13 7l2 2"/><path d="M18 3v4M16 5h4"/>'
            '<path d="M20 11v3M18.5 12.5h3"/><path d="M8 3v3M6.5 4.5h3"/>',
    "export": '<path d="M12 15V3"/><path d="M7 8l5-5 5 5"/>'
              '<path d="M4 14v4a3 3 0 0 0 3 3h10a3 3 0 0 0 3-3v-4"/>',
    "board": '<rect x="3" y="4" width="18" height="12" rx="1" stroke-dasharray="3 2"/>'
             '<path d="M8 20l4-4 4 4"/><path d="M6.5 9h4M6.5 12h7"/>',
    "play": '<path d="M7 4.5v15l12-7.5z" fill="{c}"/>',
    "pause": '<rect x="6" y="4.5" width="4" height="15" rx="1" fill="{c}"/>'
             '<rect x="14" y="4.5" width="4" height="15" rx="1" fill="{c}"/>',
    "frame_back": '<path d="M17 6v12l-8-6z" fill="{c}"/><path d="M7 6v12"/>',
    "frame_fwd": '<path d="M7 6v12l8-6z" fill="{c}"/><path d="M17 6v12"/>',
    "prev_mark": '<path d="M5 5v14"/><path d="M19 6l-9 6 9 6z"/>',
    "next_mark": '<path d="M19 5v14"/><path d="M5 6l9 6-9 6z"/>',
    "prev_review": '<path d="M15 6l-6 6 6 6"/><circle cx="19" cy="6" r="2" fill="{c}"/>',
    "next_review": '<path d="M9 6l6 6-6 6"/><circle cx="5" cy="6" r="2" fill="{c}"/>',
    "split": '<path d="M12 3v18"/><path d="M5 8l-2 4 2 4"/><path d="M19 8l2 4-2 4"/>'
             '<path d="M8 12h1M15 12h1"/>',
    "delete": '<path d="M4 7h16"/><path d="M9 7V4h6v3"/><path d="M6 7l1 13h10l1-13"/>'
              '<path d="M10 11v6M14 11v6"/>',
    "restore": '<path d="M3 12a9 9 0 1 0 3-6.7"/><path d="M3 4v5h5"/>',
    "protect": '<path d="M12 3l8 3v6c0 4.5-3.4 8-8 9-4.6-1-8-4.5-8-9V6z"/><path d="M8.5 12l2.5 2.5 4.5-5"/>',
    "unlock": '<rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 7.5-2"/>',
    "zoom_in": '<circle cx="11" cy="11" r="7"/><path d="M20 20l-4-4M8 11h6M11 8v6"/>',
    "zoom_out": '<circle cx="11" cy="11" r="7"/><path d="M20 20l-4-4M8 11h6"/>',
    "fit": '<path d="M4 9V4h5M15 4h5v5M20 15v5h-5M9 20H4v-5"/>',
    "panel_left": '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M9 4v16"/>',
    "panel_right": '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M15 4v16"/>',
    "panel_bottom": '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M3 14h18"/>',
    "settings": '<path d="M4 6h10M18 6h2M4 12h4M12 12h8M4 18h12M20 18h0"/>'
                '<circle cx="16" cy="6" r="2"/><circle cx="10" cy="12" r="2"/>'
                '<circle cx="18" cy="18" r="2"/>',
    "volume": '<path d="M4 9v6h4l5 4V5L8 9z"/><path d="M16.5 8.5a5 5 0 0 1 0 7"/>',
    "mute": '<path d="M4 9v6h4l5 4V5L8 9z"/><path d="M17 9l4 6M21 9l-4 6"/>',
    "flag": '<path d="M5 21V4"/><path d="M5 4h11l-2 4 2 4H5"/>',
    "check": '<path d="M5 12.5l4.5 4.5L19 7"/>',
    "keep": '<path d="M4 12h16"/><path d="M14 6l6 6-6 6"/>',
    "speed": '<path d="M3 6l7 6-7 6z"/><path d="M12 6l7 6-7 6z"/>',
    "cut": '<circle cx="6" cy="7" r="2.5"/><circle cx="6" cy="17" r="2.5"/>'
           '<path d="M8 8.5L20 17M8 15.5L20 7"/>',
    "loop": '<path d="M4 12a5 5 0 0 1 5-5h9l-3-3M20 12a5 5 0 0 1-5 5H6l3 3"/>',
    "info": '<circle cx="12" cy="12" r="9"/><path d="M12 11v6M12 7.5v.5"/>',
    "film": '<rect x="3" y="4" width="18" height="16" rx="2"/>'
            '<path d="M7 4v16M17 4v16M3 9h4M3 15h4M17 9h4M17 15h4"/>',
}


def _render(name: str, color: str, size: int) -> QPixmap:
    body = _ICONS[name].replace("{c}", color)
    svg = _S.format(c=color, b=body)
    renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    renderer.render(p, QRectF(0, 0, size, size))
    p.end()
    return pm


@lru_cache(maxsize=256)
def icon(name: str, color: str = theme.TEXT) -> QIcon:
    ic = QIcon()
    for size in (16, 20, 24, 32, 48):
        ic.addPixmap(_render(name, color, size), QIcon.Normal)
        ic.addPixmap(_render(name, theme.TEXT_FAINT, size), QIcon.Disabled)
        ic.addPixmap(_render(name, "#ffffff", size), QIcon.Active)
    return ic


def app_icon() -> QIcon:
    """Application icon: a play triangle with a scissor cut on an accent tile."""
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
           '<rect x="2" y="2" width="60" height="60" rx="14" fill="#2f5fb8"/>'
           '<path d="M22 16v32l26-16z" fill="#ffffff"/>'
           '<path d="M14 50L50 14" stroke="#d39a35" stroke-width="5" stroke-linecap="round"/>'
           '</svg>')
    ic = QIcon()
    for size in (16, 24, 32, 48, 64, 128, 256):
        renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
        pm = QPixmap(size, size)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        renderer.render(p, QRectF(0, 0, size, size))
        p.end()
        ic.addPixmap(pm)
    return ic


APP_ICON_SVG = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
                '<rect x="2" y="2" width="60" height="60" rx="14" fill="#2f5fb8"/>'
                '<path d="M22 16v32l26-16z" fill="#ffffff"/>'
                '<path d="M14 50L50 14" stroke="#d39a35" stroke-width="5" '
                'stroke-linecap="round"/></svg>')
