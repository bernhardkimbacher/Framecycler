"""Floating translucent overlay wipe / residual-halo helpers (review #12)."""

from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QApplication

from src.framecycler.ui.translucent_window import (
    FLOATING_OVERLAY_FLAGS,
    render_overlay_to_image,
)


class TestTranslucentOverlay(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_floating_flags_include_no_drop_shadow(self):
        self.assertTrue(
            bool(FLOATING_OVERLAY_FLAGS & Qt.WindowType.NoDropShadowWindowHint)
        )

    def test_image_buffer_clears_prior_aa_stroke(self):
        """Simulate shrink: thick AA stroke then empty paint → exterior alpha 0."""
        w, h, dpr = 64, 64, 1.0

        def paint_thick(painter: QPainter) -> None:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            pen = QPen(QColor(255, 200, 0, 255), 12.0)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(pen)
            painter.drawLine(8, 32, 56, 32)

        image = render_overlay_to_image(w, h, dpr, paint_thick)
        # Stroke should have written non-zero alpha near the center.
        center = image.pixelColor(32, 32)
        self.assertGreater(center.alpha(), 0)

        def paint_empty(painter: QPainter) -> None:
            return

        image = render_overlay_to_image(w, h, dpr, paint_empty, image=image)
        # Full clear: every pixel transparent (no residual halo in the buffer).
        for y in (0, 16, 32, 48, 63):
            for x in (0, 16, 32, 48, 63):
                self.assertEqual(
                    image.pixelColor(x, y).alpha(),
                    0,
                    f"residual alpha at ({x},{y})",
                )

    def test_image_buffer_smaller_stroke_leaves_old_extent_clear(self):
        """After shrinking the stroke, pixels outside the new stroke stay alpha 0."""
        w, h, dpr = 80, 40, 1.0

        def paint_long(painter: QPainter) -> None:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            pen = QPen(QColor(0, 255, 0, 255), 8.0)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(pen)
            painter.drawLine(4, 20, 76, 20)

        image = render_overlay_to_image(w, h, dpr, paint_long)
        self.assertGreater(image.pixelColor(70, 20).alpha(), 0)

        def paint_short(painter: QPainter) -> None:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            pen = QPen(QColor(0, 255, 0, 255), 8.0)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(pen)
            painter.drawLine(20, 20, 40, 20)

        image = render_overlay_to_image(w, h, dpr, paint_short, image=image)
        # Far-right extent of the old stroke must be fully cleared.
        self.assertEqual(image.pixelColor(70, 20).alpha(), 0)
        self.assertEqual(image.pixelColor(4, 20).alpha(), 0)
        # New stroke still present near center.
        self.assertGreater(image.pixelColor(30, 20).alpha(), 0)


if __name__ == "__main__":
    unittest.main()
