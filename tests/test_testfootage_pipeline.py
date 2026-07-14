import os
import sys
import time
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt, QPointF
from PySide6.QtGui import QDragEnterEvent, QDropEvent
from PySide6.QtCore import QMimeData, QUrl

from src.framecycler.ui.main_window import MainWindow
from src.framecycler.core.media_source import decoder_frame_for_source

TEST_EXR = os.path.join(
    REPO_ROOT, ".temp/testFootage/KPO_012_0140_MP_v001.0993.exr"
)


@unittest.skipUnless(os.path.isfile(TEST_EXR), "testFootage EXR not present")
class TestTestFootagePipeline(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_load_testfootage_exr_updates_viewport_slot(self):
        window = MainWindow()
        self.addCleanup(window.close)
        window._add_media([TEST_EXR])
        self.app.processEvents()

        self.assertEqual(len(window.sources), 1)
        src = window.sources[0]
        self.assertEqual(src.decoder_start_frame, 993)
        self.assertEqual(src.frame_count, 4)

        decoder_frame = decoder_frame_for_source(window.sources, 0, 0)
        deadline = time.time() + 15.0
        while time.time() < deadline:
            self.app.processEvents()
            if src.cache.native_cache.has_frame(decoder_frame):
                break
            time.sleep(0.05)
        else:
            self.fail(f"frame {decoder_frame} was not cached")

        for _ in range(30):
            self.app.processEvents()
            time.sleep(0.05)
            slot = window.viewport.frame_slots[0]
            if slot.cached and slot.decoder_frame == decoder_frame:
                break
        else:
            window.seek_to_frame(0)
            self.app.processEvents()
            slot = window.viewport.frame_slots[0]

        self.assertTrue(slot.cached)
        self.assertEqual(slot.decoder_frame, 993)
        self.assertEqual(slot.width, 6054)
        self.assertEqual(slot.height, 3192)

    def test_scrub_testfootage_timeline_updates_decoder_frame(self):
        window = MainWindow()
        self.addCleanup(window.close)
        window._add_media([TEST_EXR])
        self.app.processEvents()

        deadline = time.time() + 15.0
        while time.time() < deadline:
            self.app.processEvents()
            if window.viewport.frame_slots and window.viewport.frame_slots[0].cached:
                break
            time.sleep(0.05)

        window._on_timeline_scrub(995)
        self.app.processEvents()
        time.sleep(0.3)
        self.app.processEvents()

        slot = window.viewport.frame_slots[0]
        self.assertEqual(window.current_frame, 995)
        self.assertEqual(slot.decoder_frame, 995)

    def test_drop_on_viewport_container_loads_testfootage(self):
        window = MainWindow()
        self.addCleanup(window.close)
        self.app.processEvents()

        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(TEST_EXR)])
        panel = window.viewport_panel
        enter = QDragEnterEvent(
            QPointF(200, 200).toPoint(),
            Qt.CopyAction,
            mime,
            Qt.LeftButton,
            Qt.NoModifier,
        )
        self.app.sendEvent(panel, enter)
        drop = QDropEvent(
            QPointF(200, 200),
            Qt.CopyAction,
            mime,
            Qt.LeftButton,
            Qt.NoModifier,
        )
        accepted = self.app.sendEvent(panel, drop)
        self.app.processEvents()

        self.assertTrue(enter.isAccepted())
        self.assertTrue(accepted)
        self.assertEqual(len(window.sources), 1)

    def test_drop_on_viewport_window_loads_testfootage(self):
        window = MainWindow()
        self.addCleanup(window.close)
        self.app.processEvents()

        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(TEST_EXR)])
        viewport_window = window.viewport.viewport_window
        enter = QDragEnterEvent(
            QPointF(200, 200).toPoint(),
            Qt.CopyAction,
            mime,
            Qt.LeftButton,
            Qt.NoModifier,
        )
        self.app.sendEvent(viewport_window, enter)
        drop = QDropEvent(
            QPointF(200, 200),
            Qt.CopyAction,
            mime,
            Qt.LeftButton,
            Qt.NoModifier,
        )
        accepted = self.app.sendEvent(viewport_window, drop)
        self.app.processEvents()

        self.assertTrue(enter.isAccepted())
        self.assertTrue(accepted)
        self.assertEqual(len(window.sources), 1)


if __name__ == "__main__":
    unittest.main()
