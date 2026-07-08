#!/usr/bin/env python3
"""Load testFootage EXR, render to display, capture window screenshot.

Usage (from repo root):
    python3 build.py
    python3 scripts/capture_testfootage_screenshot.py

Output: scripts/output/testfootage_window.png
"""
from __future__ import annotations

import os
import sys
import time

import numpy as np

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "src"))

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QTimer
from PySide6.QtGui import QImage

from framecycler.ui.main_window import MainWindow

TEST_EXR = os.path.join(REPO_ROOT, ".temp/testFootage/KPO_012_0140_MP_v001.0993.exr")
OUT_DIR = os.path.join(REPO_ROOT, "scripts/output")
OUT_PATH = os.path.join(OUT_DIR, "testfootage_window.png")


def log(msg: str) -> None:
    print(msg, flush=True)


def image_luminance_stats(image: QImage) -> dict:
    if image.isNull():
        return {"valid": False}
    rgb = image.convertToFormat(QImage.Format.Format_RGB888)
    w, h = rgb.width(), rgb.height()
    if w <= 0 or h <= 0:
        return {"valid": False}
    bpl = rgb.bytesPerLine()
    raw = rgb.bits().tobytes()
    arr = np.zeros((h, w, 3), dtype=np.uint8)
    for y in range(h):
        row = raw[y * bpl : y * bpl + w * 3]
        arr[y] = np.frombuffer(row, dtype=np.uint8).reshape(w, 3)
    lum = 0.2126 * arr[:, :, 0] + 0.7152 * arr[:, :, 1] + 0.0722 * arr[:, :, 2]
    return {
        "valid": True,
        "size": (w, h),
        "mean_lum": float(lum.mean()),
        "max_lum": float(lum.max()),
        "nonblack_ratio": float((lum > 8).mean()),
    }


def main() -> int:
    if not os.path.isfile(TEST_EXR):
        log(f"ERROR: missing {TEST_EXR}")
        return 1

    os.makedirs(OUT_DIR, exist_ok=True)

    app = QApplication(sys.argv)
    window = MainWindow()
    window.setWindowTitle("Framecycler Reboot")
    window.resize(1280, 800)
    window.move(80, 80)
    window.show()
    app.processEvents()
    time.sleep(0.3)

    log("Loading testFootage EXR...")
    window._add_media([TEST_EXR])

    exit_code = [0]

    def after_load():
        renderer = window.viewport.native_renderer

        for _ in range(60):
            window.viewport.update()
            app.processEvents()
            time.sleep(0.1)
            stats = renderer.get_debug_stats()
            if stats.get("frames_drawn", 0) > 0 and stats.get("cache_hits", 0) > 0:
                break

        stats = renderer.get_debug_stats()
        slot = window.viewport.frame_slots[0] if window.viewport.frame_slots else None
        log(f"debug_stats: {dict(stats)}")
        if slot:
            log(
                f"viewport slot: cached={slot.cached} decoder_frame={slot.decoder_frame} "
                f"size={slot.width}x{slot.height}"
            )
            if window.sources:
                df = slot.decoder_frame
                data = window.sources[0].cache.native_cache.get_frame_data(df)
                if data is not None:
                    sample = np.asarray(data, dtype=np.float16)
                    log(
                        f"cache sample: shape={sample.shape} "
                        f"mean={float(sample.mean()):.4f} max={float(sample.max()):.4f}"
                    )

        pixmap = app.primaryScreen().grabWindow(int(window.winId()))
        pixmap.save(OUT_PATH)
        log(f"Window screenshot saved: {OUT_PATH}")
        win_stats = image_luminance_stats(pixmap.toImage())
        log(f"window_luminance: {win_stats}")

        ok = (
            stats.get("frames_drawn", 0) > 0
            and win_stats.get("valid")
            and win_stats.get("mean_lum", 0) > 12
            and win_stats.get("nonblack_ratio", 0) > 0.05
        )
        if ok:
            log("PASS: EXR rendered to display (non-black pixels confirmed)")
        else:
            log("FAIL: window still black or renderer did not draw")

        exit_code[0] = 0 if ok else 1
        window.close()
        app.quit()

    QTimer.singleShot(500, after_load)
    app.exec()
    return exit_code[0]


if __name__ == "__main__":
    sys.exit(main() or 0)
