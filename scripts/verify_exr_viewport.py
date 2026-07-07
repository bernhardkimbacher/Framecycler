#!/usr/bin/env python3
"""Verify EXR load + QRhi viewport render path using local test footage.

Requires a GUI session (QRhiWidget does not render headless). Exits 0 when
initialize/render run and the grabbed viewport center is non-black.

Usage:
    source .venv/bin/activate
    python scripts/verify_exr_viewport.py
    python scripts/verify_exr_viewport.py path/to/shot.exr
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

DEFAULT_FOOTAGE = REPO_ROOT / ".temp" / "testFootage" / "KPO_012_0140_MP_v001.0993.exr"


def main() -> int:
    if os.environ.get("QT_QPA_PLATFORM") == "offscreen":
        print("SKIP: QT_QPA_PLATFORM=offscreen (QRhiWidget needs a real display)")
        return 0

    footage = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_FOOTAGE
    if not footage.is_file():
        print(f"FAIL: test footage not found: {footage}")
        return 1

    import numpy as np
    from PySide6.QtCore import QTimer
    from PySide6.QtGui import QImage
    from PySide6.QtWidgets import QApplication, QMainWindow

    from framecycler.color.ocio_manager import OCIOManager
    from framecycler.core.cache import CacheEngine
    from framecycler.core.settings import Settings
    from framecycler.decoders.exr_decoder import EXRDecoder
    from framecycler.ui.viewport import ViewportContainer

    decoder = EXRDecoder(str(footage))
    frame_idx = decoder.start_frame
    engine = CacheEngine(decoder, Settings())
    engine._read_and_cache_worker(frame_idx)
    hit = engine.get_frame(frame_idx)
    if hit is None:
        print("FAIL: cache miss after synchronous decode")
        return 1

    app = QApplication(sys.argv)
    ocio = OCIOManager()
    calls = {"initialize": 0, "render": 0}

    class Window(QMainWindow):
        def __init__(self):
            super().__init__()
            self.viewport_panel = ViewportContainer(ocio)
            self.viewport = self.viewport_panel.viewport
            self.setCentralWidget(self.viewport_panel)
            self.resize(1280, 720)

    window = Window()
    viewport = window.viewport
    orig_initialize = viewport.initialize
    orig_render = viewport.render

    def initialize(cb):
        calls["initialize"] += 1
        orig_initialize(cb)

    def render(cb):
        calls["render"] += 1
        orig_render(cb)

    viewport.initialize = initialize
    viewport.render = render

    state = {"done": False}

    def finish():
        viewport.update_ocio_pipeline()
        viewport.set_frame(
            0,
            hit["data"],
            hit["channels"],
            local_frame=frame_idx,
            timecode=hit["timecode"],
            fps=decoder.get_metadata()["fps"],
            upload_buffer=hit.get("upload_buffer"),
            is_primary=True,
        )
        for _ in range(80):
            app.processEvents()

        if calls["initialize"] < 1 or calls["render"] < 1:
            print(
                "FAIL: QRhiWidget never invoked initialize/render "
                f"(initialize={calls['initialize']}, render={calls['render']}). "
                "Viewport.paintEvent must delegate to QRhiWidget.paintEvent()."
            )
            app.quit()
            return

        img = viewport.grab().toImage().convertToFormat(QImage.Format.Format_RGBA8888)
        max_rgb = 0
        for y in range(img.height() // 4, 3 * img.height() // 4, max(1, img.height() // 16)):
            for x in range(img.width() // 4, 3 * img.width() // 4, max(1, img.width() // 16)):
                color = img.pixelColor(x, y)
                max_rgb = max(max_rgb, color.red(), color.green(), color.blue())

        data = hit["data"]
        print(
            f"frame {frame_idx}: {data.shape[1]}x{data.shape[0]} "
            f"decoded max={float(np.max(data[..., :3])):.3f}"
        )
        print(f"initialize={calls['initialize']} render={calls['render']} grab_max_rgb={max_rgb}")

        if max_rgb < 8:
            print("FAIL: viewport grab is still black")
            state["exit_code"] = 1
        else:
            print("OK: viewport shows non-black EXR image")
            state["exit_code"] = 0

        state["done"] = True
        app.quit()

    window.show()
    QTimer.singleShot(400, finish)
    app.exec()
    engine.close()
    return int(state.get("exit_code", 1 if not state["done"] else 0))


if __name__ == "__main__":
    raise SystemExit(main())
