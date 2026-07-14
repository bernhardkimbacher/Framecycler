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

if __name__ == "__main__":
    unittest.main()
