import time
import unittest

import numpy as np

from src.framecycler import framecycler_engine
from src.framecycler.core.cache import CacheEngine
from src.framecycler.core.settings import Settings
from src.framecycler.decoders.base import BaseDecoder


def _wait_until(predicate, timeout=2.0, interval=0.01):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


class _ContainerDecoder(BaseDecoder):
    def __init__(self, frame_count=8):
        self.read_calls = []
        self._meta = {
            "frame_count": frame_count,
            "fps": 24.0,
            "start_frame": 0,
            "end_frame": frame_count - 1,
            "width": 8,
            "height": 4,
            "channels": ["R", "G", "B"],
        }

    def get_metadata(self):
        return self._meta

    def read_frame(self, frame_index: int, resolution_scale: float = 1.0):
        self.read_calls.append(frame_index)
        data = np.full((4, 8, 3), 0.5, dtype=np.float16)
        return {"data": data, "channels": ["R", "G", "B"], "frame_index": frame_index}

    def close(self):
        pass


class _NativePathDecoder(BaseDecoder):
    def __init__(self, frame_map: dict[int, str], frame_count=None):
        self.frame_map = dict(frame_map)
        keys = sorted(self.frame_map.keys())
        self.metadata = {"width": 16, "height": 8, "channels": ["R", "G", "B", "A"]}
        self.active_layer = ""
        end = keys[-1] if keys else 0
        count = frame_count if frame_count is not None else (end + 1)
        self._meta = {
            "frame_count": count,
            "fps": 24.0,
            "start_frame": 0,
            "end_frame": count - 1,
            "width": 16,
            "height": 8,
            "channels": ["R", "G", "B", "A"],
        }

    def get_metadata(self):
        return self._meta

    def read_frame(self, frame_index: int, resolution_scale: float = 1.0):
        raise AssertionError("native-path decoder should not call read_frame")

    def get_file_path(self, frame_index: int, fallback_nearest: bool = False) -> str | None:
        if frame_index in self.frame_map:
            return self.frame_map[frame_index]
        if fallback_nearest and self.frame_map:
            nearest = min(self.frame_map.keys(), key=lambda k: abs(k - frame_index))
            return self.frame_map[nearest]
        return None

    def uses_native_path_decode(self) -> bool:
        return True

    def close(self):
        pass


class TestPrefetchEngine(unittest.TestCase):
    def test_claim_blocks_duplicate_decode(self):
        cache = framecycler_engine.CacheManager(0.1)
        self.assertTrue(cache.try_claim_decode(7))
        self.assertTrue(cache.is_decode_claimed(7))
        self.assertFalse(cache.try_claim_decode(7))
        cache.release_decode_claim(7)
        self.assertFalse(cache.is_decode_claimed(7))
        self.assertTrue(cache.try_claim_decode(7))
        cache.release_decode_claim(7)

    def test_no_decode_before_start(self):
        settings = Settings()
        settings.decode_cache_limit_gb = 0.1
        settings.reader_threads = 2
        decoder = _ContainerDecoder(frame_count=4)
        engine = CacheEngine(decoder, settings)
        try:
            time.sleep(0.15)
            self.assertEqual(decoder.read_calls, [])
            self.assertFalse(engine.has_frame(0))
            ready = []
            engine.add_frame_ready_callback(lambda f: ready.append(f))
            engine.start()
            self.assertTrue(_wait_until(lambda: engine.has_frame(0)))
            self.assertIn(0, ready)
            self.assertIn(0, decoder.read_calls)
        finally:
            engine.close()

    def test_python_decode_callback_fills_cache(self):
        settings = Settings()
        settings.decode_cache_limit_gb = 0.1
        settings.reader_threads = 2
        decoder = _ContainerDecoder(frame_count=4)
        engine = CacheEngine(decoder, settings)
        try:
            engine.start()
            self.assertTrue(
                _wait_until(lambda: engine.has_frame(0)),
                "playhead frame never arrived via python decode callback",
            )
            self.assertIn(0, decoder.read_calls)
            view = engine.native_cache.get_frame_data(0)
            self.assertIsNotNone(view)
            self.assertEqual(view.shape, (4, 8, 4))
        finally:
            engine.close()

    def test_idle_fill_reaches_whole_small_range(self):
        settings = Settings()
        settings.decode_cache_limit_gb = 0.1
        settings.reader_threads = 2
        decoder = _ContainerDecoder(frame_count=6)
        engine = CacheEngine(decoder, settings)
        try:
            engine.start()
            self.assertTrue(
                _wait_until(
                    lambda: all(engine.has_frame(i) for i in range(6)),
                    timeout=3.0,
                ),
                f"idle fill incomplete: {engine.get_cached_frames()}",
            )
        finally:
            engine.close()

    def test_native_prefetch_uses_path_table_placeholders(self):
        settings = Settings()
        settings.decode_cache_limit_gb = 0.1
        settings.missing_frame_mode = "Flat Gray"
        settings.reader_threads = 2
        decoder = _NativePathDecoder(frame_map={}, frame_count=4)
        engine = CacheEngine(decoder, settings)
        try:
            engine._prefetch.set_lookahead(2)
            engine.start()
            self.assertTrue(
                _wait_until(lambda: engine.has_frame(0)),
                "native placeholder never cached",
            )
            view = engine.native_cache.get_frame_data(0)
            self.assertIsNotNone(view)
            self.assertEqual(view.shape[:2], (8, 16))
            self.assertTrue(np.allclose(view[..., 0].astype(np.float32), 0.05, atol=0.01))
        finally:
            engine.close()

    def test_seek_prefers_new_playhead(self):
        settings = Settings()
        settings.decode_cache_limit_gb = 0.1
        settings.missing_frame_mode = "Flat Gray"
        settings.reader_threads = 1
        decoder = _NativePathDecoder(frame_map={}, frame_count=40)
        engine = CacheEngine(decoder, settings)
        try:
            engine._prefetch.set_lookahead(4)
            engine.start()
            engine.set_playhead(30, 1)
            self.assertTrue(
                _wait_until(lambda: engine.has_frame(30), timeout=3.0),
                "seeked playhead never cached",
            )
        finally:
            engine.close()

    def test_direct_prefetch_engine_schedule(self):
        cache = framecycler_engine.CacheManager(0.1)
        ready = []
        engine = framecycler_engine.PrefetchEngine(cache, 2)
        try:
            engine.set_frame_ready_callback(lambda f: ready.append(f))
            engine.set_options(1.0, "", "Flat Gray", 8, 4, True)
            engine.set_path_table({}, [])
            engine.set_enabled(True)
            engine.set_playback_range(0, 3)
            engine.set_playhead(0, 1)
            engine.schedule(2, 0)
            self.assertTrue(_wait_until(lambda: cache.has_frame(2)))
            self.assertTrue(_wait_until(lambda: 2 in ready))
        finally:
            engine.stop()

    def test_cache_manager_budget_bytes(self):
        cache = framecycler_engine.CacheManager(0.01)
        self.assertGreater(cache.max_bytes(), 0)
        self.assertEqual(cache.allocated_bytes(), 0)
        self.assertEqual(cache.bytes_per_frame(), 0)
        w, h, c = 8, 4, 4
        data = np.ones((h, w, c), dtype=np.float16)
        cache.write_frame(0, w, h, c, data)
        self.assertEqual(cache.bytes_per_frame(), w * h * c * 2)
        self.assertEqual(cache.allocated_bytes(), w * h * c * 2)


if __name__ == "__main__":
    unittest.main()
