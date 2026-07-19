"""Image-normalized ↔ widget mapping and hit-testing for annotations."""

from __future__ import annotations

import math
from typing import Optional

from PySide6.QtCore import QPointF, QRectF

from ..overlay_geometry import displayed_image_rect
from .models import AnnotationKind, AnnotationShape


def image_rect_for_viewport(
    widget_w: float,
    widget_h: float,
    scale_x: float,
    scale_y: float,
    zoom: float,
    pan_x_px: float,
    pan_y_px: float,
) -> QRectF:
    return displayed_image_rect(
        widget_w, widget_h, scale_x, scale_y, zoom, pan_x_px, pan_y_px
    )


def widget_to_uv(
    widget_x: float,
    widget_y: float,
    image_rect: QRectF,
) -> Optional[tuple[float, float]]:
    """Map widget pixel to image UV in 0..1, or None if outside the image rect."""
    if image_rect.isEmpty() or image_rect.width() <= 0 or image_rect.height() <= 0:
        return None
    u = (widget_x - image_rect.left()) / image_rect.width()
    v = (widget_y - image_rect.top()) / image_rect.height()
    if u < 0.0 or u > 1.0 or v < 0.0 or v > 1.0:
        return None
    return (float(u), float(v))


def uv_to_widget(u: float, v: float, image_rect: QRectF) -> QPointF:
    return QPointF(
        image_rect.left() + float(u) * image_rect.width(),
        image_rect.top() + float(v) * image_rect.height(),
    )


def thickness_px(shape: AnnotationShape, image_rect: QRectF) -> float:
    return max(1.0, float(shape.thickness) * max(1.0, image_rect.height()))


def _dist_point_to_segment(
    px: float, py: float, ax: float, ay: float, bx: float, by: float
) -> float:
    dx = bx - ax
    dy = by - ay
    len2 = dx * dx + dy * dy
    if len2 <= 1e-12:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / len2))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def hit_test_shape(
    shape: AnnotationShape,
    u: float,
    v: float,
    *,
    image_rect: QRectF,
    tol_px: float = 8.0,
) -> bool:
    """Return True if (u,v) is within *tol_px* of *shape* in widget space."""
    if image_rect.isEmpty() or not shape.points:
        return False
    tol_u = tol_px / max(1.0, image_rect.width())
    tol_v = tol_px / max(1.0, image_rect.height())
    tol = max(tol_u, tol_v)

    pts = shape.points
    if shape.kind == AnnotationKind.TEXT:
        ax, ay = pts[0]
        # Approximate text box ~ 0.12 x 0.04 of image.
        return (abs(u - ax) <= max(0.06, tol)) and (abs(v - ay) <= max(0.03, tol))

    if shape.kind in (AnnotationKind.RECT, AnnotationKind.ELLIPSE):
        if len(pts) < 2:
            return False
        x0, y0 = pts[0]
        x1, y1 = pts[1]
        left, right = min(x0, x1), max(x0, x1)
        top, bottom = min(y0, y1), max(y0, y1)
        # Expand by tolerance; hit near edge or inside for select.
        if left - tol <= u <= right + tol and top - tol <= v <= bottom + tol:
            # Prefer edge hits but allow interior for small shapes.
            near_edge = (
                abs(u - left) <= tol
                or abs(u - right) <= tol
                or abs(v - top) <= tol
                or abs(v - bottom) <= tol
            )
            small = (right - left) < 0.05 or (bottom - top) < 0.05
            return near_edge or small or (left <= u <= right and top <= v <= bottom)
        return False

    # Polyline / line / arrow / freehand
    if len(pts) == 1:
        return abs(u - pts[0][0]) <= tol and abs(v - pts[0][1]) <= tol
    for i in range(len(pts) - 1):
        ax, ay = pts[i]
        bx, by = pts[i + 1]
        if _dist_point_to_segment(u, v, ax, ay, bx, by) <= tol:
            return True
    return False


def hit_test_topmost(
    shapes: list[AnnotationShape],
    u: float,
    v: float,
    *,
    image_rect: QRectF,
    tol_px: float = 8.0,
) -> Optional[int]:
    """Return index of topmost hit shape, or None."""
    for i in range(len(shapes) - 1, -1, -1):
        if hit_test_shape(shapes[i], u, v, image_rect=image_rect, tol_px=tol_px):
            return i
    return None


def translate_shape(shape: AnnotationShape, du: float, dv: float) -> None:
    shape.points = [
        (max(0.0, min(1.0, u + du)), max(0.0, min(1.0, v + dv)))
        for u, v in shape.points
    ]
