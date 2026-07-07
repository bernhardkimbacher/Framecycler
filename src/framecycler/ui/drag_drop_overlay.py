from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QWidget

from .fonts import ui_font


class DragDropOverlay(QWidget):
    zone_changed = Signal(str)

    ZONE_REPLACE = "replace"
    ZONE_ADD = "add"

    def __init__(self, parent=None):
        super().__init__(parent)
        self._active_zone = self.ZONE_REPLACE
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.hide()

    def set_active_zone(self, zone: str) -> None:
        if zone != self._active_zone:
            self._active_zone = zone
            self.zone_changed.emit(zone)
            self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = self.rect()
        mid_x = rect.width() // 2

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
