"""Shots panel: timeline shots (stacks) with nested version clips."""

from __future__ import annotations

import os

from PySide6.QtCore import Qt, Signal, QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .fonts import ui_font

ROLE_SHOT = Qt.ItemDataRole.UserRole
ROLE_VERSION = Qt.ItemDataRole.UserRole + 1


class SourceListPanel(QWidget):
    """Tree of shots → versions. Kept class name for MainWindow imports."""

    shot_selected = Signal(int, int)  # shot_index, version_index
    shot_removed = Signal(int)
    version_removed = Signal(int, int)
    active_version_changed = Signal(int, int)
    order_changed = Signal(list)  # list of shot indices in new order
    hide_requested = Signal()

    # Back-compat aliases used during migration
    source_selected = shot_selected
    source_removed = shot_removed

    def __init__(self, main_window=None, parent=None):
        super().__init__(parent)
        self._main_window = main_window
        self._updating = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(6)

        header_row = QHBoxLayout()
        header_row.setContentsMargins(0, 0, 0, 0)
        title = QLabel("Shots")
        title.setFont(ui_font(11, weight=QFont.Weight.Bold))
        header_row.addWidget(title)
        header_row.addStretch()

        self.btn_close = QPushButton("×")
        self.btn_close.setFixedSize(22, 22)
        self.btn_close.setFlat(True)
        self.btn_close.setToolTip("Hide panel")
        self.btn_close.clicked.connect(self.hide_requested.emit)
        header_row.addWidget(self.btn_close)
        layout.addLayout(header_row)

        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.tree.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tree.setExpandsOnDoubleClick(False)
        self.tree.currentItemChanged.connect(self._on_current_changed)
        self.tree.itemDoubleClicked.connect(self._on_item_double_clicked)
        self.tree.model().rowsMoved.connect(self._on_rows_moved)
        layout.addWidget(self.tree, stretch=1)

        button_row = QHBoxLayout()
        self.btn_remove = QPushButton("Remove")
        self.btn_remove.clicked.connect(self._remove_current)
        button_row.addWidget(self.btn_remove)

        self.btn_set_active = QPushButton("Set Active")
        self.btn_set_active.clicked.connect(self._set_active_current)
        button_row.addWidget(self.btn_set_active)
        layout.addLayout(button_row)

        self.setMinimumWidth(180)
        self.setMaximumWidth(320)

    def set_plan(self, plan, *, selected_shot: int = 0, selected_version: int = 0) -> None:
        self._updating = True
        self.tree.clear()
        if plan is None or plan.empty:
            self._updating = False
            return

        select_item = None
        for segment in plan.segments:
            shot_item = QTreeWidgetItem()
            active = segment.active
            active_name = ""
            if active is not None:
                path = active.source.path if active.source else active.clip.name
                active_name = os.path.basename(path or "")
            version_count = len(segment.versions)
            label = segment.stack.name or f"Shot {segment.index + 1}"
            if version_count > 1:
                shot_item.setText(0, f"{label}  ({version_count} vers)  [{active_name}]")
            else:
                shot_item.setText(0, f"{label}  ({segment.frame_count} fr)")
            shot_item.setData(0, ROLE_SHOT, segment.index)
            shot_item.setData(0, ROLE_VERSION, -1)
            self.tree.addTopLevelItem(shot_item)

            for v_index, version in enumerate(segment.versions):
                child = QTreeWidgetItem()
                name = version.clip.name or (
                    os.path.basename(version.source.path) if version.source else "offline"
                )
                marks = []
                if version.is_active:
                    marks.append("A")
                if version.is_compare and not version.is_active:
                    marks.append("C")
                if version.offline:
                    marks.append("OFF")
                suffix = f" [{'/'.join(marks)}]" if marks else ""
                frames = version.source.frame_count if version.source else 0
                child.setText(0, f"{name} ({frames} fr){suffix}")
                child.setData(0, ROLE_SHOT, segment.index)
                child.setData(0, ROLE_VERSION, v_index)
                shot_item.addChild(child)
                if segment.index == selected_shot and v_index == selected_version:
                    select_item = child

            shot_item.setExpanded(version_count > 1)
            if select_item is None and segment.index == selected_shot:
                select_item = shot_item

        if select_item is not None:
            self.tree.setCurrentItem(select_item)
        self._updating = False

    # Back-compat for callers still using set_sources during transition
    def set_sources(self, sources: list) -> None:
        self._updating = True
        self.tree.clear()
        self._updating = False

    def set_active_index(self, index: int) -> None:
        if index < 0 or index >= self.tree.topLevelItemCount():
            return
        self._updating = True
        item = self.tree.topLevelItem(index)
        if item is not None:
            self.tree.setCurrentItem(item)
        self._updating = False

    def highlight_sequence_index(self, index: int) -> None:
        self.set_active_index(index)

    def _on_current_changed(self, current, previous) -> None:
        if self._updating or current is None:
            return
        shot = current.data(0, ROLE_SHOT)
        version = current.data(0, ROLE_VERSION)
        if shot is None:
            return
        version = 0 if version is None or version < 0 else version
        self.shot_selected.emit(int(shot), int(version))

    def _on_item_double_clicked(self, item, column) -> None:
        if item is None:
            return
        shot = item.data(0, ROLE_SHOT)
        version = item.data(0, ROLE_VERSION)
        if shot is None or version is None or version < 0:
            return
        self.active_version_changed.emit(int(shot), int(version))

    def _set_active_current(self) -> None:
        item = self.tree.currentItem()
        if item is None:
            return
        shot = item.data(0, ROLE_SHOT)
        version = item.data(0, ROLE_VERSION)
        if shot is None:
            return
        if version is None or version < 0:
            version = 0
        self.active_version_changed.emit(int(shot), int(version))

    def _on_rows_moved(self, *args) -> None:
        if self._updating:
            return
        QTimer.singleShot(0, self._emit_current_order)

    def _emit_current_order(self) -> None:
        if self._updating:
            return
        # Only top-level shot reordering is supported
        order = []
        for i in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(i)
            if item is not None:
                shot = item.data(0, ROLE_SHOT)
                if shot is not None:
                    order.append(int(shot))
        if order:
            self.order_changed.emit(order)

    def _remove_current(self) -> None:
        item = self.tree.currentItem()
        if item is None:
            return
        shot = item.data(0, ROLE_SHOT)
        version = item.data(0, ROLE_VERSION)
        if shot is None:
            return
        if version is not None and version >= 0 and item.parent() is not None:
            self.version_removed.emit(int(shot), int(version))
        else:
            self.shot_removed.emit(int(shot))
