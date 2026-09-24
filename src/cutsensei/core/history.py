"""Snapshot based undo/redo."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, List, Optional


@dataclass
class _Entry:
    label: str
    state: Any


class History:
    """Keeps copies of the editable state before each change.

    The state is an arbitrary JSON-like object (small: the segment list and
    settings), so plain snapshots are simpler and more robust than commands.
    """

    def __init__(self, limit: int = 300) -> None:
        self.limit = limit
        self._undo: List[_Entry] = []
        self._redo: List[_Entry] = []

    def clear(self) -> None:
        self._undo.clear()
        self._redo.clear()

    def push(self, label: str, state_before: Any) -> None:
        self._undo.append(_Entry(label, copy.deepcopy(state_before)))
        if len(self._undo) > self.limit:
            del self._undo[0]
        self._redo.clear()

    @property
    def can_undo(self) -> bool:
        return bool(self._undo)

    @property
    def can_redo(self) -> bool:
        return bool(self._redo)

    @property
    def undo_label(self) -> Optional[str]:
        return self._undo[-1].label if self._undo else None

    @property
    def redo_label(self) -> Optional[str]:
        return self._redo[-1].label if self._redo else None

    def undo(self, current_state: Any) -> Optional[Any]:
        if not self._undo:
            return None
        entry = self._undo.pop()
        self._redo.append(_Entry(entry.label, copy.deepcopy(current_state)))
        return entry.state

    def redo(self, current_state: Any) -> Optional[Any]:
        if not self._redo:
            return None
        entry = self._redo.pop()
        self._undo.append(_Entry(entry.label, copy.deepcopy(current_state)))
        return entry.state
