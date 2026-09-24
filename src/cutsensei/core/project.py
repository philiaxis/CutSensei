"""Project model and ``.cutsensei`` file format (JSON).

A project stores a reference to the source video (absolute and relative
path plus a fingerprint), the analysis features, the segment list including
manual edits, board regions and all settings.  The source is never written.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from .. import __version__
from ..analysis.result import AnalysisResult
from .errors import CutSenseiError
from .media import MediaInfo
from .settings import AutoEditSettings, ExportSettings
from .timeline import Timeline

PROJECT_EXTENSION = ".cutsensei"
FORMAT_VERSION = 1


class Project:
    def __init__(self, media: MediaInfo) -> None:
        self.media = media
        self.name = Path(media.path).stem
        self.path: Optional[str] = None
        self.timeline = Timeline(media.duration)
        self.settings = AutoEditSettings()
        self.export = ExportSettings()
        self.board_regions: List[List[float]] = []   # normalized [x, y, w, h]
        self.analysis: Optional[AnalysisResult] = None
        self.auto_applied = False
        self.dirty = False

    # ------------------------------------------------------------ edit state
    def edit_state(self) -> Dict[str, Any]:
        """The part of the project covered by undo/redo."""
        return {
            "segments": self.timeline.to_list(),
            "settings": self.settings.to_dict(),
            "board_regions": [list(r) for r in self.board_regions],
            "auto_applied": self.auto_applied,
        }

    def restore_edit_state(self, state: Dict[str, Any]) -> None:
        self.timeline = Timeline.from_list(self.media.duration, state["segments"])
        self.settings = AutoEditSettings.from_dict(state["settings"])
        self.board_regions = [list(r) for r in state.get("board_regions", [])]
        self.auto_applied = bool(state.get("auto_applied", self.auto_applied))

    # ------------------------------------------------------------ persistence
    def to_dict(self, project_path: Optional[str] = None) -> Dict[str, Any]:
        media = self.media.to_dict()
        if project_path:
            try:
                media["relpath"] = os.path.relpath(self.media.path,
                                                   os.path.dirname(os.path.abspath(project_path)))
            except ValueError:  # different drive on Windows
                media["relpath"] = None
        return {
            "app": "CutSensei",
            "format_version": FORMAT_VERSION,
            "app_version": __version__,
            "name": self.name,
            "media": media,
            "board_regions": self.board_regions,
            "settings": self.settings.to_dict(),
            "export": self.export.to_dict(),
            "auto_applied": self.auto_applied,
            "segments": self.timeline.to_list(),
            "analysis": self.analysis.to_b64() if self.analysis is not None else None,
        }

    def save(self, path: str) -> None:
        path = str(path)
        if not path.endswith(PROJECT_EXTENSION):
            path += PROJECT_EXTENSION
        data = self.to_dict(path)
        directory = os.path.dirname(os.path.abspath(path))
        os.makedirs(directory, exist_ok=True)
        # write atomically so a crash never leaves a truncated project
        fd, tmp = tempfile.mkstemp(prefix=".cutsensei-", suffix=".tmp", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=1)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        self.path = path
        self.dirty = False

    @classmethod
    def load(cls, path: str, media_override: Optional[str] = None) -> "Project":
        """Load a project.  ``media_override`` replaces a moved source file."""
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if data.get("app") != "CutSensei":
            raise CutSenseiError("Not a CutSensei project file.")
        if int(data.get("format_version", 0)) > FORMAT_VERSION:
            raise CutSenseiError("The project was created by a newer version of CutSensei.")
        media = MediaInfo.from_dict(data["media"])
        source = media_override or resolve_media_path(data["media"], path)
        if source:
            media.path = source
        proj = cls(media)
        proj.name = data.get("name") or proj.name
        proj.path = path
        proj.board_regions = [list(map(float, r)) for r in data.get("board_regions", [])]
        proj.settings = AutoEditSettings.from_dict(data.get("settings"))
        proj.export = ExportSettings.from_dict(data.get("export"))
        proj.auto_applied = bool(data.get("auto_applied", False))
        proj.timeline = Timeline.from_list(media.duration, data.get("segments") or [])
        if data.get("analysis"):
            try:
                proj.analysis = AnalysisResult.from_b64(data["analysis"])
            except Exception:
                proj.analysis = None  # stale/corrupt cache; can be re-analysed
        proj.dirty = False
        return proj

    @property
    def media_exists(self) -> bool:
        return os.path.isfile(self.media.path)


def resolve_media_path(media: Dict[str, Any], project_path: str) -> Optional[str]:
    """Find the source video: absolute path, then relative to the project,
    then a file with the same name next to the project."""
    abs_path = media.get("path")
    if abs_path and os.path.isfile(abs_path):
        return abs_path
    base = os.path.dirname(os.path.abspath(project_path))
    rel = media.get("relpath")
    if rel:
        cand = os.path.normpath(os.path.join(base, rel))
        if os.path.isfile(cand):
            return cand
    if abs_path:
        cand = os.path.join(base, os.path.basename(abs_path.replace("\\", "/")))
        if os.path.isfile(cand):
            return cand
    return abs_path
