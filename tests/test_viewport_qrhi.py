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


if __name__ == "__main__":
    unittest.main()
