import unittest
import tempfile
import threading
import time
from pathlib import Path

import numpy as np

from src.framecycler import framecycler_engine
from tests.oiio_fixtures import (
    require_oiio,
    write_float_exr,
    write_layered_exr,
    write_mipmapped_float_exr,
    write_scattered_channel_exr,
    write_tiled_float_exr,
    write_uint16_dpx,
)


class TestCppCache(unittest.TestCase):
    def test_cache_manager_basic(self):
        # 1. Instantiate C++ CacheManager (RAM Limit = 0.05 GB to trigger eviction quickly)
        cache = framecycler_engine.CacheManager(0.05)
        self.assertFalse(cache.has_frame(0))

        # 2. Write a float16 frame: width=1920, height=1080, channels=4 (~16 MB)
        w, h, c = 1920, 1080, 4
        frame_data = np.ones((h, w, c), dtype=np.float16) * np.float16(0.5)

        cache.write_frame(10, w, h, c, frame_data)

        # 3. Check cached state
        self.assertTrue(cache.has_frame(10))
        self.assertEqual(cache.get_cached_frames(), [10])

        # 4. Fetch array back and check content (copied out of the cache)
        retrieved_data = cache.get_frame_data(10)
        self.assertIsNotNone(retrieved_data)
        self.assertEqual(retrieved_data.shape, (h, w, c))
        self.assertTrue(np.allclose(retrieved_data.astype(np.float32), 0.5))

    def test_eviction(self):
        # Limit to 0.02 GB so it can only fit one 1920x1080 float16 RGBA frame (~16 MB)
        cache = framecycler_engine.CacheManager(0.02)

        w, h, c = 1920, 1080, 4
        frame1 = np.ones((h, w, c), dtype=np.float16) * np.float16(0.1)
        frame2 = np.ones((h, w, c), dtype=np.float16) * np.float16(0.2)

        cache.set_playhead(0, 1, 0, 100)

        cache.write_frame(0, w, h, c, frame1)
        self.assertTrue(cache.has_frame(0))

        cache.write_frame(1, w, h, c, frame2)

        self.assertTrue(cache.has_frame(1))
        self.assertFalse(cache.has_frame(0))

    def test_missing_frame_fallbacks(self):
        cache = framecycler_engine.CacheManager(0.1)

        # 1. Test Flat Gray fallback
        # Non-existent path should fallback to Flat Gray by default
        cache.decode_and_cache_frame(100, "non_existent_file.exr", 1.0, "", "Flat Gray")
        self.assertTrue(cache.has_frame(100))
        data_gray = cache.get_frame_data(100)
        self.assertIsNotNone(data_gray)
        # Check solid gray value (0.05)
        self.assertTrue(np.allclose(data_gray[..., 0].astype(np.float32), 0.05, atol=0.01))

        # 2. Test Red X fallback
        cache.decode_and_cache_frame(101, "non_existent_file.exr", 1.0, "", "Red X")
        self.assertTrue(cache.has_frame(101))
        data_red_x = cache.get_frame_data(101)
        self.assertIsNotNone(data_red_x)

        # Diagonals should be bright red [1.0, 0.0, 0.0, 1.0]
        h, w, c = data_red_x.shape
        # Center pixel (h//2, w//2) is on the diagonal intersection, should be red
        center_pixel = data_red_x[h // 2, w // 2].astype(np.float32)
        self.assertAlmostEqual(center_pixel[0], 1.0, places=2)
        self.assertAlmostEqual(center_pixel[1], 0.0, places=2)
        self.assertAlmostEqual(center_pixel[2], 0.0, places=2)

        # Corner pixel (0, 0) is also on the main diagonal
        corner_pixel = data_red_x[0, 0].astype(np.float32)
        self.assertAlmostEqual(corner_pixel[0], 1.0, places=2)

        # Pixel at (0, w//2) should be background (dark gray 0.1)
        bg_pixel = data_red_x[0, w // 2].astype(np.float32)
        self.assertAlmostEqual(bg_pixel[0], 0.1, places=2)
        self.assertAlmostEqual(bg_pixel[1], 0.1, places=2)
        self.assertAlmostEqual(bg_pixel[2], 0.1, places=2)

    def test_get_frame_data_safe_under_concurrent_writes(self):
        """Regression: views must not dangle when write_frame grows the slot table."""
        cache = framecycler_engine.CacheManager(0.5)
        w, h, c = 64, 32, 4
        stop = threading.Event()
        errors = []

        def writer():
            frame = 0
            while not stop.is_set() and frame < 200:
                data = np.full((h, w, c), np.float16(frame % 17), dtype=np.float16)
                try:
                    cache.write_frame(frame, w, h, c, data)
                except Exception as exc:
                    errors.append(exc)
                    return
                frame += 1

        def reader():
            while not stop.is_set():
                try:
                    view = cache.get_frame_data(0)
                    if view is not None:
                        _ = float(view[0, 0, 0])
                        _ = view.copy()
                except Exception as exc:
                    errors.append(exc)
                    return
                time.sleep(0.0005)

        threads = [threading.Thread(target=writer), threading.Thread(target=reader)]
        for t in threads:
            t.start()
        time.sleep(0.3)
        stop.set()
        for t in threads:
            t.join(timeout=2.0)
        self.assertEqual(errors, [])
        self.assertTrue(cache.has_frame(0))

    def test_acquire_commit_safe_under_concurrent_readers(self):
        """acquire/commit must not expose half-written slots to readers."""
        cache = framecycler_engine.CacheManager(0.5)
        w, h, c = 64, 32, 4
        stop = threading.Event()
        errors = []

        def writer():
            frame = 0
            while not stop.is_set() and frame < 100:
                try:
                    if not cache.try_claim_decode(frame):
                        frame += 1
                        continue
                    view = cache.acquire_write_slot(frame, w, h, c)
                    if view is None:
                        cache.release_decode_claim(frame)
                        errors.append(RuntimeError("acquire_write_slot returned None"))
                        return
                    view[...] = np.float16(0.42)
                    # Brief window where active=false — readers must not see this.
                    time.sleep(0.0002)
                    cache.commit_write_slot(frame, True)
                    cache.release_decode_claim(frame)
                except Exception as exc:
                    errors.append(exc)
                    return
                frame += 1

        def reader():
            while not stop.is_set():
                try:
                    for f in range(100):
                        data = cache.get_frame_data(f)
                        if data is not None:
                            vals = data.astype(np.float32)
                            if not np.allclose(vals, 0.42, atol=0.02):
                                errors.append(
                                    AssertionError(f"saw incomplete frame {f}: {vals[0,0]}")
                                )
                                return
                except Exception as exc:
                    errors.append(exc)
                    return
                time.sleep(0.0005)

        threads = [threading.Thread(target=writer), threading.Thread(target=reader)]
        for t in threads:
            t.start()
        time.sleep(0.4)
        stop.set()
        for t in threads:
            t.join(timeout=2.0)
        self.assertEqual(errors, [])

    def test_clear_during_inflight_decode_keeps_claim(self):
        """clear() must not abort mid-decode acquires; stale commits stay hidden."""
        cache = framecycler_engine.CacheManager(0.5)
        w, h, c = 32, 16, 4
        frame = 7

        self.assertTrue(cache.try_claim_decode(frame))
        view = cache.acquire_write_slot(frame, w, h, c)
        self.assertIsNotNone(view)
        view[...] = np.float16(0.5)

        # Mid-decode clear (layer/resolution change): claim must survive so
        # acquire still works, but commit must not surface the stale pixels.
        cache.clear()
        self.assertTrue(cache.is_decode_claimed(frame))
        self.assertFalse(cache.has_frame(frame))

        # Pointer from before clear is still valid; commit after clear is stale.
        cache.commit_write_slot(frame, True)
        cache.release_decode_claim(frame)
        self.assertFalse(cache.has_frame(frame))

        # A fresh decode after clear must still be able to acquire.
        self.assertTrue(cache.try_claim_decode(frame))
        view2 = cache.acquire_write_slot(frame, w, h, c)
        self.assertIsNotNone(view2, "acquire_write_slot returned None after clear()")
        view2[...] = np.float16(0.75)
        cache.commit_write_slot(frame, True)
        cache.release_decode_claim(frame)
        self.assertTrue(cache.has_frame(frame))
        data = cache.get_frame_data(frame)
        self.assertIsNotNone(data)
        self.assertTrue(np.allclose(data.astype(np.float32), 0.75, atol=0.02))

    def test_unmapped_slot_eviction_does_not_alias_frame_zero(self):
        """Failed-commit leftover must not spuriously erase frame 0 on reuse."""
        # Tiny budget: one ~8 KB frame fits; force eviction after a failed commit.
        cache = framecycler_engine.CacheManager(0.00002)  # ~20 KB
        w, h, c = 32, 16, 4
        pixels = np.full((h, w, c), np.float16(0.25), dtype=np.float16)

        cache.write_frame(0, w, h, c, pixels)
        self.assertTrue(cache.has_frame(0))

        # Failed acquire/commit leaves an unmapped inactive slot with capacity.
        self.assertTrue(cache.try_claim_decode(1))
        view = cache.acquire_write_slot(1, w, h, c)
        self.assertIsNotNone(view)
        cache.commit_write_slot(1, False)
        cache.release_decode_claim(1)
        self.assertFalse(cache.has_frame(1))

        # Fill past budget so the unmapped slot is reused for a new frame.
        for frame in range(2, 6):
            cache.write_frame(frame, w, h, c, pixels)

        self.assertTrue(
            cache.has_frame(0),
            "frame 0 was spuriously unmapped by unmapped-slot reuse",
        )

    def test_allocated_bytes_after_failed_commit_and_clear(self):
        """clear() must free non-inflight capacity; failed commits stay reusable."""
        cache = framecycler_engine.CacheManager(0.5)
        w, h, c = 64, 32, 4
        frame_bytes = w * h * c * 2  # uint16

        cache.write_frame(0, w, h, c, np.zeros((h, w, c), dtype=np.float16))
        self.assertEqual(cache.allocated_bytes(), frame_bytes)

        self.assertTrue(cache.try_claim_decode(1))
        view = cache.acquire_write_slot(1, w, h, c)
        self.assertIsNotNone(view)
        cache.commit_write_slot(1, False)
        cache.release_decode_claim(1)
        # Failed commit keeps capacity counted until clear/reuse.
        self.assertGreaterEqual(cache.allocated_bytes(), frame_bytes)

        cache.clear()
        self.assertEqual(cache.allocated_bytes(), 0)
        self.assertFalse(cache.has_frame(0))
        self.assertEqual(cache.get_cached_frames(), [])


class TestNativeDecodePath(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        require_oiio()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="framecycler_native_decode_")
        self.tmp_path = Path(self.tmp.name)
        self.cache = framecycler_engine.CacheManager(0.5)

    def tearDown(self):
        self.tmp.cleanup()

    def test_decode_rgb_exr_expands_alpha(self):
        path = self.tmp_path / "plate.1001.exr"
        write_float_exr(path, width=48, height=24, value=0.33)
        self.assertTrue(self.cache.decode_and_cache_frame(1001, str(path), 1.0))
        data = self.cache.get_frame_data(1001)
        self.assertIsNotNone(data)
        self.assertEqual(data.shape, (24, 48, 4))
        self.assertTrue(np.allclose(data[..., :3].astype(np.float32), 0.33, atol=0.01))
        self.assertTrue(np.allclose(data[..., 3].astype(np.float32), 1.0, atol=0.01))

    def test_decode_layered_exr(self):
        path = self.tmp_path / "multi.1001.exr"
        write_layered_exr(path, width=32, height=16)
        self.assertTrue(self.cache.decode_and_cache_frame(1, str(path), 1.0, "beauty"))
        beauty = self.cache.get_frame_data(1)
        self.assertTrue(np.allclose(beauty[..., :3].astype(np.float32), 0.25, atol=0.01))

        self.assertTrue(self.cache.decode_and_cache_frame(2, str(path), 1.0, "depth"))
        depth = self.cache.get_frame_data(2)
        self.assertTrue(np.allclose(depth[..., :3].astype(np.float32), 0.75, atol=0.01))

    def test_decode_scattered_channels(self):
        path = self.tmp_path / "scatter.1001.exr"
        write_scattered_channel_exr(path, width=16, height=8)
        self.assertTrue(self.cache.decode_and_cache_frame(1, str(path), 1.0))
        data = self.cache.get_frame_data(1)
        self.assertEqual(data.shape, (8, 16, 4))
        # R=0.2, G=0.4, B=0.6, A=0.9 from fixture channel layout
        self.assertTrue(np.allclose(data[..., 0].astype(np.float32), 0.2, atol=0.01))
        self.assertTrue(np.allclose(data[..., 1].astype(np.float32), 0.4, atol=0.01))
        self.assertTrue(np.allclose(data[..., 2].astype(np.float32), 0.6, atol=0.01))
        self.assertTrue(np.allclose(data[..., 3].astype(np.float32), 0.9, atol=0.01))

    def test_decode_resolution_scale(self):
        path = self.tmp_path / "plate.1001.exr"
        write_float_exr(path, width=64, height=32, value=0.5)
        self.assertTrue(self.cache.decode_and_cache_frame(1, str(path), 0.5))
        data = self.cache.get_frame_data(1)
        self.assertEqual(data.shape, (16, 32, 4))
        self.assertTrue(np.allclose(data[..., :3].astype(np.float32), 0.5, atol=0.02))

    def test_decode_tiled_exr_proxy_scales(self):
        path = self.tmp_path / "tiled.1001.exr"
        write_tiled_float_exr(path, width=128, height=96, value=0.4, tile_size=64)
        self.assertTrue(self.cache.decode_and_cache_frame(10, str(path), 0.5))
        half = self.cache.get_frame_data(10)
        self.assertEqual(half.shape, (48, 64, 4))
        self.assertTrue(np.allclose(half[..., :3].astype(np.float32), 0.4, atol=0.02))

        self.assertTrue(self.cache.decode_and_cache_frame(11, str(path), 0.25))
        quarter = self.cache.get_frame_data(11)
        self.assertEqual(quarter.shape, (24, 32, 4))
        self.assertTrue(np.allclose(quarter[..., :3].astype(np.float32), 0.4, atol=0.02))

    def test_decode_proxy_scanline_matches_constant(self):
        """Scanline EXR at 0.5 must match constant fill (band path)."""
        path = self.tmp_path / "scan.1001.exr"
        write_float_exr(path, width=80, height=40, value=0.6)
        self.assertTrue(self.cache.decode_and_cache_frame(1, str(path), 0.5))
        data = self.cache.get_frame_data(1)
        self.assertEqual(data.shape, (20, 40, 4))
        self.assertTrue(np.allclose(data[..., :3].astype(np.float32), 0.6, atol=0.02))

    def test_decode_mipmapped_exact_mip(self):
        path = self.tmp_path / "mip.1001.exr"
        try:
            write_mipmapped_float_exr(path, width=64, height=32, base_value=0.5)
        except RuntimeError as exc:
            self.skipTest(f"mipmapped EXR write unsupported: {exc}")

        # scale 0.5 → 32×16 matches mip level 1; constant fill stays ~0.5
        self.assertTrue(self.cache.decode_and_cache_frame(1, str(path), 0.5))
        data = self.cache.get_frame_data(1)
        self.assertEqual(data.shape, (16, 32, 4))
        self.assertTrue(np.allclose(data[..., :3].astype(np.float32), 0.5, atol=0.05))

    def test_decode_dpx(self):
        path = self.tmp_path / "plate.1001.dpx"
        write_uint16_dpx(path, width=32, height=16)
        self.assertTrue(self.cache.decode_and_cache_frame(1, str(path), 1.0))
        data = self.cache.get_frame_data(1)
        self.assertEqual(data.shape, (16, 32, 4))
        # uint16 32768 → ~0.5 in float
        self.assertTrue(np.allclose(data[..., :3].astype(np.float32), 0.5, atol=0.02))

    def test_set_decode_threads(self):
        framecycler_engine.set_decode_threads(2)
        framecycler_engine.set_decode_threads(4)


if __name__ == "__main__":
    unittest.main()
