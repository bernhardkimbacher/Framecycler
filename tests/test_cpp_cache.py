import unittest
import numpy as np
from src.framecycler import framecycler_engine

class TestCppCache(unittest.TestCase):
    def test_cache_manager_basic(self):
        # 1. Instantiate C++ CacheManager (RAM Limit = 0.05 GB to trigger eviction quickly)
        cache = framecycler_engine.CacheManager(0.05)
        self.assertFalse(cache.has_frame(0))
        
        # 2. Write a float32 frame: width=1920, height=1080, channels=4 (~33 MB)
        w, h, c = 1920, 1080, 4
        frame_data = np.ones((h, w, c), dtype=np.float32) * 0.5
        
        # Call positionally
        cache.write_frame(10, w, h, c, frame_data)
        
        # 3. Check cached state
        self.assertTrue(cache.has_frame(10))
        self.assertEqual(cache.get_cached_frames(), [10])
        
        # 4. Fetch zero-copy array back and check content
        retrieved_data = cache.get_frame_data(10)
        self.assertIsNotNone(retrieved_data)
        self.assertEqual(retrieved_data.shape, (h, w, c))
        self.assertTrue(np.allclose(retrieved_data, 0.5))

    def test_eviction(self):
        # Limit to 0.04 GB so it can only fit one 1920x1080 float32 RGBA frame (~33 MB)
        cache = framecycler_engine.CacheManager(0.04)
        
        w, h, c = 1920, 1080, 4
        frame1 = np.ones((h, w, c), dtype=np.float32) * 0.1
        frame2 = np.ones((h, w, c), dtype=np.float32) * 0.2
        
        # Call positionally
        cache.set_playhead(0, 1, 0, 100)
        
        # Write first frame
        cache.write_frame(0, w, h, c, frame1)
        self.assertTrue(cache.has_frame(0))
        
        # Write second frame - should trigger eviction of frame 0 since limit is exceeded
        cache.write_frame(1, w, h, c, frame2)
        
        self.assertTrue(cache.has_frame(1))
        self.assertFalse(cache.has_frame(0)) # Evicted!

if __name__ == "__main__":
    unittest.main()
