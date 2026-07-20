"""One-frame GPU present smoke (deployment readiness #10).

Skips when FRAMECYCLER_REQUIRE_GPU is unset/0, or when no display/GPU backend
is available. CI sets FRAMECYCLER_REQUIRE_GPU=1 on macOS/Windows runners in a
dedicated step (not the offscreen unit-test job).
"""

from __future__ import annotations

import os
import sys
import time
import unittest


def _gpu_required() -> bool:
    return os.environ.get("FRAMECYCLER_REQUIRE_GPU", "0") == "1"


class TestGpuPresentSmoke(unittest.TestCase):
    def test_one_frame_present_non_null(self):
        if not _gpu_required():
            self.skipTest("FRAMECYCLER_REQUIRE_GPU not set")

        # Only clear offscreen when this dedicated smoke is requested.
        os.environ.pop("QT_QPA_PLATFORM", None)

        from PySide6.QtGui import QGuiApplication
        from PySide6.QtWidgets import QApplication
        from PySide6.QtGui import QWindow

        from src.framecycler import framecycler_engine

        app = QApplication.instance() or QApplication(sys.argv)

        if QGuiApplication.platformName() in ("offscreen", "minimal"):
            self.skipTest(f"platform={QGuiApplication.platformName()} has no GPU present")

        window = QWindow()
        window.resize(64, 64)
        window.setTitle("framecycler-gpu-smoke")
        window.show()
        app.processEvents()

        renderer = framecycler_engine.RhiRenderer()
        try:
            ok = renderer.initialize(int(window.winId()))
            if not ok:
                self.skipTest("RhiRenderer.initialize failed (no GPU?)")
            renderer.set_exposed(True)
            renderer.request_redraw()
            renderer.sync_and_render()

            deadline = time.time() + 5.0
            stats = {}
            while time.time() < deadline:
                app.processEvents()
                stats = renderer.get_debug_stats()
                if int(stats.get("begin_frame_ok", 0)) > 0:
                    break
                time.sleep(0.05)

            if renderer.is_fallback_null_backend():
                self.skipTest("Null RHI backend (GPU unavailable on runner)")

            self.assertGreater(
                int(stats.get("begin_frame_ok", 0)),
                0,
                f"expected a successful beginFrame; stats={stats}",
            )
        finally:
            renderer.shutdown()
            window.close()
            app.processEvents()


if __name__ == "__main__":
    unittest.main()
