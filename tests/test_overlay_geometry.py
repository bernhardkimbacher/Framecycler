"""Unit tests for review overlay geometry and blend mode constant."""

import unittest

from PySide6.QtCore import QRectF

from src.framecycler.ui.overlay_geometry import (
    aspect_mask_rect,
    displayed_image_rect,
    safe_inset_rect,
)
from src.framecycler.ui.viewport import COMPARE_BLEND, COMPARE_WIPE


class TestOverlayGeometry(unittest.TestCase):
    def test_displayed_image_rect_fit_letterbox(self):
        # Widget wider than frame → pillarbox (scale_x < 1)
        rect = displayed_image_rect(1920, 1080, 0.5, 1.0, 1.0, 0.0, 0.0)
        self.assertAlmostEqual(rect.width(), 960.0)
        self.assertAlmostEqual(rect.height(), 1080.0)
        self.assertAlmostEqual(rect.center().x(), 960.0)
        self.assertAlmostEqual(rect.center().y(), 540.0)

    def test_displayed_image_rect_applies_pan_and_zoom(self):
        rect = displayed_image_rect(1000, 1000, 1.0, 1.0, 2.0, 50.0, -20.0)
        self.assertAlmostEqual(rect.width(), 2000.0)
        self.assertAlmostEqual(rect.height(), 2000.0)
        self.assertAlmostEqual(rect.center().x(), 550.0)
        self.assertAlmostEqual(rect.center().y(), 480.0)

    def test_aspect_mask_letterbox(self):
        image = QRectF(0, 0, 1920, 1080)  # ~1.78
        mask = aspect_mask_rect(image, 2.39)
        self.assertLess(mask.height(), image.height())
        self.assertAlmostEqual(mask.width(), image.width())
        self.assertAlmostEqual(mask.width() / mask.height(), 2.39, places=2)
        self.assertAlmostEqual(mask.center().x(), image.center().x())
        self.assertAlmostEqual(mask.center().y(), image.center().y())

    def test_aspect_mask_pillarbox(self):
        image = QRectF(0, 0, 1920, 1080)
        mask = aspect_mask_rect(image, 1.33)
        self.assertLess(mask.width(), image.width())
        self.assertAlmostEqual(mask.height(), image.height())
        self.assertAlmostEqual(mask.width() / mask.height(), 1.33, places=2)

    def test_safe_inset(self):
        image = QRectF(0, 0, 1000, 500)
        safe = safe_inset_rect(image, 0.10)
        self.assertAlmostEqual(safe.width(), 800.0)
        self.assertAlmostEqual(safe.height(), 400.0)
        self.assertAlmostEqual(safe.left(), 100.0)
        self.assertAlmostEqual(safe.top(), 50.0)

    def test_blend_mode_id(self):
        self.assertEqual(COMPARE_BLEND, 4)
        self.assertNotEqual(COMPARE_BLEND, COMPARE_WIPE)


class TestOverlaySettings(unittest.TestCase):
    def test_overlay_settings_roundtrip(self):
        import os
        import shutil
        import tempfile
        from unittest.mock import patch

        from src.framecycler.core.settings import Settings
        from src.framecycler.core.system_memory import PlatformCacheLimits

        tmp = tempfile.mkdtemp()
        limits = PlatformCacheLimits(
            decode_max_gb=64.0,
            display_max_gb=16.0,
            coupled=False,
            combined_max_gb=80.0,
            system_memory_gb=64.0,
            vram_gb=16.0,
            platform_label="Mock",
        )
        try:
            with patch(
                "src.framecycler.core.settings.get_platform_cache_limits",
                return_value=limits,
            ):
                s = Settings(config_dir=tmp)
                s.overlay_mask_aspect = 2.39
                s.overlay_mask_opacity = 0.75
                s.overlay_action_safe = 0.05
                s.overlay_title_safe = 0.10
                s.save()
                s2 = Settings(config_dir=tmp)
                self.assertAlmostEqual(s2.overlay_mask_aspect, 2.39)
                self.assertAlmostEqual(s2.overlay_mask_opacity, 0.75)
                self.assertAlmostEqual(s2.overlay_action_safe, 0.05)
                self.assertAlmostEqual(s2.overlay_title_safe, 0.10)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
