import os

from PySide6.QtCore import Qt, Signal, QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .fonts import ui_font


class SourceListPanel(QWidget):
    source_selected = Signal(int)
    source_removed = Signal(int)
    order_changed = Signal(list)
    hide_requested = Signal()

    def __init__(self, main_window=None, parent=None):
        super().__init__(parent)
        self._main_window = main_window
        self._updating = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(6)

        header_row = QHBoxLayout()
        header_row.setContentsMargins(0, 0, 0, 0)
        title = QLabel("Sources")
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

        self.list_widget = QListWidget()
        self.list_widget.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.list_widget.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.list_widget.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.list_widget.currentRowChanged.connect(self._on_row_changed)
        self.list_widget.model().rowsMoved.connect(self._on_rows_moved)
        layout.addWidget(self.list_widget, stretch=1)

        button_row = QHBoxLayout()
        self.btn_remove = QPushButton("Remove")
        self.btn_remove.clicked.connect(self._remove_current)
        button_row.addWidget(self.btn_remove)
        layout.addLayout(button_row)

        self.setMinimumWidth(180)
        self.setMaximumWidth(280)

    def set_sources(self, sources: list) -> None:
        self._updating = True
        self.list_widget.clear()
        for source in sources:
            item = QListWidgetItem(self._format_item(source))
            item.setData(Qt.ItemDataRole.UserRole, source.path)
            self.list_widget.addItem(item)
        self._updating = False

    def set_active_index(self, index: int) -> None:
        if index < 0 or index >= self.list_widget.count():
            return
        self._updating = True
        self.list_widget.setCurrentRow(index)
        self._updating = False

    def highlight_sequence_index(self, index: int) -> None:
        if index < 0 or index >= self.list_widget.count():
            return
        self._updating = True
        self.list_widget.setCurrentRow(index)
        self._updating = False

    def _format_item(self, source) -> str:
        name = os.path.basename(source.path)
        return f"{name} ({source.frame_count} fr)"

    def _on_row_changed(self, row: int) -> None:
        if self._updating or row < 0:
            return
        self.source_selected.emit(row)

    def _on_rows_moved(self, parent, start, end, destination, row) -> None:
        if self._updating:
            return
        QTimer.singleShot(0, self._emit_current_order)

    def _emit_current_order(self) -> None:
        if self._updating:
            return
        paths = []
        for index in range(self.list_widget.count()):
            item = self.list_widget.item(index)
            if item is not None:
                paths.append(item.data(Qt.ItemDataRole.UserRole))
        if paths:
            self.order_changed.emit(paths)

    def _remove_current(self) -> None:
        row = self.list_widget.currentRow()
        if row >= 0:
            self.source_removed.emit(row)
