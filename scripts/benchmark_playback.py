#!/usr/bin/env python3
"""Benchmark playback with GPU display cache verification."""
from __future__ import annotations

import glob
import os
import statistics
import sys
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "src"))

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QTimer

from framecycler.ui.main_window import MainWindow
from framecycler.core.media_source import decoder_frame_for_source

TEST_DIR = os.path.join(REPO_ROOT, ".temp/testFootage")


def log(msg: str) -> None:
    print(msg, flush=True)


def main() -> int:
    exrs = sorted(glob.glob(os.path.join(TEST_DIR, "*.exr")))
    if not exrs:
        log(f"ERROR: no EXRs in {TEST_DIR}")
        return 1

    app = QApplication(sys.argv)
    window = MainWindow()
    window.settings.display_cache_limit_gb = 8.0
    window._apply_renderer_cache_settings()
    window.resize(1280, 800)
    window.show()
    app.processEvents()
    time.sleep(0.2)

    window._add_media(exrs)
    app.processEvents()

    src = window.sources[0]
    frame_count = min(4, src.frame_count)
    renderer = window.viewport.native_renderer

    for local_frame in range(frame_count):
        decoder_frame = decoder_frame_for_source(window.sources, 0, local_frame)
        deadline = time.time() + 30.0
        while time.time() < deadline:
            app.processEvents()
            if src.cache.native_cache.has_frame(decoder_frame):
                break
            time.sleep(0.05)

    renderer.clear_display_cache()
    app.processEvents()

    cycle1_uploads = []
    cycle2_uploads = []
    cycle1_misses = 0
    cycle2_hits_start = renderer.get_display_cache_stats().get("hits", 0)

    for cycle in range(2):
        for local_frame in range(frame_count):
            window.seek_to_frame(local_frame)
            app.processEvents()
            stats = renderer.get_debug_stats()
            cache_stats = renderer.get_display_cache_stats()
            if cycle == 0:
                cycle1_uploads.append(stats.get("last_upload_bytes", 0))
                cycle1_misses = cache_stats.get("misses", 0)
            else:
                cycle2_uploads.append(stats.get("last_upload_bytes", 0))

    cache_stats = renderer.get_display_cache_stats()
    cycle2_hits = cache_stats.get("hits", 0) - cycle2_hits_start

    log(f"cycle1 mean upload bytes: {statistics.mean(cycle1_uploads):.0f}")
    log(f"cycle1 gpu cache misses: {cycle1_misses}")
    log(f"cycle2 mean upload bytes: {statistics.mean(cycle2_uploads):.0f}")
    log(f"cycle2 gpu cache hits: {cycle2_hits}")
    log(f"display cache stats: {cache_stats}")

    ok = (
        cycle1_misses >= frame_count
        and cycle2_hits >= frame_count
        and statistics.mean(cycle2_uploads) == 0
    )
    log("PASS" if ok else "FAIL")
    window.close()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
