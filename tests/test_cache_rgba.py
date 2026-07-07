import unittest
import numpy as np

from src.framecycler.core.cache import CacheEngine
from src.framecycler.core.settings import Settings


class _FakeDecoder:
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
        settings.ram_cache_limit_gb = 0.1
        engine = CacheEngine(_FakeDecoder(channels=3), settings)
        try:
            engine._read_and_cache_worker(0)
            self.assertTrue(engine.native_cache.has_frame(0))
            view = engine.native_cache.get_frame_data(0)
            self.assertIsNotNone(view)
            self.assertEqual(view.shape, (16, 32, 4))
            hit = engine.get_frame(0)
            self.assertIsNotNone(hit)
            self.assertEqual(hit["channels"], 4)
        finally:
            engine.close()


if __name__ == "__main__":
    unittest.main()
