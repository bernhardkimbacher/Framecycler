"""QPainter helpers for annotation shapes."""

from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QPolygonF

from .geometry import thickness_px, uv_to_widget
from .models import AnnotationKind, AnnotationShape


def _pen_for(shape: AnnotationShape, image_rect: QRectF, *, selected: bool = False) -> QPen:
    color = QColor(shape.color)
    if not color.isValid():
        color = QColor("#FFCC00")
    width = thickness_px(shape, image_rect)
    if selected:
        width = max(width, width + 1.5)
    pen = QPen(color, width)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    if selected:
        pen.setStyle(Qt.PenStyle.DashLine)
    return pen


def _arrow_head(p0: QPointF, p1: QPointF, size: float) -> QPolygonF:
    angle = math.atan2(p1.y() - p0.y(), p1.x() - p0.x())
    left = QPointF(
        p1.x() - size * math.cos(angle - math.pi / 6),
        p1.y() - size * math.sin(angle - math.pi / 6),
    )
    right = QPointF(
        p1.x() - size * math.cos(angle + math.pi / 6),
        p1.y() - size * math.sin(angle + math.pi / 6),
    )
    return QPolygonF([p1, left, right])


def paint_shape(painter: QPainter, shape: AnnotationShape, image_rect: QRectF) -> None:
    if not shape.points or image_rect.isEmpty():
        return
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    pen = _pen_for(shape, image_rect, selected=shape.selected)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)

    pts = [uv_to_widget(u, v, image_rect) for u, v in shape.points]

    if shape.kind == AnnotationKind.FREEHAND:
        if len(pts) == 1:
            painter.drawPoint(pts[0])
        else:
            for i in range(len(pts) - 1):
                painter.drawLine(pts[i], pts[i + 1])
        return

    if shape.kind in (AnnotationKind.LINE, AnnotationKind.ARROW):
        if len(pts) < 2:
            return
        painter.drawLine(pts[0], pts[1])
        if shape.kind == AnnotationKind.ARROW:
            size = max(8.0, thickness_px(shape, image_rect) * 4.0)
            head = _arrow_head(pts[0], pts[1], size)
            painter.setBrush(QColor(shape.color))
            painter.drawPolygon(head)
            painter.setBrush(Qt.BrushStyle.NoBrush)
        return

    if shape.kind in (AnnotationKind.RECT, AnnotationKind.ELLIPSE):
        if len(pts) < 2:
            return
        rect = QRectF(pts[0], pts[1]).normalized()
        if shape.kind == AnnotationKind.RECT:
            painter.drawRect(rect)
        else:
            painter.drawEllipse(rect)
        return

    if shape.kind == AnnotationKind.TEXT:
        anchor = pts[0]
        font = QFont()
        font.setPixelSize(max(10, int(round(thickness_px(shape, image_rect) * 6))))
        painter.setFont(font)
        painter.setPen(QColor(shape.color))
        text = shape.text or "Text"
        painter.drawText(anchor, text)
        if shape.selected:
            painter.setPen(_pen_for(shape, image_rect, selected=True))
            br = painter.fontMetrics().boundingRect(text)
            painter.drawRect(
                QRectF(anchor.x() - 2, anchor.y() - br.height(), br.width() + 4, br.height() + 4)
            )
