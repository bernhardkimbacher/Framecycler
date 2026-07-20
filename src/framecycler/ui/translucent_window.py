"""Helpers for floating translucent Tool overlays above the native QRhi surface.

macOS retains prior pixels in WA_TranslucentBackground top-levels unless the
backing store is fully replaced. CompositionMode_Clear alone can leave soft AA
halos after content shrinks; Darwin therefore paints into an offscreen QImage
and blits with CompositionMode_Source. System drop shadows on frameless windows
also leave debris — NoDropShadowWindowHint is part of FLOATING_OVERLAY_FLAGS.
"""

from __future__ import annotations

import sys
from collections.abc import Callable

from PySide6.QtCore import Qt, QRect
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QWidget

FLOATING_OVERLAY_FLAGS = (
    Qt.WindowType.Tool
    | Qt.WindowType.FramelessWindowHint
    | Qt.WindowType.WindowDoesNotAcceptFocus
    | Qt.WindowType.NoDropShadowWindowHint
)

_ATTR_BUFFER = "_fc_overlay_buffer"
_ATTR_BUFFER_KEY = "_fc_overlay_buffer_key"


def configure_floating_overlay(widget: QWidget) -> None:
    """Apply shared translucent Tool-window attributes (call after __init__)."""
    widget.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
    widget.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
    widget.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)


def clear_translucent_backdrop(painter: QPainter, rect: QRect) -> None:
    """Wipe retained translucent-window pixels before painting overlay content."""
    painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
    painter.fillRect(rect, QColor(0, 0, 0, 0))
    painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)


def _use_image_buffer_path(*, force_image_buffer: bool | None) -> bool:
    if force_image_buffer is not None:
        return bool(force_image_buffer)
    return sys.platform == "darwin"


def _overlay_image_for(widget: QWidget, logical_w: int, logical_h: int, dpr: float) -> QImage:
    """Return a reused ARGB32_Premultiplied buffer sized for the widget."""
    pixel_w = max(1, int(round(logical_w * dpr)))
    pixel_h = max(1, int(round(logical_h * dpr)))
    key = (pixel_w, pixel_h, float(dpr))
    cached: QImage | None = getattr(widget, _ATTR_BUFFER, None)
    cached_key = getattr(widget, _ATTR_BUFFER_KEY, None)
    if cached is not None and cached_key == key and not cached.isNull():
        return cached
    image = QImage(pixel_w, pixel_h, QImage.Format.Format_ARGB32_Premultiplied)
    image.setDevicePixelRatio(dpr)
    setattr(widget, _ATTR_BUFFER, image)
    setattr(widget, _ATTR_BUFFER_KEY, key)
    return image


def render_overlay_to_image(
    logical_w: int,
    logical_h: int,
    dpr: float,
    paint_fn: Callable[[QPainter], None],
    *,
    image: QImage | None = None,
) -> QImage:
    """Paint overlay content into a fully cleared offscreen image (testable)."""
    pixel_w = max(1, int(round(logical_w * dpr)))
    pixel_h = max(1, int(round(logical_h * dpr)))
    if image is None or image.isNull() or image.width() != pixel_w or image.height() != pixel_h:
        image = QImage(pixel_w, pixel_h, QImage.Format.Format_ARGB32_Premultiplied)
        image.setDevicePixelRatio(dpr)
    else:
        image.setDevicePixelRatio(dpr)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    paint_fn(painter)
    painter.end()
    return image


def paint_floating_overlay(
    widget: QWidget,
    paint_fn: Callable[[QPainter], None],
    *,
    force_image_buffer: bool | None = None,
) -> None:
    """Paint a floating overlay with a platform-appropriate full wipe.

    On Darwin (or when ``force_image_buffer=True``), content is drawn into an
    offscreen QImage then blitted with CompositionMode_Source so prior AA
    fringes cannot remain. Elsewhere: CompositionMode_Clear then paint.
    """
    rect = widget.rect()
    if rect.width() <= 0 or rect.height() <= 0:
        return

    if _use_image_buffer_path(force_image_buffer=force_image_buffer):
        dpr = float(widget.devicePixelRatioF())
        image = _overlay_image_for(widget, rect.width(), rect.height(), dpr)
        render_overlay_to_image(
            rect.width(),
            rect.height(),
            dpr,
            paint_fn,
            image=image,
        )
        painter = QPainter(widget)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
        painter.drawImage(rect, image)
        painter.end()
        return

    painter = QPainter(widget)
    clear_translucent_backdrop(painter, rect)
    paint_fn(painter)
    painter.end()
