"""Tests for NativeAudioDecoder, peaks, and audio settings."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.framecycler.core.settings import Settings
from src.framecycler.core.system_memory import PlatformCacheLimits
from src.framecycler.ui.timeline_editor import TimelineEditor, TimelineSegmentInfo, TimelineVersionInfo

try:
    from src.framecycler import framecycler_engine
except ImportError:
    import framecycler_engine

FFMPEG = shutil.which("ffmpeg")
HAS_AUDIO_API = hasattr(framecycler_engine, "NativeAudioDecoder")


def _make_movie_with_audio(path: str, seconds: float = 0.5) -> None:
    subprocess.run(
        [
            FFMPEG,
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"color=c=black:s=64x48:r=24:d={seconds}",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:sample_rate=48000:duration={seconds}",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            path,
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


@unittest.skipUnless(HAS_AUDIO_API and FFMPEG, "NativeAudioDecoder + ffmpeg required")
class TestNativeAudioDecoder(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="fc_audio_")
        self.movie = os.path.join(self._tmpdir, "tone.mp4")
        _make_movie_with_audio(self.movie)

    def tearDown(self):
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_open_has_audio_and_decode(self):
        dec = framecycler_engine.NativeAudioDecoder()
        self.assertTrue(dec.open(self.movie))
        self.assertTrue(dec.has_audio())
        self.assertGreater(dec.duration_seconds, 0.2)
        self.assertEqual(dec.sample_rate, 48000)
        self.assertEqual(dec.channels, 2)
        self.assertTrue(dec.seek(0.0))
        frames = dec.decode_frames(2048)
        self.assertIsNotNone(frames)
        self.assertEqual(frames.shape[1], 2)
        self.assertGreater(frames.shape[0], 100)
        # Sine should have non-trivial energy
        self.assertGreater(float(abs(frames).mean()), 0.001)
        peaks = dec.build_peaks(200)
        self.assertGreater(len(peaks), 10)
        self.assertGreater(float(max(peaks)), 0.01)
        dec.close()

    def test_video_only_has_no_audio(self):
        fixture = Path(__file__).resolve().parent / "fixtures" / "tiny_movie.mp4"
        if not fixture.is_file():
            self.skipTest("tiny_movie.mp4 missing")
        dec = framecycler_engine.NativeAudioDecoder()
        self.assertTrue(dec.open(str(fixture)))
        self.assertFalse(dec.has_audio())
        self.assertEqual(len(dec.build_peaks()), 0)
        dec.close()


class TestAudioSettings(unittest.TestCase):
    def setUp(self):
        self.test_dir = os.path.abspath("./tests_config_audio_temp")
        if os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir)
        self.mock_limits = PlatformCacheLimits(
            decode_max_gb=64.0,
            display_max_gb=16.0,
            coupled=False,
            combined_max_gb=80.0,
            system_memory_gb=64.0,
            vram_gb=16.0,
            platform_label="MockPlatform",
        )
        self.patcher = patch(
            "src.framecycler.core.settings.get_platform_cache_limits",
            return_value=self.mock_limits,
        )
        self.patcher.start()
        self.settings = Settings(config_dir=self.test_dir)

    def tearDown(self):
        self.patcher.stop()
        if os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir)

    def test_audio_settings_round_trip(self):
        self.settings.audio_muted = True
        self.settings.audio_volume = 0.42
        self.settings.scrub_audio = True
        self.settings.timeline_show_waveform = True
        self.settings.audio_output_device_id = "abc123"
        self.settings.save()
        loaded = Settings(config_dir=self.test_dir)
        self.assertTrue(loaded.audio_muted)
        self.assertAlmostEqual(loaded.audio_volume, 0.42, places=5)
        self.assertTrue(loaded.scrub_audio)
        self.assertTrue(loaded.timeline_show_waveform)
        self.assertEqual(loaded.audio_output_device_id, "abc123")


class TestTimelineWaveform(unittest.TestCase):
    def test_set_show_waveform_and_peaks(self):
        from PySide6.QtWidgets import QApplication
        import sys

        app = QApplication.instance() or QApplication(sys.argv)
        tl = TimelineEditor()
        tl.set_show_waveform(True)
        self.assertTrue(tl.show_waveform)
        seg = TimelineSegmentInfo(
            index=0,
            global_start=0,
            global_end=23,
            versions=[TimelineVersionInfo(name="a", is_active=True)],
            audio_peaks=[0.1, 0.5, 0.2, 0.8],
        )
        tl.set_segments([seg])
        tl.resize(400, 120)
        tl.repaint()
        app.processEvents()


if __name__ == "__main__":
    unittest.main()
