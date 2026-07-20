"""Viewer SDR/EDR present API and View-menu wiring (HDR / EDR output #11)."""
from __future__ import annotations

import os
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from src.framecycler.core.settings import Settings
from src.framecycler.ui.main_window import MainWindow


class TestViewerEdr(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_settings_prefer_edr_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(config_dir=tmp)
            self.assertFalse(settings.prefer_edr)
            settings.prefer_edr = True
            settings.save()
            loaded = Settings(config_dir=tmp)
            self.assertTrue(loaded.prefer_edr)

    def test_view_menu_viewer_actions_exist(self):
        window = MainWindow()
        try:
            self.assertTrue(hasattr(window, "act_viewer_sdr"))
            self.assertTrue(hasattr(window, "act_viewer_edr"))
            self.assertTrue(window.act_viewer_sdr.isCheckable())
            self.assertTrue(window.act_viewer_edr.isCheckable())
            self.assertTrue(window.act_viewer_sdr.isChecked())
            # Offscreen / Null: EDR unsupported → leave SDR (menu stays checked SDR).
            window._set_viewer_output_mode(1)
            self.assertEqual(window.viewport.viewer_output_mode(), 0)
            self.assertEqual(window.viewport.actual_viewer_output_mode(), 0)
            self.assertTrue(window.act_viewer_sdr.isChecked())
            self.assertFalse(window.act_viewer_edr.isEnabled())
        finally:
            window.close()


if __name__ == "__main__":
    unittest.main()
