import time
import unittest

import numpy as np

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


class _FakeDecoder(BaseDecoder):
    def __init__(self, channels: int = 3):
        self._channels = channels
        self._meta = {"frame_count": 2, "fps": 24.0, "start_frame": 0, "end_frame": 1, "channels": ["R", "G", "B"]}

    def get_metadata(self):
        return self._meta

    def read_frame(self, frame_index: int, resolution_scale: float = 1.0):
        h, w = 16, 32
        if self._channels == 1:
            data = np.full((h, w, 1), frame_index * 0.1, dtype=np.float16)
        else:
            data = np.zeros((h, w, 3), dtype=np.float16)
            data[..., 0] = frame_index * 0.1
            data[..., 1] = 0.5
            data[..., 2] = 0.25
        return {"data": data}

    def close(self):
        pass


class _ContainerDecoder(BaseDecoder):
    """Mimics QuickTime: get_file_path is None, pixels come from read_frame."""

    def __init__(self):
        self.read_calls = []
        self._meta = {
            "frame_count": 2,
            "fps": 24.0,
            "start_frame": 0,
            "end_frame": 1,
            "width": 16,
            "height": 8,
            "channels": ["R", "G", "B"],
        }

    def get_metadata(self):
        return self._meta

    def read_frame(self, frame_index: int, resolution_scale: float = 1.0):
        self.read_calls.append(frame_index)
        data = np.full((8, 16, 3), 0.75, dtype=np.float16)
        return {"data": data, "channels": ["R", "G", "B"], "frame_index": frame_index, "timecode": "01:00:00:00"}

    def close(self):
        pass


class _NativePathDecoder(BaseDecoder):
    """Mimics EXR/DPX path decode; missing frames return None from get_file_path."""

    def __init__(self, path_for_frame=None):
        self._path_for_frame = path_for_frame
        self.metadata = {"width": 64, "height": 32, "channels": ["R", "G", "B", "A"]}
        self.active_layer = ""
        self._meta = {
            "frame_count": 2,
            "fps": 24.0,
            "start_frame": 0,
            "end_frame": 1,
            "width": 64,
            "height": 32,
            "channels": ["R", "G", "B", "A"],
        }

    def get_metadata(self):
        return self._meta

    def read_frame(self, frame_index: int, resolution_scale: float = 1.0):
        raise AssertionError("native-path decoder should not call read_frame")

    def get_file_path(self, frame_index: int, fallback_nearest: bool = False) -> str | None:
        return self._path_for_frame

    def uses_native_path_decode(self) -> bool:
        return True

    def close(self):
        pass


class TestCacheRgbaExpansion(unittest.TestCase):
    def test_prepare_cache_image_expands_rgb_to_rgba(self):
        rgb = np.zeros((4, 4, 3), dtype=np.float16)
        img, channels = CacheEngine._prepare_cache_image(rgb)
        self.assertEqual(channels, 4)
        self.assertEqual(img.shape, (4, 4, 4))
        self.assertTrue(np.allclose(img[..., 3].astype(np.float32), 1.0))

    def test_prepare_cache_image_keeps_single_channel(self):
        gray = np.zeros((4, 4, 1), dtype=np.float16)
        img, channels = CacheEngine._prepare_cache_image(gray)
        self.assertEqual(channels, 1)
        self.assertEqual(img.shape, (4, 4, 1))

    def test_cache_worker_stores_rgba_for_rgb_decoder(self):
        settings = Settings()
        settings.decode_cache_limit_gb = 0.1
        engine = CacheEngine(_FakeDecoder(channels=3), settings)
        try:
            engine.set_playhead(0, 1)
            self.assertTrue(_wait_until(lambda: engine.has_frame(0)))
            view = engine.native_cache.get_frame_data(0)
            self.assertIsNotNone(view)
            self.assertEqual(view.shape, (16, 32, 4))
            hit = engine.get_frame(0)
            self.assertIsNotNone(hit)
            self.assertEqual(hit["channels"], 4)
        finally:
            engine.close()

    def test_container_decoder_uses_read_frame_despite_get_file_path(self):
        """Regression: BaseDecoder.get_file_path always exists; movies must still use read_frame."""
        settings = Settings()
        settings.decode_cache_limit_gb = 0.1
        decoder = _ContainerDecoder()
        engine = CacheEngine(decoder, settings)
        try:
            engine.set_playhead(0, 1)
            self.assertTrue(_wait_until(lambda: engine.has_frame(0)))
            self.assertIn(0, decoder.read_calls)
            view = engine.native_cache.get_frame_data(0)
            self.assertIsNotNone(view)
            self.assertEqual(view.shape, (8, 16, 4))
            self.assertTrue(np.allclose(view[..., 0].astype(np.float32), 0.75, atol=0.02))
        finally:
            engine.close()

    def test_native_path_missing_frame_uses_placeholder(self):
        """When get_file_path returns None on a native-path decoder, cache a Flat Gray placeholder."""
        settings = Settings()
        settings.decode_cache_limit_gb = 0.1
        settings.missing_frame_mode = "Flat Gray"
        decoder = _NativePathDecoder(path_for_frame=None)
        engine = CacheEngine(decoder, settings)
        try:
            engine._prefetch.set_lookahead(1)
            engine.set_playhead(0, 1)
            self.assertTrue(_wait_until(lambda: engine.has_frame(0)))
            view = engine.native_cache.get_frame_data(0)
            self.assertIsNotNone(view)
            # Placeholder sized from decoder metadata (64x32), Flat Gray ~0.05
            self.assertEqual(view.shape[:2], (32, 64))
            self.assertTrue(np.allclose(view[..., 0].astype(np.float32), 0.05, atol=0.01))
        finally:
            engine.close()


if __name__ == "__main__":
    unittest.main()
