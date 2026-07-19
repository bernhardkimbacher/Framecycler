"""App-level shortcut registry for built-ins and package keybinds."""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import QWidget

from ..packages.api import KeybindSpec

logger = logging.getLogger(__name__)


def _normalize_sequence(sequence: str) -> str:
    return QKeySequence(sequence).toString(QKeySequence.SequenceFormat.PortableText)


class KeybindRegistry:
    def __init__(self, host: QWidget) -> None:
        self._host = host
        self._reserved: set[str] = set()
        self._package_actions: dict[str, QAction] = {}  # keybind_id -> action

    def reserve(self, sequence: str) -> None:
        norm = _normalize_sequence(sequence)
        if norm:
            self._reserved.add(norm)

    def reserve_many(self, sequences: list[str]) -> None:
        for sequence in sequences:
            self.reserve(sequence)

    def is_reserved(self, sequence: str) -> bool:
        return _normalize_sequence(sequence) in self._reserved

    def register_package_keybind(self, spec: KeybindSpec) -> bool:
        norm = _normalize_sequence(spec.sequence)
        if not norm:
            logger.warning("Package keybind %s has empty sequence", spec.keybind_id)
            return False
        if norm in self._reserved or any(
            _normalize_sequence(a.shortcut().toString()) == norm
            for a in self._package_actions.values()
        ):
            logger.warning(
                "Package keybind %s sequence %s conflicts; skipping",
                spec.keybind_id,
                spec.sequence,
            )
            return False

        action = QAction(self._host)
        action.setShortcut(QKeySequence(spec.sequence))
        ctx = (
            Qt.ShortcutContext.ApplicationShortcut
            if spec.context == "app"
            else Qt.ShortcutContext.WindowShortcut
        )
        action.setShortcutContext(ctx)
        action.triggered.connect(spec.callback)
        self._host.addAction(action)
        self._package_actions[spec.keybind_id] = action
        self._reserved.add(norm)
        return True

    def unregister_package(self, package_id: str) -> None:
        prefix = f"{package_id}."
        to_remove = [kid for kid in self._package_actions if kid.startswith(prefix)]
        for kid in to_remove:
            action = self._package_actions.pop(kid)
            seq = _normalize_sequence(action.shortcut().toString())
            self._host.removeAction(action)
            action.deleteLater()
            if seq:
                self._reserved.discard(seq)

    def clear_package_keybinds(self) -> None:
        for kid in list(self._package_actions):
            action = self._package_actions.pop(kid)
            seq = _normalize_sequence(action.shortcut().toString())
            self._host.removeAction(action)
            action.deleteLater()
            if seq:
                self._reserved.discard(seq)
