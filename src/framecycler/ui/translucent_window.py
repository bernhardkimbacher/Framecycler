"""Helpers for floating translucent Tool overlays above the native QRhi surface.

macOS retains prior pixels in WA_TranslucentBackground top-levels unless the
backing store is cleared with CompositionMode_Clear. System drop shadows on
frameless windows also leave debris that looks like a soft halo.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QRect
from PySide6.QtGui import QColor, QPainter

FLOATING_OVERLAY_FLAGS = (
    Qt.WindowType.Tool
    | Qt.WindowType.FramelessWindowHint
    | Qt.WindowType.WindowDoesNotAcceptFocus
    | Qt.WindowType.NoDropShadowWindowHint
)


def clear_translucent_backdrop(painter: QPainter, rect: QRect) -> None:
    """Wipe retained translucent-window pixels before painting overlay content."""
    painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
    painter.fillRect(rect, QColor(0, 0, 0, 0))
    painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
