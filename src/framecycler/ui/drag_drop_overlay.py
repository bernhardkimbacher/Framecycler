from PySide6.QtCore import Qt, QPoint, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QWidget

from .fonts import ui_font


class DropTargetMixin:
    """Shared file drag-and-drop forwarding to a MainWindow.

    Any including widget must set ``self._main_window`` and call
    ``self.setAcceptDrops(True)``. Widgets need this instead of relying on
    propagation up to MainWindow because Qt's window-container widget (used
    to embed the native QRhi/Metal render surface) does not support drag and
    drop at all (documented Qt limitation), so a real, non-native Qt widget
    layered in front of that surface (e.g. the transparent HUD overlay) must
    claim the drop explicitly.
    """

    def _dnd_target_pos(self, event_pos):
        main_window = getattr(self, "_main_window", None)
        if main_window is None:
            return event_pos
        return main_window.mapFromGlobal(self.mapToGlobal(event_pos.toPoint()))

    def dragEnterEvent(self, event):
        main_window = getattr(self, "_main_window", None)
        if main_window is None or not event.mimeData().hasUrls():
            event.ignore()
            return
        main_window._drag_enter_count += 1
        self.setGeometry(main_window.rect())
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)
        self.show()
        self.raise_()
        event.acceptProposedAction()

    def dragMoveEvent(self, event):
        main_window = getattr(self, "_main_window", None)
        if main_window is None or not event.mimeData().hasUrls():
            event.ignore()
            return
        pos = self._dnd_target_pos(event.position())
        zone = (
            DragDropOverlay.ZONE_REPLACE
            if pos.x() < main_window.width() * 0.5
            else DragDropOverlay.ZONE_ADD
        )
        main_window._drag_drop_zone = zone
        self.set_active_zone(zone)
        event.acceptProposedAction()

    def dragLeaveEvent(self, event):
        main_window = getattr(self, "_main_window", None)
        if main_window is None:
            event.ignore()
            return
        main_window._drag_enter_count = max(0, main_window._drag_enter_count - 1)
        if main_window._drag_enter_count == 0:
            self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
            self.hide()
        event.accept()

    def dropEvent(self, event):
        main_window = getattr(self, "_main_window", None)
        if main_window is None or not event.mimeData().hasUrls():
            event.ignore()
            return
        main_window._drag_enter_count = 0
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.hide()
        paths = [url.toLocalFile() for url in event.mimeData().urls() if url.toLocalFile()]
        if paths:
            replace = main_window._drag_drop_zone == DragDropOverlay.ZONE_REPLACE
            main_window._add_media(paths, replace=replace)
        event.acceptProposedAction()


class DragDropOverlay(DropTargetMixin, QWidget):
    zone_changed = Signal(str)

    ZONE_REPLACE = "replace"
    ZONE_ADD = "add"

    def __init__(self, parent=None, main_window=None, floating: bool = False):
        if floating:
            super().__init__(
                parent,
                Qt.WindowType.Tool
                | Qt.WindowType.FramelessWindowHint
                | Qt.WindowType.WindowStaysOnTopHint,
            )
            self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
            self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
            self.setWindowFlag(Qt.WindowType.WindowTransparentForInput, True)
        else:
            super().__init__(parent)
            self.setAcceptDrops(True)
            self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._main_window = main_window if main_window is not None else parent
        self._active_zone = self.ZONE_REPLACE
        self._split_x: int | None = None
        self.hide()

    def split_x_for_main_window(self, main_window) -> int:
        window_mid = main_window.mapToGlobal(QPoint(main_window.width() // 2, 0))
        return self.mapFromGlobal(window_mid).x()

    def set_split_x(self, split_x: int | None) -> None:
        if self._split_x != split_x:
            self._split_x = split_x
            self.update()

    def set_active_zone(self, zone: str) -> None:
        if zone != self._active_zone:
            self._active_zone = zone
            self.zone_changed.emit(zone)
            self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = self.rect()
        mid_x = self._split_x if self._split_x is not None else rect.width() // 2
        mid_x = max(0, min(mid_x, rect.width()))

        replace_active = self._active_zone == self.ZONE_REPLACE
        add_active = self._active_zone == self.ZONE_ADD

        replace_color = QColor(40, 90, 160, 180 if replace_active else 100)
        add_color = QColor(40, 140, 90, 180 if add_active else 100)

        painter.fillRect(0, 0, mid_x, rect.height(), replace_color)
        painter.fillRect(mid_x, 0, rect.width() - mid_x, rect.height(), add_color)

        painter.setPen(QPen(QColor(255, 255, 255, 200), 2))
        painter.drawLine(mid_x, 0, mid_x, rect.height())

        font = ui_font(16, QFont.Weight.Bold)
        painter.setFont(font)
        painter.setPen(QColor(255, 255, 255))

        replace_rect = rect.adjusted(0, 0, -rect.width() // 2, 0)
        add_rect = rect.adjusted(rect.width() // 2, 0, 0, 0)
        painter.drawText(replace_rect, Qt.AlignmentFlag.AlignCenter, "Replace Media")
        painter.drawText(add_rect, Qt.AlignmentFlag.AlignCenter, "Add Media")

        hint_font = ui_font(11)
        painter.setFont(hint_font)
        painter.drawText(
            replace_rect.adjusted(12, 40, -12, -12),
            Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
            "Clear existing sources\nand load dropped files",
        )
        painter.drawText(
            add_rect.adjusted(12, 40, -12, -12),
            Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
            "Append dropped files\nto the source list",
        )
        painter.end()
