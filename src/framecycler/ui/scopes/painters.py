"""Render scope accumulators to QImage for QPainter blit."""

from __future__ import annotations

import math

import numpy as np
from PySide6.QtCore import QPointF, QRectF
from PySide6.QtGui import QColor, QImage, QPainter, QPen, QPolygonF

from .analysis import ScopeType
from .cie_data import (
    CIE_LOCUS_XY,
    D65_WHITE_XY,
    P3_D65_PRIMARIES_XY,
    REC709_PRIMARIES_XY,
)


def _log_norm(buf: np.ndarray) -> np.ndarray:
    """Log-scale normalize density buffer to 0..1."""
    m = float(buf.max()) if buf.size else 0.0
    if m <= 0.0:
        return np.zeros_like(buf, dtype=np.float32)
    return (np.log1p(buf) / math.log1p(m)).astype(np.float32, copy=False)


def _dilate_vertical(density: np.ndarray, radius: int = 2) -> np.ndarray:
    """Thicken horizontal traces so they survive downscale in the widget."""
    if radius <= 0:
        return density
    out = density.copy()
    for d in range(1, radius + 1):
        out[:-d, :] = np.maximum(out[:-d, :], density[d:, :])
        out[d:, :] = np.maximum(out[d:, :], density[:-d, :])
    return out


def _rgba_qimage(rgba: np.ndarray) -> QImage:
    """Build a deep-copied QImage from HxWx4 uint8 RGBA."""
    arr = np.ascontiguousarray(rgba, dtype=np.uint8)
    h, w, _ = arr.shape
    return QImage(arr.data, w, h, w * 4, QImage.Format.Format_RGBA8888).copy()


def density_to_qimage(
    density: np.ndarray,
    *,
    tint: tuple[int, int, int] = (80, 220, 120),
    flip_y: bool = True,
    dilate: int = 0,
) -> QImage:
    """Map 2D density [H,W] to RGBA8888 with alpha (empty = transparent)."""
    buf = _dilate_vertical(density, dilate) if dilate > 0 else density
    h, w = buf.shape
    norm = _log_norm(buf)
    if flip_y:
        norm = np.ascontiguousarray(np.flipud(norm))
    a = np.clip(norm * 255.0 * 1.35, 0, 255)
    img = np.zeros((h, w, 4), dtype=np.uint8)
    img[..., 0] = (a * (tint[0] / 255.0)).astype(np.uint8)
    img[..., 1] = (a * (tint[1] / 255.0)).astype(np.uint8)
    img[..., 2] = (a * (tint[2] / 255.0)).astype(np.uint8)
    img[..., 3] = a.astype(np.uint8)
    return _rgba_qimage(img)


def parade_to_qimage(density: np.ndarray, *, dilate: int = 2) -> QImage:
    """Parade [bins, 3*w] → RGB-tinted columns with alpha."""
    bins, total_w = density.shape
    w = total_w // 3
    parts = []
    tints = ((255, 70, 70), (70, 255, 90), (80, 140, 255))
    for i, tint in enumerate(tints):
        section = _dilate_vertical(density[:, i * w : (i + 1) * w], dilate)
        norm = np.ascontiguousarray(np.flipud(_log_norm(section)))
        a = np.clip(norm * 255.0 * 1.35, 0, 255)
        img = np.zeros((bins, w, 4), dtype=np.uint8)
        img[..., 0] = (a * (tint[0] / 255.0)).astype(np.uint8)
        img[..., 1] = (a * (tint[1] / 255.0)).astype(np.uint8)
        img[..., 2] = (a * (tint[2] / 255.0)).astype(np.uint8)
        img[..., 3] = a.astype(np.uint8)
        parts.append(img)
    return _rgba_qimage(np.concatenate(parts, axis=1))


def histogram_to_qimage(hist: np.ndarray, width: int = 512, height: int = 256) -> QImage:
    """Overlay R/G/B histograms into a transparent image."""
    bins = hist.shape[1]
    img = np.zeros((height, width, 4), dtype=np.uint8)
    peak = float(hist.max()) if hist.size else 1.0
    peak = max(peak, 1.0)
    xs = np.linspace(0, bins - 1, width)
    for c, tint in enumerate(((255, 70, 70), (70, 255, 90), (80, 140, 255))):
        samples = np.interp(xs, np.arange(bins), hist[c])
        for x in range(width):
            bar_h = int(round((samples[x] / peak) * (height - 1)))
            if bar_h <= 0:
                continue
            y0 = height - bar_h
            img[y0:height, x, 0] = np.maximum(img[y0:height, x, 0], tint[0])
            img[y0:height, x, 1] = np.maximum(img[y0:height, x, 1], tint[1])
            img[y0:height, x, 2] = np.maximum(img[y0:height, x, 2], tint[2])
            img[y0:height, x, 3] = np.maximum(img[y0:height, x, 3], 220)
    return _rgba_qimage(img)


def _xy_to_widget(x: float, y: float, rect: QRectF) -> QPointF:
    # Same window as accumulate_cie: x 0..0.8, y 0..0.9
    px = rect.left() + (x / 0.8) * rect.width()
    py = rect.bottom() - (y / 0.9) * rect.height()
    return QPointF(px, py)


def draw_scope_chrome(
    painter: QPainter,
    rect: QRectF,
    scope_type: ScopeType,
    *,
    label: str = "",
    fill_background: bool = True,
) -> None:
    """Scope chrome: optional dark fill + grid / circle / CIE overlays."""
    if fill_background:
        painter.fillRect(rect, QColor(12, 14, 16))
    pen = QPen(QColor(45, 50, 55))
    pen.setWidthF(1.0)
    painter.setPen(pen)

    if scope_type in (ScopeType.WAVEFORM, ScopeType.PARADE, ScopeType.HISTOGRAM):
        for t in (0.0, 0.25, 0.5, 0.75, 1.0):
            y = rect.bottom() - t * rect.height()
            painter.drawLine(QPointF(rect.left(), y), QPointF(rect.right(), y))
        if scope_type == ScopeType.PARADE:
            for i in (1, 2):
                x = rect.left() + (i / 3.0) * rect.width()
                painter.drawLine(QPointF(x, rect.top()), QPointF(x, rect.bottom()))
        painter.setPen(QColor(120, 130, 140))
        painter.drawText(QPointF(rect.left() + 4, rect.top() + 12), "1023")
        painter.drawText(QPointF(rect.left() + 4, rect.bottom() - 4), "0")
    elif scope_type == ScopeType.VECTORSCOPE:
        cx = rect.center().x()
        cy = rect.center().y()
        rad = min(rect.width(), rect.height()) * 0.45
        painter.setPen(QPen(QColor(55, 60, 65), 1.0))
        painter.drawEllipse(QPointF(cx, cy), rad, rad)
        painter.drawEllipse(QPointF(cx, cy), rad * 0.75, rad * 0.75)
        painter.drawLine(QPointF(cx - rad, cy), QPointF(cx + rad, cy))
        painter.drawLine(QPointF(cx, cy - rad), QPointF(cx, cy + rad))
        ang = math.radians(123.0)
        painter.setPen(QPen(QColor(180, 140, 90), 1.0))
        painter.drawLine(
            QPointF(cx, cy),
            QPointF(cx + rad * math.cos(ang), cy - rad * math.sin(ang)),
        )
        targets = (
            ("R", 0.44, 0.0),
            ("Y", 0.31, 0.31),
            ("G", -0.13, 0.44),
            ("C", -0.44, 0.0),
            ("B", -0.31, -0.31),
            ("M", 0.13, -0.44),
        )
        painter.setPen(QColor(200, 200, 200))
        for name, cb, cr in targets:
            px = cx + cb * rad * 2.0
            py = cy - cr * rad * 2.0
            painter.drawRect(QRectF(px - 3, py - 3, 6, 6))
            painter.drawText(QPointF(px + 5, py - 2), name)
    elif scope_type == ScopeType.CIE:
        painter.setPen(QPen(QColor(80, 90, 100), 1.2))
        pts = [_xy_to_widget(x, y, rect) for x, y in CIE_LOCUS_XY]
        for i in range(len(pts) - 1):
            painter.drawLine(pts[i], pts[i + 1])
        for primaries, color in (
            (REC709_PRIMARIES_XY, QColor(100, 180, 255)),
            (P3_D65_PRIMARIES_XY, QColor(255, 180, 80)),
        ):
            painter.setPen(QPen(color, 1.2))
            tpts = [_xy_to_widget(x, y, rect) for x, y in primaries]
            painter.drawPolygon(QPolygonF(tpts))
        wpt = _xy_to_widget(D65_WHITE_XY[0], D65_WHITE_XY[1], rect)
        painter.setPen(QColor(220, 220, 220))
        painter.drawEllipse(wpt, 3, 3)
        painter.drawText(wpt + QPointF(5, -2), "D65")
        painter.setPen(QColor(100, 180, 255))
        painter.drawText(QPointF(rect.left() + 6, rect.top() + 14), "Rec.709")
        painter.setPen(QColor(255, 180, 80))
        painter.drawText(QPointF(rect.left() + 6, rect.top() + 28), "P3 D65")

    if label:
        painter.setPen(QColor(160, 170, 180))
        painter.drawText(QPointF(rect.right() - 8 - len(label) * 6, rect.top() + 14), label)


def accumulator_image(
    scope_type: ScopeType,
    data: np.ndarray,
    *,
    dilate: int | None = None,
) -> QImage | None:
    if data is None or data.size == 0:
        return None
    if scope_type == ScopeType.WAVEFORM:
        d = 2 if dilate is None else dilate
        return density_to_qimage(data, tint=(80, 220, 120), dilate=d)
    if scope_type == ScopeType.PARADE:
        d = 2 if dilate is None else dilate
        return parade_to_qimage(data, dilate=d)
    if scope_type == ScopeType.VECTORSCOPE:
        d = 1 if dilate is None else dilate
        return density_to_qimage(data, tint=(230, 230, 230), flip_y=False, dilate=d)
    if scope_type == ScopeType.HISTOGRAM:
        return histogram_to_qimage(data)
    if scope_type == ScopeType.CIE:
        d = 1 if dilate is None else dilate
        return density_to_qimage(data, tint=(210, 170, 255), flip_y=False, dilate=d)
    return None
