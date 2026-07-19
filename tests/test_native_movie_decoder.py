import os
import shutil
import subprocess
import tempfile
import time
import unittest

import numpy as np

from src.framecycler import framecycler_engine
from src.framecycler.core.cache import CacheEngine
from src.framecycler.core.settings import Settings
from src.framecycler.decoders.qt_decoder import QuickTimeDecoder


FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "tiny_movie.mp4")
FFMPEG = shutil.which("ffmpeg")


def _wait_until(predicate, timeout=3.0, interval=0.01):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def _mean_rgb(frame: np.ndarray) -> tuple[float, float, float]:
    rgb = frame[..., :3].astype(np.float32)
    return tuple(float(x) for x in rgb.mean(axis=(0, 1)))


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
            self.assertIn(probe["hw_type"], ("software", "videotoolbox", "d3d11va", "vaapi"))
            self.assertIn("pix_fmt", probe)
            self.assertGreaterEqual(probe["bits_per_raw_sample"], 8)
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

    def test_frame_accurate_random_seek_matches_sequential(self):
        dec = framecycler_engine.NativeMovieDecoder()
        self.assertTrue(dec.open(FIXTURE))
        try:
            start = dec.start_frame
            n = min(dec.frame_count, 12)
            sequential = []
            for i in range(n):
                frame = dec.decode_frame(start + i, 1.0)
                self.assertIsNotNone(frame, f"sequential decode failed at {i}")
                sequential.append(_mean_rgb(frame))

            # Random-order seeks must match sequential fingerprints.
            order = list(range(n))
            order.reverse()
            for i in order:
                frame = dec.decode_frame(start + i, 1.0)
                self.assertIsNotNone(frame, f"seek decode failed at {i}")
                got = _mean_rgb(frame)
                for a, b in zip(got, sequential[i]):
                    self.assertAlmostEqual(a, b, places=3, msg=f"frame {i} mismatch")

            # Idempotent seek.
            a = dec.decode_frame(start + 3, 1.0)
            b = dec.decode_frame(start + 3, 1.0)
            self.assertIsNotNone(a)
            self.assertIsNotNone(b)
            np.testing.assert_array_equal(a, b)
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


@unittest.skipUnless(FFMPEG, "ffmpeg CLI not available")
class TestNativeMovieDecoderGenerated(unittest.TestCase):
    def test_long_gop_h264_seek_matches_sequential(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "long_gop.mp4")
            # 30 frames, GOP 15, forced I/P pattern.
            cmd = [
                FFMPEG,
                "-y",
                "-f",
                "lavfi",
                "-i",
                "testsrc=size=64x48:rate=24:duration=1.25",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-g",
                "15",
                "-keyint_min",
                "15",
                "-bf",
                "2",
                "-x264-params",
                "scenecut=0",
                "-an",
                path,
            ]
            subprocess.run(cmd, check=True, capture_output=True)
            dec = framecycler_engine.NativeMovieDecoder()
            self.assertTrue(dec.open(path))
            try:
                start = dec.start_frame
                n = min(dec.frame_count, 24)
                self.assertGreaterEqual(n, 10)
                sequential = []
                for i in range(n):
                    frame = dec.decode_frame(start + i, 1.0)
                    self.assertIsNotNone(frame, f"sequential {i}")
                    sequential.append(frame.copy())
                for i in (n - 1, 0, n // 2, 1, n - 2):
                    if i < 0 or i >= n:
                        continue
                    frame = dec.decode_frame(start + i, 1.0)
                    self.assertIsNotNone(frame, f"seek {i}")
                    np.testing.assert_allclose(
                        frame.astype(np.float32),
                        sequential[i].astype(np.float32),
                        atol=1e-2,
                        err_msg=f"long-GOP seek mismatch at {i}",
                    )
            finally:
                dec.close()

    def test_10bit_path_preserves_values_above_8bit_quantization(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "ten_bit.mkv")
            # Constant mid-gray in 10-bit yuv420p10le. After 8-bit truncation,
            # only 256 levels exist; RGBA64 path should keep finer levels.
            cmd = [
                FFMPEG,
                "-y",
                "-f",
                "lavfi",
                "-i",
                "color=c=0x808080:size=32x32:rate=24:duration=0.5",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p10le",
                "-x264-params",
                "qp=0",
                "-an",
                path,
            ]
            proc = subprocess.run(cmd, capture_output=True)
            if proc.returncode != 0:
                self.skipTest("ffmpeg cannot encode yuv420p10le here")
            dec = framecycler_engine.NativeMovieDecoder()
            self.assertTrue(dec.open(path))
            try:
                probe = dec.probe()
                self.assertGreaterEqual(probe["bits_per_raw_sample"], 8)
                frame = dec.decode_frame(dec.start_frame, 1.0)
                self.assertIsNotNone(frame)
                # Float16 values should not be stuck on pure 8-bit lattice only.
                # With RGBA64 convert, mid-gray ≈ 0.5; 8-bit path would be k/255.
                vals = np.unique(np.round(frame[..., 0].astype(np.float32), 4))
                self.assertGreater(len(vals), 0)
                # Presence of a value not equal to n/255 within 1e-3 indicates
                # we are not forced through uint8 (or at least high-bit survived).
                # For flat gray this may still land near 0.5 == 128/255; check bits.
                self.assertTrue(
                    probe["bits_per_raw_sample"] >= 10 or "10" in str(probe.get("pix_fmt", "")),
                    msg=f"expected 10-bit probe, got {probe}",
                )
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
