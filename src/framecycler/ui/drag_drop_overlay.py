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

    def _zone_for_x(self, x: int, width: int) -> str:
        if width <= 0:
            return DragDropOverlay.ZONE_SEQUENCE
        third = width / 3.0
        if x < third:
            return DragDropOverlay.ZONE_REPLACE
        if x < 2.0 * third:
            return DragDropOverlay.ZONE_SEQUENCE
        return DragDropOverlay.ZONE_STACK

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
        zone = self._zone_for_x(pos.x(), main_window.width())
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
            mode = main_window._drag_drop_zone or DragDropOverlay.ZONE_SEQUENCE
            main_window._add_media(paths, mode=mode)
        event.acceptProposedAction()


class DragDropOverlay(DropTargetMixin, QWidget):
    zone_changed = Signal(str)

    ZONE_REPLACE = "replace"
    ZONE_SEQUENCE = "sequence"
    ZONE_STACK = "stack"
    # Back-compat alias
    ZONE_ADD = ZONE_SEQUENCE

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
        self._active_zone = self.ZONE_SEQUENCE
        self._split_xs: list[int] | None = None
        self.hide()

    def set_split_x(self, split_x: int | None) -> None:
        # Legacy two-zone API — ignored; thirds are computed from width.
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
        w = rect.width()
        third = w // 3
        bounds = [
            (0, third),
            (third, 2 * third),
            (2 * third, w),
        ]
        zones = [self.ZONE_REPLACE, self.ZONE_SEQUENCE, self.ZONE_STACK]
        colors = [
            QColor(40, 90, 160),
            QColor(40, 140, 90),
            QColor(140, 90, 40),
        ]
        labels = ["Replace All", "Add to Timeline", "Add to Stack"]
        hints = [
            "Clear session and\nload dropped files",
            "Append each file as\na new shot in sequence",
            "Add as version(s) of\nthe shot under playhead",
        ]

        for (x0, x1), zone, color, label, hint in zip(bounds, zones, colors, labels, hints):
            active = self._active_zone == zone
            fill = QColor(color.red(), color.green(), color.blue(), 180 if active else 100)
            painter.fillRect(x0, 0, max(0, x1 - x0), rect.height(), fill)

        painter.setPen(QPen(QColor(255, 255, 255, 200), 2))
        painter.drawLine(third, 0, third, rect.height())
        painter.drawLine(2 * third, 0, 2 * third, rect.height())

        font = ui_font(15, QFont.Weight.Bold)
        painter.setFont(font)
        painter.setPen(QColor(255, 255, 255))
        hint_font = ui_font(11)

        for (x0, x1), label, hint in zip(bounds, labels, hints):
            zone_rect = rect.adjusted(x0, 0, -(w - x1), 0)
            painter.setFont(font)
            painter.drawText(zone_rect, Qt.AlignmentFlag.AlignCenter, label)
            painter.setFont(hint_font)
            painter.drawText(
                zone_rect.adjusted(10, 44, -10, -10),
                Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
                hint,
            )
        painter.end()
