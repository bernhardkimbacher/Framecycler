"""Unit tests for pixel probe sampling helpers."""

import unittest

import numpy as np

from src.framecycler.ui.overlay_geometry import displayed_image_rect
from src.framecycler.ui.probe_sampling import (
    apply_ocio_rgb_array,
    encode_levels,
    extract_neighborhood,
    magnifier_cell_size,
    magnifier_texel_counts,
    sample_probe,
    widget_to_image_xy,
)


class TestProbeSampling(unittest.TestCase):
    def test_widget_to_image_center(self):
        # Full-bleed image in 200x100 widget
        mapped = widget_to_image_xy(
            100, 50, 200, 100, 1.0, 1.0, 1.0, 0.0, 0.0, 1920, 1080
        )
        self.assertIsNotNone(mapped)
        x, y = mapped
        self.assertAlmostEqual(x, 960, delta=1)
        self.assertAlmostEqual(y, 540, delta=1)

    def test_widget_to_image_outside(self):
        # Pillarboxed: scale_x=0.5 → image is 100px wide centered in 200
        mapped = widget_to_image_xy(
            10, 50, 200, 100, 0.5, 1.0, 1.0, 0.0, 0.0, 100, 100
        )
        self.assertIsNone(mapped)

    def test_widget_to_image_corners(self):
        mapped = widget_to_image_xy(
            0, 0, 100, 100, 1.0, 1.0, 1.0, 0.0, 0.0, 10, 10
        )
        self.assertEqual(mapped, (0, 0))
        mapped = widget_to_image_xy(
            99.9, 99.9, 100, 100, 1.0, 1.0, 1.0, 0.0, 0.0, 10, 10
        )
        self.assertEqual(mapped, (9, 9))

    def test_widget_to_image_matches_rect_at_zoom(self):
        """NDC inverse must stay aligned with displayed_image_rect under zoom/pan."""
        ww, wh = 1600, 900
        sx, sy, zoom = 0.8, 1.0, 2.5
        pan_x, pan_y = 40.0, -25.0
        iw, ih = 3840, 2160
        rect = displayed_image_rect(ww, wh, sx, sy, zoom, pan_x, pan_y)
        # Sample a few points inside the image rect
        for u, v in ((0.1, 0.1), (0.5, 0.5), (0.9, 0.75)):
            wx = rect.left() + u * rect.width()
            wy = rect.top() + v * rect.height()
            mapped = widget_to_image_xy(
                wx, wy, ww, wh, sx, sy, zoom, pan_x, pan_y, iw, ih
            )
            self.assertIsNotNone(mapped)
            x, y = mapped
            self.assertAlmostEqual(x, int(u * iw), delta=1)
            self.assertAlmostEqual(y, int(v * ih), delta=1)

    def test_widget_to_image_actual_size_zoom(self):
        """Fit → 100% (actual size) keeps the widget-center pixel stable."""
        ww, wh = 1920, 1080
        iw, ih = 3840, 2160
        # Pillarbox fit
        sx, sy = 0.75, 1.0
        z_fit = 1.0
        z_100 = (iw * 1.0) / (ww * sx)  # actual-size zoom
        cx, cy = ww * 0.5, wh * 0.5
        fit = widget_to_image_xy(cx, cy, ww, wh, sx, sy, z_fit, 0, 0, iw, ih)
        actual = widget_to_image_xy(cx, cy, ww, wh, sx, sy, z_100, 0, 0, iw, ih)
        self.assertEqual(fit, (iw // 2, ih // 2))
        self.assertEqual(actual, (iw // 2, ih // 2))

    def test_extract_neighborhood_clamps(self):
        frame = np.zeros((8, 8, 4), dtype=np.float32)
        frame[0, 0] = (1, 0, 0, 1)
        frame[7, 7] = (0, 1, 0, 1)
        patch = extract_neighborhood(frame, 0, 0, radius=2)
        self.assertEqual(patch.shape, (5, 5, 4))
        np.testing.assert_array_equal(patch[2, 2], (1, 0, 0, 1))

    def test_encode_levels(self):
        f, u8, u10 = encode_levels(0.5)
        self.assertAlmostEqual(f, 0.5)
        self.assertEqual(u8, 128)
        self.assertEqual(u10, 512)

    def test_sample_probe_identity_ocio(self):
        frame = np.zeros((4, 4, 4), dtype=np.float32)
        frame[2, 1] = (0.25, 0.5, 0.75, 1.0)
        sample = sample_probe(frame, 1, 2, timeline_frame=42, ocio_manager=None, radius=1)
        self.assertEqual(sample.image_x, 1)
        self.assertEqual(sample.image_y, 2)
        self.assertEqual(sample.frame, 42)
        self.assertAlmostEqual(sample.source_rgba[0], 0.25)
        self.assertAlmostEqual(sample.source_rgba[1], 0.5)
        self.assertAlmostEqual(sample.source_rgba[2], 0.75)
        self.assertEqual(sample.display_rgb, sample.source_rgba[:3])
        self.assertEqual(sample.neighborhood.shape, (3, 3, 3))
        self.assertEqual(sample.neighborhood_display.shape, (3, 3, 3))
        np.testing.assert_allclose(sample.neighborhood_display, sample.neighborhood)

    def test_apply_ocio_rgb_array_passthrough(self):
        patch = np.linspace(0, 1, 27, dtype=np.float32).reshape(3, 3, 3)
        out = apply_ocio_rgb_array(None, patch)
        np.testing.assert_allclose(out, patch)

    def test_magnifier_cell_size_respects_par(self):
        self.assertEqual(magnifier_cell_size(1.0, base_px=12), (12, 12))
        self.assertEqual(magnifier_cell_size(2.0, base_px=12), (24, 12))
        self.assertEqual(magnifier_cell_size(0.5, base_px=12), (6, 12))

    def test_magnifier_texel_counts_keep_square_view(self):
        """PAR=2 uses fewer columns so the magnified pixmap stays ~square."""
        cols_sq, rows_sq = magnifier_texel_counts(180, 180, 1.0, base_px=12)
        self.assertEqual(cols_sq, rows_sq)
        cols_ana, rows_ana = magnifier_texel_counts(180, 180, 2.0, base_px=12)
        self.assertLess(cols_ana, rows_ana)
        cell_w, cell_h = magnifier_cell_size(2.0, base_px=12)
        pix_w = cols_ana * cell_w
        pix_h = rows_ana * cell_h
        # Within one cell of square.
        self.assertLessEqual(abs(pix_w - pix_h), max(cell_w, cell_h))

    def test_extract_neighborhood_rect(self):
        frame = np.zeros((10, 10, 3), dtype=np.float32)
        patch = extract_neighborhood(frame, 5, 5, radius_x=1, radius_y=3)
        self.assertEqual(patch.shape, (7, 3, 3))

    def test_sample_probe_stores_par(self):
        frame = np.zeros((4, 4, 3), dtype=np.float32)
        sample = sample_probe(
            frame, 1, 1, timeline_frame=0, ocio_manager=None, radius=1, pixel_aspect_ratio=2.0
        )
        self.assertAlmostEqual(sample.pixel_aspect_ratio, 2.0)


if __name__ == "__main__":
    unittest.main()
