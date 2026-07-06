import unittest
import numpy as np
from src.framecycler import framecycler_engine

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
        
        # 4. Fetch zero-copy array back and check content
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

if __name__ == "__main__":
    unittest.main()
