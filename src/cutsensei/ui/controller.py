"""The document controller: owns the project, selection and undo history.

All modifications of the project go through this object so that undo/redo,
the "modified" flag and the views stay consistent.
"""

from __future__ import annotations

from typing import Iterable, List, Optional, Sequence, Set

from PySide6.QtCore import QObject, Signal

from ..analysis.classifier import regenerate
from ..analysis.result import regions_signature
from ..core.history import History
from ..core.project import Project
from ..core.settings import AutoEditSettings
from ..core.timeline import Action, EditMap, Segment, Timeline
from .i18n import tr


class ProjectController(QObject):
    projectChanged = Signal()        # a different project was loaded / closed
    timelineChanged = Signal()       # segments changed
    settingsChanged = Signal()       # auto edit settings changed
    selectionChanged = Signal()
    analysisChanged = Signal()
    dirtyChanged = Signal(bool)
    historyChanged = Signal()

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self.project: Optional[Project] = None
        self.history = History()
        self._selected: List[str] = []
        self._map: Optional[EditMap] = None
        self.settings_pending = False   # settings changed since the last auto edit

    # ------------------------------------------------------------ project
    def set_project(self, project: Optional[Project]) -> None:
        self.project = project
        self.history.clear()
        self._selected = []
        self._map = None
        self.settings_pending = False
        self.projectChanged.emit()
        self.timelineChanged.emit()
        self.selectionChanged.emit()
        self.historyChanged.emit()
        self.dirtyChanged.emit(False)

    @property
    def has_project(self) -> bool:
        return self.project is not None

    @property
    def timeline(self) -> Optional[Timeline]:
        return self.project.timeline if self.project else None

    @property
    def settings(self) -> AutoEditSettings:
        return self.project.settings if self.project else AutoEditSettings()

    def edit_map(self) -> Optional[EditMap]:
        if self.project is None:
            return None
        if self._map is None:
            self._map = self.project.timeline.build_map(self.project.settings)
        return self._map

    def mark_dirty(self) -> None:
        if self.project and not self.project.dirty:
            self.project.dirty = True
            self.dirtyChanged.emit(True)

    def mark_saved(self) -> None:
        if self.project:
            self.project.dirty = False
            self.dirtyChanged.emit(False)

    # ------------------------------------------------------------ undo
    def _snapshot(self, label: str) -> None:
        assert self.project is not None
        self.history.push(label, self.project.edit_state())
        self.historyChanged.emit()

    def _changed(self, timeline: bool = True, settings: bool = False) -> None:
        self._map = None
        if self.project is not None:
            ids = {s.id for s in self.project.timeline}
            sel = [i for i in self._selected if i in ids]
            if sel != self._selected:
                self._selected = sel
                self.selectionChanged.emit()
        self.mark_dirty()
        if settings:
            self.settingsChanged.emit()
        if timeline:
            self.timelineChanged.emit()

    def undo(self) -> None:
        if not self.project:
            return
        state = self.history.undo(self.project.edit_state())
        if state is not None:
            self.project.restore_edit_state(state)
            self.historyChanged.emit()
            self._changed(timeline=True, settings=True)

    def redo(self) -> None:
        if not self.project:
            return
        state = self.history.redo(self.project.edit_state())
        if state is not None:
            self.project.restore_edit_state(state)
            self.historyChanged.emit()
            self._changed(timeline=True, settings=True)

    # ------------------------------------------------------------ selection
    def selected_ids(self) -> List[str]:
        return list(self._selected)

    def selected_indices(self) -> List[int]:
        if not self.project:
            return []
        index = {s.id: i for i, s in enumerate(self.project.timeline)}
        return sorted(index[i] for i in self._selected if i in index)

    def selected_segments(self) -> List[Segment]:
        tl = self.timeline
        return [tl[i] for i in self.selected_indices()] if tl else []

    def select(self, ids: Iterable[str]) -> None:
        ids = list(dict.fromkeys(ids))
        if ids != self._selected:
            self._selected = ids
            self.selectionChanged.emit()

    def select_index(self, index: int, extend: bool = False, toggle: bool = False) -> None:
        tl = self.timeline
        if tl is None or not (0 <= index < len(tl)):
            return
        sid = tl[index].id
        if toggle:
            ids = [i for i in self._selected if i != sid]
            if sid not in self._selected:
                ids.append(sid)
            self.select(ids)
        elif extend and self._selected:
            cur = self.selected_indices()
            anchor = cur[0] if index >= cur[0] else cur[-1]
            lo, hi = sorted((anchor, index))
            self.select([tl[i].id for i in range(lo, hi + 1)])
        else:
            self.select([sid])

    def clear_selection(self) -> None:
        self.select([])

    # ------------------------------------------------------------ edits
    def _target(self, indices: Optional[Sequence[int]]) -> List[int]:
        return list(indices) if indices is not None else self.selected_indices()

    def split_at(self, t: float) -> bool:
        if not self.project:
            return False
        tl = self.project.timeline
        probe = tl.clone()
        if probe.split_at(t) is None:
            return False
        self._snapshot(tr("Split"))
        res = tl.split_at(t)
        if res:
            self.select([tl[res[1]].id])
        self._changed()
        return True

    def set_action(self, action: str, indices: Optional[Sequence[int]] = None,
                   speed: Optional[float] = None) -> None:
        idx = self._target(indices)
        if not self.project or not idx:
            return
        label = {Action.KEEP: tr("Keep at 1x"), Action.SPEED: tr("Speed up"),
                 Action.CUT: tr("Delete")}[action]
        self._snapshot(label)
        self.project.timeline.set_action(idx, action, speed)
        self._changed()

    def set_speed(self, speed: float, indices: Optional[Sequence[int]] = None) -> None:
        idx = self._target(indices)
        if not self.project or not idx:
            return
        self._snapshot(tr("Change speed"))
        self.project.timeline.set_speed(idx, speed)
        self._changed()

    def set_volume(self, volume: Optional[float], indices: Optional[Sequence[int]] = None,
                   merge_key: Optional[str] = None) -> None:
        idx = self._target(indices)
        if not self.project or not idx:
            return
        if merge_key is None or self.history.undo_label != merge_key:
            self._snapshot(merge_key or tr("Change volume"))
        self.project.timeline.set_volume(idx, volume)
        self._changed()

    def set_protected(self, protected: bool, indices: Optional[Sequence[int]] = None) -> None:
        idx = self._target(indices)
        if not self.project or not idx:
            return
        self._snapshot(tr("Always keep") if protected else tr("Remove protection"))
        self.project.timeline.set_protected(idx, protected)
        self._changed()

    def restore(self, indices: Optional[Sequence[int]] = None) -> None:
        idx = self._target(indices)
        if not self.project or not idx:
            return
        idx = [i for i in idx if self.project.timeline[i].action == Action.CUT]
        if not idx:
            return
        self._snapshot(tr("Restore"))
        self.project.timeline.restore(idx)
        self._changed()

    def set_reviewed(self, reviewed: bool, indices: Optional[Sequence[int]] = None) -> None:
        idx = self._target(indices)
        if not self.project or not idx:
            return
        self._snapshot(tr("Mark as checked"))
        for i in idx:
            self.project.timeline[i].reviewed = reviewed
        self._changed()

    def unlock(self, indices: Optional[Sequence[int]] = None) -> None:
        """Release manual edits and let the automatic editor decide again."""
        idx = self._target(indices)
        if not self.project or not idx:
            return
        self._snapshot(tr("Revert to automatic"))
        self.project.timeline.unlock(idx)
        if self.project.analysis is not None and self.project.auto_applied:
            self.project.timeline = regenerate(self.project.timeline, self.project.analysis,
                                               self.project.settings)
        self._changed()

    def unlock_all(self) -> None:
        if not self.project:
            return
        self._snapshot(tr("Release all manual edits"))
        self.project.timeline.unlock(range(len(self.project.timeline)))
        if self.project.analysis is not None and self.project.auto_applied:
            self.project.timeline = regenerate(self.project.timeline, self.project.analysis,
                                               self.project.settings, keep_locked=False)
        self._changed()

    def set_boundary(self, index: int, t: float) -> bool:
        """Move boundary ``index`` (between segments index-1 and index) to ``t``."""
        if not self.project or not (0 < index < len(self.project.timeline)):
            return False
        self._snapshot(tr("Adjust boundary"))
        self.project.timeline.move_boundary(index, t)
        self._changed()
        return True

    # boundary dragging: begin once, update many times
    def begin_boundary_drag(self) -> None:
        if self.project:
            self._snapshot(tr("Adjust boundary"))

    def move_boundary(self, index: int, t: float) -> float:
        assert self.project is not None
        applied = self.project.timeline.move_boundary(index, t)
        self._changed()
        return applied

    def end_boundary_drag(self) -> None:
        if self.project:
            self.project.timeline.normalize()
            self._changed()

    # ------------------------------------------------------------ settings / auto
    def update_settings(self, **changes) -> None:
        if not self.project:
            return
        st = self.project.settings
        changed = {k: v for k, v in changes.items() if getattr(st, k) != v}
        if not changed:
            return
        key = tr("Change settings")
        if self.history.undo_label != key:
            self._snapshot(key)
        for k, v in changed.items():
            setattr(st, k, v)
        # speed / audio of sped-up parts take effect immediately
        self.settings_pending = self.project.auto_applied and any(
            k not in ("writing_speed", "speed_audio", "speed_audio_volume") for k in changed)
        self._changed(timeline=True, settings=True)

    def replace_settings(self, settings: AutoEditSettings) -> None:
        if not self.project:
            return
        self._snapshot(tr("Reset settings"))
        self.project.settings = settings
        self.settings_pending = self.project.auto_applied
        self._changed(timeline=True, settings=True)

    def set_board_regions(self, regions: List[List[float]]) -> None:
        if not self.project:
            return
        if regions_signature(regions) == regions_signature(self.project.board_regions):
            return
        self._snapshot(tr("Board region"))
        self.project.board_regions = [list(r) for r in regions]
        self._changed(timeline=False, settings=True)

    def analysis_is_current(self) -> bool:
        p = self.project
        if p is None or p.analysis is None:
            return False
        return regions_signature(p.analysis.board_regions) == regions_signature(p.board_regions)

    def set_analysis(self, analysis) -> None:
        if not self.project:
            return
        self.project.analysis = analysis
        self.mark_dirty()
        self.analysisChanged.emit()

    def apply_auto_edit(self) -> None:
        p = self.project
        if p is None or p.analysis is None:
            return
        self._snapshot(tr("Automatic edit"))
        p.timeline = regenerate(p.timeline, p.analysis, p.settings, keep_locked=True)
        p.auto_applied = True
        self.settings_pending = False
        self._changed(timeline=True, settings=True)

    def locked_count(self) -> int:
        return sum(1 for s in self.timeline or [] if s.locked)
