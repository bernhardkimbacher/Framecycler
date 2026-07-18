import os
import time
import unittest

import numpy as np

from src.framecycler import framecycler_engine
from src.framecycler.core.cache import CacheEngine
from src.framecycler.core.settings import Settings
from src.framecycler.decoders.qt_decoder import QuickTimeDecoder


FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "tiny_movie.mp4")


def _wait_until(predicate, timeout=3.0, interval=0.01):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


@unittest.skipUnless(os.path.isfile(FIXTURE), "tiny_movie.mp4 fixture missing")
class TestNativeMovieDecoder(unittest.TestCase):
    def test_probe_fields(self):
        dec = framecycler_engine.NativeMovieDecoder()
        self.assertTrue(dec.open(FIXTURE))
        try:
            probe = dec.probe()
            self.assertEqual(probe["width"], 64)
            self.assertEqual(probe["height"], 48)
            self.assertAlmostEqual(probe["fps"], 24.0, places=1)
            self.assertGreaterEqual(probe["frame_count"], 10)
            self.assertEqual(probe["timecode_start"], "01:00:00:00")
            self.assertEqual(probe["start_frame"], 86400)
            self.assertEqual(probe["end_frame"], probe["start_frame"] + probe["frame_count"] - 1)
            self.assertIn("R", probe["channels"])
            self.assertFalse(probe["has_alpha"])
        finally:
            dec.close()

    def test_sequential_and_seek_decode(self):
        dec = framecycler_engine.NativeMovieDecoder()
        self.assertTrue(dec.open(FIXTURE))
        try:
            start = dec.start_frame
            f0 = dec.decode_frame(start, 1.0)
            self.assertIsNotNone(f0)
            self.assertEqual(f0.shape, (48, 64, 4))
            self.assertEqual(f0.dtype, np.float16)

            f1 = dec.decode_frame(start + 1, 1.0)
            self.assertIsNotNone(f1)
            self.assertEqual(f1.shape, (48, 64, 4))

            # Seek backward then forward.
            f5 = dec.decode_frame(start + 5, 1.0)
            self.assertIsNotNone(f5)
            f2 = dec.decode_frame(start + 2, 1.0)
            self.assertIsNotNone(f2)

            half = dec.decode_frame(start, 0.5)
            self.assertIsNotNone(half)
            self.assertEqual(half.shape[0], 24)
            self.assertEqual(half.shape[1], 32)
        finally:
            dec.close()

    def test_write_into_cache_manager(self):
        dec = framecycler_engine.NativeMovieDecoder()
        self.assertTrue(dec.open(FIXTURE))
        cache = framecycler_engine.CacheManager(0.1)
        try:
            start = dec.start_frame
            img = dec.decode_frame(start, 1.0)
            self.assertIsNotNone(img)
            cache.write_frame(start, img.shape[1], img.shape[0], img.shape[2], img)
            self.assertTrue(cache.has_frame(start))
            view = cache.get_frame_data(start)
            self.assertEqual(view.shape, img.shape)
        finally:
            dec.close()


@unittest.skipUnless(os.path.isfile(FIXTURE), "tiny_movie.mp4 fixture missing")
class TestQuickTimeDecoderFacade(unittest.TestCase):
    def test_metadata_and_read_frame(self):
        qt = QuickTimeDecoder(FIXTURE)
        try:
            meta = qt.get_metadata()
            self.assertEqual(meta["width"], 64)
            self.assertEqual(meta["height"], 48)
            self.assertTrue(qt.uses_native_movie_decode())
            self.assertFalse(qt.uses_native_path_decode())
            frame = qt.read_frame(meta["start_frame"])
            self.assertEqual(frame["data"].shape, (48, 64, 4))
            self.assertEqual(frame["data"].dtype, np.float16)
        finally:
            qt.close()


@unittest.skipUnless(os.path.isfile(FIXTURE), "tiny_movie.mp4 fixture missing")
class TestPrefetchEngineMovieMode(unittest.TestCase):
    def test_movie_mode_fills_without_python_callback(self):
        settings = Settings()
        settings.decode_cache_limit_gb = 0.1
        settings.reader_threads = 2
        decoder = QuickTimeDecoder(FIXTURE)
        engine = CacheEngine(decoder, settings)
        try:
            start = decoder.start_frame
            ready = []
            engine.add_frame_ready_callback(lambda f: ready.append(f))
            engine.start()
            engine.set_playhead(start, 1)
            self.assertTrue(
                _wait_until(lambda: engine.has_frame(start), timeout=5.0),
                "movie playhead never cached",
            )
            self.assertIn(start, ready)
            # Sequential neighbor should fill under Movie mode.
            self.assertTrue(
                _wait_until(lambda: engine.has_frame(start + 1), timeout=5.0),
                "lookahead neighbor never cached",
            )
        finally:
            engine.close()
            decoder.close()

    def test_direct_movie_prefetch(self):
        movie = framecycler_engine.NativeMovieDecoder()
        self.assertTrue(movie.open(FIXTURE))
        cache = framecycler_engine.CacheManager(0.1)
        engine = framecycler_engine.PrefetchEngine(cache, 2)
        ready = []
        try:
            start = movie.start_frame
            end = movie.end_frame
            engine.set_frame_ready_callback(lambda f: ready.append(f))
            engine.set_python_decode_callback(None)
            engine.set_movie_decoder(movie)
            engine.set_options(
                1.0, "", "Flat Gray", 64, 48, framecycler_engine.PrefetchDecodeMode.Movie
            )
            engine.set_enabled(True)
            engine.set_playback_range(start, end)
            engine.set_playhead(start, 1)
            engine.schedule(start + 3, 0)
            self.assertTrue(_wait_until(lambda: cache.has_frame(start + 3), timeout=5.0))
            self.assertIn(start + 3, ready)
        finally:
            engine.stop()
            movie.close()


if __name__ == "__main__":
    unittest.main()
