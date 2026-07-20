import unittest
from unittest.mock import patch
from PySide6.QtWidgets import QWidget
from src.framecycler.ui.viewport import Viewport, ViewportFrameSlot, COMPARE_SEQUENCE


class TestViewportQrhiIntegration(unittest.TestCase):
    def test_viewport_subclasses_qwidget(self):
        """Viewport must subclass QWidget to containerize the render QWindow."""
        self.assertTrue(issubclass(Viewport, QWidget))

    def test_viewport_uses_cpp_rhi_renderer(self):
        # Verify RhiRenderer is used
        from src.framecycler import framecycler_engine
        RhiRenderer = framecycler_engine.RhiRenderer
        viewport = Viewport.__new__(Viewport)
        viewport.native_renderer = RhiRenderer()
        self.assertIsInstance(viewport.native_renderer, RhiRenderer)

    def test_render_params_use_decoder_frame_not_local_frame(self):
        from PySide6.QtCore import QPoint
        from src.framecycler import framecycler_engine

        viewport = Viewport.__new__(Viewport)
        viewport.compare_mode = COMPARE_SEQUENCE
        viewport.sequence_index = 0
        viewport.wipe_pos = 0.5
        viewport.channel_mask = 0
        viewport.false_color_mode = 0
        viewport.zebra_lo = 0.02
        viewport.zebra_hi = 0.98
        viewport.zoom = 1.0
        viewport.zoom_mode = "fit"
        viewport.pan_offset = QPoint(0, 0)
        viewport.frame_slots = [
            ViewportFrameSlot(
                width=1920,
                height=1080,
                channels=4,
                local_frame=0,
                decoder_frame=993,
                upload_token=993,
                cached=True,
            )
        ]

        captured = {}

        class _FakeRenderer:
            def update_render_params(self, params):
                captured["params"] = params

            def sync_and_render(self):
                pass

            def clear_grading_uniforms(self):
                pass

        viewport.native_renderer = _FakeRenderer()

        def _width():
            return 1920

        def _height():
            return 1080

        viewport.width = _width
        viewport.height = _height
        with patch.object(QWidget, "update", return_value=None):
            viewport.update()

        self.assertIn("params", captured)
        self.assertEqual(captured["params"].slots[0].frame_index, 993)
        self.assertNotEqual(captured["params"].slots[0].frame_index, 0)

    def test_resize_event_updates_fit_scales(self):
        """Fit mode must refresh RenderParams on resize (not wait for playback)."""
        from PySide6.QtCore import QPoint, QSize
        from PySide6.QtGui import QResizeEvent
        from src.framecycler import framecycler_engine
        from src.framecycler.ui.viewport import COMPARE_TILE

        viewport = Viewport.__new__(Viewport)
        viewport.compare_mode = COMPARE_SEQUENCE
        viewport.sequence_index = 0
        viewport.wipe_pos = 0.5
        viewport.channel_mask = 0
        viewport.false_color_mode = 0
        viewport.zebra_lo = 0.02
        viewport.zebra_hi = 0.98
        viewport.zoom = 1.0
        viewport.zoom_mode = "fit"
        viewport.pan_offset = QPoint(0, 0)
        viewport.pixel_aspect_ratio = 1.0
        viewport.frame_slots = [
            ViewportFrameSlot(
                width=1920,
                height=1080,
                channels=4,
                local_frame=0,
                decoder_frame=10,
                upload_token=10,
                cached=True,
                pixel_aspect_ratio=1.0,
            )
        ]

        captured = []

        class _FakeRenderer:
            def update_render_params(self, params):
                captured.append((params.scale_x, params.scale_y))

            def sync_and_render(self):
                pass

            def clear_grading_uniforms(self):
                pass

        viewport.native_renderer = _FakeRenderer()
        viewport._size = (800, 600)

        def _width():
            return viewport._size[0]

        def _height():
            return viewport._size[1]

        viewport.width = _width
        viewport.height = _height

        with patch.object(QWidget, "update", return_value=None):
            viewport.update()
        self.assertEqual(len(captured), 1)
        scale_before = captured[0]

        viewport._size = (1600, 600)
        event = QResizeEvent(QSize(1600, 600), QSize(800, 600))
        with patch.object(QWidget, "resizeEvent", return_value=None):
            with patch.object(QWidget, "update", return_value=None):
                viewport.resizeEvent(event)

        self.assertEqual(len(captured), 2)
        scale_after = captured[1]
        self.assertNotEqual(scale_before, scale_after)
        # Wider widget → letterbox on X for 16:9 frame
        self.assertLess(scale_after[0], scale_before[0])

        # Tile mode also refreshes on resize
        captured.clear()
        viewport.compare_mode = COMPARE_TILE
        viewport._size = (400, 400)
        with patch.object(viewport, "_build_tile_draws", return_value=[]):
            with patch.object(QWidget, "resizeEvent", return_value=None):
                with patch.object(QWidget, "update", return_value=None):
                    viewport.resizeEvent(QResizeEvent(QSize(400, 400), QSize(1600, 600)))
        self.assertEqual(len(captured), 1)


if __name__ == "__main__":
    unittest.main()
