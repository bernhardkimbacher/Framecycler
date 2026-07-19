"""Simple drawn tool icons (no external assets)."""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QColor,
    QIcon,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QPolygonF,
)

from .models import AnnotationTool


def _base_pixmap(size: int = 22) -> QPixmap:
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    return pm


def tool_icon(tool: AnnotationTool, size: int = 22, *, active: bool = False) -> QIcon:
    pm = _base_pixmap(size)
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    fg = QColor("#F0F0F0") if not active else QColor("#FFFFFF")
    pen = QPen(fg, 1.6)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    m = size * 0.14
    r = QRectF(m, m, size - 2 * m, size - 2 * m)
    s = float(size)

    if tool == AnnotationTool.SELECT:
        # Classic OS mouse pointer (arrow), upright — tip near top-left.
        # Normalized to a standard cursor silhouette.
        tip = QPointF(s * 0.22, s * 0.14)
        pts = QPolygonF(
            [
                tip,
                QPointF(s * 0.22, s * 0.78),
                QPointF(s * 0.36, s * 0.64),
                QPointF(s * 0.48, s * 0.88),
                QPointF(s * 0.58, s * 0.84),
                QPointF(s * 0.46, s * 0.58),
                QPointF(s * 0.68, s * 0.58),
            ]
        )
        painter.setBrush(fg)
        outline = QPen(QColor(0, 0, 0, 180), 1.0)
        outline.setJoinStyle(Qt.PenJoinStyle.MiterJoin)
        painter.setPen(outline)
        painter.drawPolygon(pts)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawPolygon(pts)
    elif tool == AnnotationTool.FREEHAND:
        # Smooth S-curve (brush stroke), not a polyline zig-zag.
        path = QPainterPath()
        path.moveTo(s * 0.16, s * 0.72)
        path.cubicTo(
            s * 0.28, s * 0.28,
            s * 0.48, s * 0.28,
            s * 0.52, s * 0.52,
        )
        path.cubicTo(
            s * 0.56, s * 0.76,
            s * 0.72, s * 0.76,
            s * 0.84, s * 0.28,
        )
        stroke = QPen(fg, 2.0)
        stroke.setCapStyle(Qt.PenCapStyle.RoundCap)
        stroke.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(stroke)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(path)
    elif tool == AnnotationTool.LINE:
        painter.drawLine(QPointF(m, size - m), QPointF(size - m, m))
    elif tool == AnnotationTool.ARROW:
        painter.drawLine(QPointF(m, size - m), QPointF(size - m, m))
        painter.setBrush(fg)
        painter.drawPolygon(
            QPolygonF(
                [
                    QPointF(size - m, m),
                    QPointF(size - m - 6, m + 2),
                    QPointF(size - m - 2, m + 6),
                ]
            )
        )
    elif tool == AnnotationTool.RECT:
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(r.adjusted(1, 2, -1, -2))
    elif tool == AnnotationTool.ELLIPSE:
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(r)
    elif tool == AnnotationTool.TEXT:
        font = painter.font()
        font.setBold(True)
        font.setPixelSize(int(size * 0.7))
        painter.setFont(font)
        painter.drawText(QRectF(0, 0, size, size), Qt.AlignmentFlag.AlignCenter, "T")

    painter.end()
    return QIcon(pm)
