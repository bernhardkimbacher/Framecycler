"""Floating pixel probe window (magnifier + source/display readouts)."""

from __future__ import annotations

import numpy as np
from PySide6.QtCore import QRect, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QGuiApplication,
    QImage,
    QKeyEvent,
    QMoveEvent,
    QPainter,
    QPen,
    QPixmap,
    QResizeEvent,
)
from PySide6.QtWidgets import QLabel, QSizePolicy, QVBoxLayout, QWidget

from .fonts import mono_font, ui_font
from .probe_sampling import (
    ProbeSample,
    encode_levels,
    magnifier_cell_size,
    magnifier_texel_counts,
)

# On-screen pixels per source texel height in the magnifier. Width is scaled by PAR.
# Growing the window increases how many texels are shown (not the same patch scaled up).
# Texel grid is rectangular under PAR so the magnified pixmap stays square.
_MAG_CELL_PX = 12
_MIN_CELLS = 5
_MAX_RADIUS = 64


class _MagnifierLabel(QLabel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(120, 120)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setStyleSheet("background: #111; border: 1px solid #333;")
        self._pix: QPixmap | None = None

    def set_neighborhood(
        self, neighborhood: np.ndarray, *, pixel_aspect_ratio: float = 1.0
    ) -> None:
        """neighborhood: float HxWx3 display-referred RGB; cells respect PAR."""
        if neighborhood.size == 0:
            self.clear()
            self._pix = None
            return
        patch = np.asarray(neighborhood[..., :3], dtype=np.float32)
        # Display-referred preview (matches viewer OCIO path including ASC CDL).
        preview = np.clip(patch, 0.0, 1.0)
        u8 = (preview * 255.0).astype(np.uint8)
        cell_w, cell_h = magnifier_cell_size(pixel_aspect_ratio, base_px=_MAG_CELL_PX)
        big = np.ascontiguousarray(np.repeat(np.repeat(u8, cell_h, axis=0), cell_w, axis=1))
        bh, bw = big.shape[0], big.shape[1]
        img = QImage(big.data, bw, bh, int(big.strides[0]), QImage.Format.Format_RGB888).copy()
        pix = QPixmap.fromImage(img)
        painter = QPainter(pix)
        painter.setPen(QPen(QColor(255, 255, 255, 220), 1))
        cx, cy = pix.width() // 2, pix.height() // 2
        painter.drawLine(cx, 0, cx, pix.height())
        painter.drawLine(0, cy, pix.width(), cy)
        painter.setPen(QPen(QColor(0, 0, 0, 180), 1))
        painter.drawRect(cx - cell_w // 2, cy - cell_h // 2, cell_w, cell_h)
        painter.end()
        self._pix = pix
        self.setPixmap(pix)


class PixelProbeWindow(QWidget):
    """Tool window for pixel analysis. Esc closes sticky (menu) mode."""

    closed_by_user = Signal()
    needs_resample = Signal()
    geometry_changed = Signal()

    def __init__(self, parent=None):
        super().__init__(
            parent,
            Qt.WindowType.Tool
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowCloseButtonHint,
        )
        self.setWindowTitle("Pixel Probe")
        self.setAttribute(Qt.WA_ShowWithoutActivating, False)
        self.setMinimumSize(240, 280)
        self._sticky = False
        self._last_texel_shape: tuple[int, int] = (-1, -1)  # (rows, cols)
        self._pixel_aspect = 1.0
        self._restoring_geometry = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(6)

        self._magnifier = _MagnifierLabel(self)
        layout.addWidget(self._magnifier, stretch=1)

        self._info = QLabel("No sample")
        self._info.setFont(mono_font(11))
        self._info.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._info.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self._info.setMinimumHeight(140)
        layout.addWidget(self._info)

        hint = QLabel("Shift opens · hover viewer · magnifier is display view · Esc closes")
        hint.setFont(ui_font(10))
        hint.setStyleSheet("color: #888;")
        layout.addWidget(hint)

        self.resize(280, 360)

    @property
    def sticky(self) -> bool:
        return self._sticky

    def set_sticky(self, sticky: bool) -> None:
        self._sticky = bool(sticky)

    def geometry_list(self) -> list[int]:
        """Return [x, y, width, height] for settings persistence."""
        g = self.frameGeometry()
        return [int(g.x()), int(g.y()), int(g.width()), int(g.height())]

    def restore_geometry_list(self, values: list[int] | None) -> bool:
        """Apply saved [x, y, w, h]; clamp onto a visible screen. Returns True if applied."""
        if not values or len(values) != 4:
            return False
        try:
            x, y, w, h = (int(v) for v in values)
        except (TypeError, ValueError):
            return False
        w = max(self.minimumWidth(), w)
        h = max(self.minimumHeight(), h)
        rect = QRect(x, y, w, h)
        screens = QGuiApplication.screens()
        if screens and not any(s.availableGeometry().intersects(rect) for s in screens):
            primary = QGuiApplication.primaryScreen()
            if primary is None:
                return False
            avail = primary.availableGeometry()
            w = min(w, avail.width())
            h = min(h, avail.height())
            rect = QRect(
                avail.x() + max(0, (avail.width() - w) // 2),
                avail.y() + max(0, (avail.height() - h) // 2),
                w,
                h,
            )
        self._restoring_geometry = True
        try:
            self.setGeometry(rect)
        finally:
            self._restoring_geometry = False
        return True

    def set_pixel_aspect_ratio(self, par: float, *, resample: bool = True) -> None:
        """Update PAR used for radius / magnifier layout; may trigger resample."""
        par = float(par) if par and par > 0.0 else 1.0
        if abs(par - self._pixel_aspect) < 1e-6:
            return
        self._pixel_aspect = par
        if resample and self.isVisible():
            self.needs_resample.emit()

    def magnifier_texel_radii(self) -> tuple[int, int]:
        """Return (radius_x, radius_y) so PAR-stretched cells fill a square view."""
        cols, rows = magnifier_texel_counts(
            self._magnifier.width(),
            self._magnifier.height(),
            self._pixel_aspect,
            base_px=_MAG_CELL_PX,
            min_cells=_MIN_CELLS,
            max_radius=_MAX_RADIUS,
        )
        return cols // 2, rows // 2

    def magnifier_radius(self) -> int:
        """Backward-compatible max radius (prefer ``magnifier_texel_radii``)."""
        rx, ry = self.magnifier_texel_radii()
        return max(rx, ry)

    def show_sample(self, sample: ProbeSample | None) -> None:
        if sample is None:
            self._magnifier.clear()
            self._info.setText("Outside image / no cache")
            return
        self._pixel_aspect = (
            float(sample.pixel_aspect_ratio)
            if sample.pixel_aspect_ratio and sample.pixel_aspect_ratio > 0.0
            else 1.0
        )
        mag = sample.neighborhood_display if sample.neighborhood_display is not None else sample.neighborhood
        self._magnifier.set_neighborhood(mag, pixel_aspect_ratio=self._pixel_aspect)
        nh, nw = int(sample.neighborhood.shape[0]), int(sample.neighborhood.shape[1])
        self._last_texel_shape = (nh, nw)
        sr, sg, sb, sa = sample.source_rgba
        dr, dg, db = sample.display_rgb

        def fmt_chan(name: str, v: float) -> str:
            f, u8, u10 = encode_levels(v)
            return f"{name} {f:8.5f}  {u8:3d}/255  {u10:4d}/1023"

        par = self._pixel_aspect
        par_note = f"   PAR {par:g}" if abs(par - 1.0) > 1e-3 else ""
        lines = [
            f"XY  {sample.image_x} , {sample.image_y}   "
            f"({sample.width}×{sample.height}){par_note}",
            f"FR  {sample.frame}   mag {nw}×{nh}",
            "",
            "Source",
            fmt_chan("R", sr),
            fmt_chan("G", sg),
            fmt_chan("B", sb),
            fmt_chan("A", sa),
            "",
            "Display (approx)",
            fmt_chan("R", dr),
            fmt_chan("G", dg),
            fmt_chan("B", db),
        ]
        self._info.setText("\n".join(lines))

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        rx, ry = self.magnifier_texel_radii()
        shape = (2 * ry + 1, 2 * rx + 1)
        if shape != self._last_texel_shape:
            self.needs_resample.emit()
        if not self._restoring_geometry:
            self.geometry_changed.emit()

    def moveEvent(self, event: QMoveEvent) -> None:
        super().moveEvent(event)
        if not self._restoring_geometry:
            self.geometry_changed.emit()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.closed_by_user.emit()
            self.hide()
            event.accept()
            return
        super().keyPressEvent(event)

    def closeEvent(self, event) -> None:
        self.closed_by_user.emit()
        super().closeEvent(event)
