"""Smoke tests for scope downsample + accumulators."""

from __future__ import annotations

import inspect
import unittest

import numpy as np

from src.framecycler.ui.scopes.analysis import (
    WAVEFORM_BINS,
    ScopeType,
    accumulate_cie,
    accumulate_histogram,
    accumulate_parade,
    accumulate_vectorscope,
    accumulate_waveform,
    analyze,
    analyze_many,
    downsample_from_cache,
    downsample_rgb,
    has_native_scopes,
    luma_rec709,
    rgb_to_xy,
)


def _gradient(h=64, w=128) -> np.ndarray:
    x = np.linspace(0, 1, w, dtype=np.float32)
    y = np.linspace(0, 1, h, dtype=np.float32)
    xx, yy = np.meshgrid(x, y)
    return np.stack([xx, yy, 0.5 * np.ones_like(xx)], axis=-1)


class TestScopeAnalysis(unittest.TestCase):
    def test_downsample_reduces_width(self):
        rgb = _gradient(h=100, w=2000)
        out = downsample_rgb(rgb, max_width=512)
        self.assertEqual(out.shape[0], 100)
        self.assertLessEqual(out.shape[1], 512)
        self.assertEqual(out.shape[2], 3)
        self.assertEqual(out.dtype, np.float32)

    def test_downsample_noop_when_narrow(self):
        rgb = _gradient(h=32, w=200)
        out = downsample_rgb(rgb, max_width=512)
        self.assertEqual(out.shape, (32, 200, 3))

    def test_downsample_rejects_bad_shape(self):
        with self.assertRaises(ValueError):
            downsample_rgb(np.zeros((10, 10), dtype=np.float32))

    def test_waveform_shape_and_energy(self):
        rgb = _gradient()
        wf = accumulate_waveform(rgb)
        self.assertEqual(wf.shape, (WAVEFORM_BINS, rgb.shape[1]))
        self.assertAlmostEqual(float(wf.sum()), float(rgb.shape[0] * rgb.shape[1]))
        left = int(wf[:, 0].argmax())
        right = int(wf[:, -1].argmax())
        self.assertLess(left, right)

    def test_parade_is_three_waveforms(self):
        rgb = _gradient()
        parade = accumulate_parade(rgb)
        self.assertEqual(parade.shape, (WAVEFORM_BINS, rgb.shape[1] * 3))

    def test_histogram_channels(self):
        rgb = np.zeros((16, 16, 3), dtype=np.float32)
        rgb[..., 0] = 0.25
        rgb[..., 1] = 0.5
        rgb[..., 2] = 0.75
        hist = accumulate_histogram(rgb, bins=256)
        self.assertEqual(hist.shape, (3, 256))
        self.assertAlmostEqual(hist[0].argmax(), 0.25 * 255, delta=1)
        self.assertAlmostEqual(hist[1].argmax(), 0.5 * 255, delta=1)
        self.assertAlmostEqual(hist[2].argmax(), 0.75 * 255, delta=1)

    def test_vectorscope_peaks_near_center_for_grey(self):
        rgb = np.full((32, 32, 3), 0.5, dtype=np.float32)
        vs = accumulate_vectorscope(rgb, size=128)
        self.assertEqual(vs.shape, (128, 128))
        yi, xi = np.unravel_index(int(vs.argmax()), vs.shape)
        self.assertLess(abs(xi - 63.5), 4)
        self.assertLess(abs(yi - 63.5), 4)

    def test_cie_white_near_d65(self):
        rgb = np.ones((8, 8, 3), dtype=np.float32)
        xx, yy = rgb_to_xy(rgb)
        self.assertAlmostEqual(float(xx.mean()), 0.3127, delta=0.02)
        self.assertAlmostEqual(float(yy.mean()), 0.3290, delta=0.02)
        dens = accumulate_cie(rgb, grid=128)
        self.assertGreater(float(dens.sum()), 0.0)

    def test_analyze_dispatch(self):
        rgb = _gradient(h=16, w=32)
        for st in ScopeType:
            out = analyze(rgb, st)
            self.assertIsNotNone(out)
            self.assertGreater(out.size, 0)

    def test_luma_rec709_weights(self):
        rgb = np.array([[[1.0, 0.0, 0.0]]], dtype=np.float32)
        self.assertAlmostEqual(float(luma_rec709(rgb)[0, 0]), 0.2126)


@unittest.skipUnless(has_native_scopes(), "native scope analyzer not built")
class TestNativeScopeParity(unittest.TestCase):
    def test_cpp_vs_python_accumulators(self):
        rgb = _gradient(h=48, w=96)
        types = (
            ScopeType.WAVEFORM,
            ScopeType.PARADE,
            ScopeType.VECTORSCOPE,
            ScopeType.HISTOGRAM,
            ScopeType.CIE,
        )
        py = [analyze(rgb, st) for st in types]
        cpp = analyze_many(rgb, types)
        for st, a, b in zip(types, py, cpp):
            self.assertEqual(a.shape, b.shape, st)
            self.assertEqual(float(a.sum()), float(b.sum()), st)
            # Peak location should match (scatter is deterministic).
            self.assertEqual(int(a.argmax()), int(b.argmax()), st)

    def test_downsample_from_cache(self):
        from src.framecycler import framecycler_engine as eng

        cache = eng.CacheManager(0.05)
        h, w, c = 40, 200, 4
        frame = np.zeros((h, w, c), dtype=np.float16)
        frame[..., 0] = np.linspace(0, 1, w, dtype=np.float32)
        frame[..., 1] = 0.25
        frame[..., 2] = 0.5
        frame[..., 3] = 1.0
        cache.write_frame(7, w, h, c, frame)
        small = downsample_from_cache(cache, 7, max_width=50)
        self.assertIsNotNone(small)
        self.assertEqual(small.shape[0], h)
        self.assertLessEqual(small.shape[1], 50)
        self.assertEqual(small.shape[2], 3)


class TestScopesPlayheadDecoupling(unittest.TestCase):
    def test_seek_light_playing_branch_does_not_notify_scopes(self):
        from src.framecycler.ui.main_window import MainWindow

        src = inspect.getsource(MainWindow._seek_light)
        # Split on the playing early-return block.
        playing_idx = src.find("if self.playing:")
        self.assertGreater(playing_idx, 0)
        early_return = src.find("return", playing_idx)
        self.assertGreater(early_return, playing_idx)
        playing_block = src[playing_idx:early_return]
        self.assertNotIn(
            "_notify_scopes_frame_changed",
            playing_block,
            "playing seek must not notify scopes every frame",
        )

    def test_notify_skips_schedule_while_playing(self):
        import os

        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication

        app = QApplication.instance() or QApplication([])
        from src.framecycler.ui.scopes.scopes_panel import ScopesPanel

        panel = ScopesPanel()
        started = {"n": 0}
        orig = panel._start_job

        def wrapped():
            started["n"] += 1
            # Do not actually start a pool job in this unit test.
            panel._inflight = False
            panel._dirty = False

        panel._start_job = wrapped  # type: ignore[method-assign]
        panel.set_providers(
            frame_provider=lambda: (None, None),
            playing_provider=lambda: True,
        )
        panel._active = True
        started["n"] = 0
        panel.notify_frame_changed()
        self.assertEqual(started["n"], 0)
        self.assertTrue(panel._dirty)
        # Restore
        panel._start_job = orig  # type: ignore[method-assign]
        del app


if __name__ == "__main__":
    unittest.main()
