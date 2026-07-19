"""Pure geometry helpers for review overlays (mask / safe guides)."""

from __future__ import annotations

from PySide6.QtCore import QRectF


def displayed_image_rect(
    widget_w: float,
    widget_h: float,
    scale_x: float,
    scale_y: float,
    zoom: float,
    pan_x_px: float,
    pan_y_px: float,
) -> QRectF:
    """Widget-space rectangle of the fitted/zoomed primary image."""
    if widget_w <= 0 or widget_h <= 0:
        return QRectF()
    iw = widget_w * scale_x * zoom
    ih = widget_h * scale_y * zoom
    cx = widget_w * 0.5 + pan_x_px
    cy = widget_h * 0.5 + pan_y_px
    return QRectF(cx - iw * 0.5, cy - ih * 0.5, iw, ih)


def aspect_mask_rect(image_rect: QRectF, aspect: float) -> QRectF:
    """Largest centered sub-rect of *image_rect* with width/height = *aspect*."""
    if image_rect.isEmpty() or aspect <= 0.0:
        return QRectF(image_rect)
    iw = image_rect.width()
    ih = image_rect.height()
    if iw <= 0 or ih <= 0:
        return QRectF(image_rect)
    image_aspect = iw / ih
    if image_aspect > aspect:
        # Pillarbox: constrain width
        tw = ih * aspect
        th = ih
    else:
        # Letterbox: constrain height
        tw = iw
        th = iw / aspect
    return QRectF(
        image_rect.center().x() - tw * 0.5,
        image_rect.center().y() - th * 0.5,
        tw,
        th,
    )


def safe_inset_rect(image_rect: QRectF, inset_fraction: float) -> QRectF:
    """Centered inset of *image_rect*; *inset_fraction* is per-side (0.05 = 5%)."""
    if image_rect.isEmpty() or inset_fraction <= 0.0:
        return QRectF(image_rect)
    inset = max(0.0, min(0.49, float(inset_fraction)))
    dx = image_rect.width() * inset
    dy = image_rect.height() * inset
    return image_rect.adjusted(dx, dy, -dx, -dy)
