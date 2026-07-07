import unittest

from PySide6.QtWidgets import QRhiWidget

from src.framecycler.render.rhi_viewport_renderer import RhiViewportRenderer
from src.framecycler.ui.viewport import Viewport


class TestViewportQrhiIntegration(unittest.TestCase):
    def test_viewport_does_not_override_qrhi_paint_event(self):
        """Viewport must not replace QRhiWidget.paintEvent; that hook drives render()."""
        self.assertNotIn("paintEvent", Viewport.__dict__)
        self.assertIs(Viewport.paintEvent, QRhiWidget.paintEvent)

    def test_viewport_uses_python_rhi_renderer(self):
        viewport = Viewport.__new__(Viewport)
        viewport.native_renderer = RhiViewportRenderer()
        self.assertIsInstance(viewport.native_renderer, RhiViewportRenderer)


if __name__ == "__main__":
    unittest.main()
