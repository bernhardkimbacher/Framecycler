#!/usr/bin/env python3
"""End-to-end verification of EXR load → cache → viewport → C++ renderer pipeline.

Uses real footage from .temp/testFootage. Run from repo root:
    python3 scripts/verify_testfootage_pipeline.py
"""
import os
import sys
import time
import threading

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "src"))

# Use offscreen unless DISPLAY needed — on macOS use default for Metal
if os.environ.get("FORCE_OFFSCREEN"):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QTimer

from framecycler.ui.main_window import MainWindow
from framecycler.core.media_source import decoder_frame_for_source

TEST_EXR = os.path.join(
    REPO_ROOT, ".temp/testFootage/KPO_012_0140_MP_v001.0993.exr"
)


def log(msg: str) -> None:
    print(msg, flush=True)


def main() -> int:
    if not os.path.isfile(TEST_EXR):
        log(f"ERROR: test file not found: {TEST_EXR}")
        return 1

    app = QApplication(sys.argv)
    window = MainWindow()
    window.resize(1200, 800)
    window.show()
    app.processEvents()
    time.sleep(0.2)
    app.processEvents()

    renderer = window.viewport.native_renderer
    exposed = getattr(renderer, "set_exposed", None)
    log(f"Window visible: {window.isVisible()}")
    log(f"Viewport size: {window.viewport.width()}x{window.viewport.height()}")

    log("\n=== Loading test EXR ===")
    window._add_media([TEST_EXR])
    app.processEvents()

    log(f"sources: {len(window.sources)}")
    if not window.sources:
        log("FAIL: no sources loaded")
        return 2

    src = window.sources[0]
    log(f"display_name: {src.display_name}")
    log(f"frame_count: {src.frame_count}, decoder_start: {src.decoder_start_frame}")
    log(f"resolution: {src.width}x{src.height}")

    decoder_frame = decoder_frame_for_source(window.sources, 0, window.current_frame)
    log(f"current_frame (global): {window.current_frame}")
    log(f"decoder_frame at playhead: {decoder_frame}")

    # Wait for async decode of playhead frame
    log("\n=== Waiting for cache (up to 15s) ===")
    deadline = time.time() + 15.0
    while time.time() < deadline:
        app.processEvents()
        if src.cache.native_cache.has_frame(decoder_frame):
            break
        time.sleep(0.1)
    else:
        log(f"FAIL: frame {decoder_frame} never cached")
        return 3

    log(f"has_frame({decoder_frame}): True")
    data = src.cache.native_cache.get_frame_data(decoder_frame)
    log(f"cache pixel shape: {data.shape if data is not None else None}")

    # Wait for frame-ready signal to update viewport
    for _ in range(30):
        app.processEvents()
        time.sleep(0.1)
        slot = window.viewport.frame_slots[0] if window.viewport.frame_slots else None
        if slot and slot.cached and slot.decoder_frame == decoder_frame:
            break
    else:
        log("WARN: viewport slot not updated via callback; forcing seek")
        window.seek_to_frame(window.current_frame)
        app.processEvents()

    slot = window.viewport.frame_slots[0]
    log(f"\n=== Viewport slot (initial) ===")
    log(f"  cached={slot.cached} decoder_frame={slot.decoder_frame} local_frame={slot.local_frame}")
    log(f"  size={slot.width}x{slot.height} channels={slot.channels}")

    if not slot.cached or slot.decoder_frame != decoder_frame:
        log("FAIL: initial viewport slot not ready")
        return 4

    log("\n=== Scrub to frame 2 (global) ===")
    window._on_timeline_scrub(2)
    app.processEvents()
    time.sleep(0.5)
    app.processEvents()
    slot2 = window.viewport.frame_slots[0]
    expected_df = decoder_frame_for_source(window.sources, 0, 2)
    log(f"  current_frame={window.current_frame} decoder_frame={slot2.decoder_frame} expected={expected_df}")
    log(f"  cached={slot2.cached}")

    log("\n=== Renderer state ===")
    log(f"  ocio_pipeline_ready: {window.viewport._ocio_pipeline_ready}")
    log(f"  request_redraw available: {hasattr(renderer, 'request_redraw')}")

    failures = []
    if slot2.decoder_frame != expected_df:
        failures.append(f"scrub decoder_frame {slot2.decoder_frame} != {expected_df}")
    if not slot2.cached:
        failures.append("scrub slot not cached")

    if failures:
        log("\nFAILURES:")
        for f in failures:
            log(f"  - {f}")
        return 10

    log("\nPASS: Python-side pipeline (load, cache, viewport slots, scrub) OK")
    log("If viewer is still black, issue is in C++ RhiRenderer draw path (shaders/exposed/swapchain).")
    QTimer.singleShot(100, window.close)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main() or 0)
