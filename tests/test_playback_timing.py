import unittest

from src.framecycler.core.playback_timing import (
    PLAYBACK_TIMING_EVERY_FRAME,
    PLAYBACK_TIMING_REALTIME,
    advance_playback,
    every_frame_can_advance,
    normalize_playback_timing,
    realtime_steps,
)


class TestNormalizePlaybackTiming(unittest.TestCase):
    def test_valid_values(self):
        self.assertEqual(
            normalize_playback_timing(PLAYBACK_TIMING_EVERY_FRAME),
            PLAYBACK_TIMING_EVERY_FRAME,
        )
        self.assertEqual(
            normalize_playback_timing(PLAYBACK_TIMING_REALTIME),
            PLAYBACK_TIMING_REALTIME,
        )

    def test_invalid_defaults_to_every_frame(self):
        self.assertEqual(normalize_playback_timing(None), PLAYBACK_TIMING_EVERY_FRAME)
        self.assertEqual(normalize_playback_timing("bogus"), PLAYBACK_TIMING_EVERY_FRAME)


class TestRealtimeSteps(unittest.TestCase):
    def test_zero_until_one_frame_elapses(self):
        self.assertEqual(realtime_steps(0.0, 24.0), 0)
        self.assertEqual(realtime_steps(1.0 / 24.0 - 1e-9, 24.0), 0)

    def test_counts_whole_frames(self):
        self.assertEqual(realtime_steps(1.0 / 24.0, 24.0), 1)
        self.assertEqual(realtime_steps(5.0 / 24.0, 24.0), 5)
        self.assertEqual(realtime_steps(1.0, 24.0), 24)

    def test_non_positive_inputs(self):
        self.assertEqual(realtime_steps(-1.0, 24.0), 0)
        self.assertEqual(realtime_steps(1.0, 0.0), 0)


class TestAdvancePlaybackEveryFrame(unittest.TestCase):
    def test_single_step_forward(self):
        result = advance_playback(10, 1, 1, 0, 100, "loop")
        self.assertEqual(result.frame, 11)
        self.assertEqual(result.direction, 1)
        self.assertFalse(result.stop)

    def test_no_steps_means_no_movement(self):
        result = advance_playback(10, 1, 0, 0, 100, "loop")
        self.assertIsNone(result.frame)
        self.assertFalse(result.stop)

    def test_once_stops_at_end_without_leaving_range(self):
        result = advance_playback(10, 1, 1, 0, 10, "once")
        self.assertEqual(result.frame, 10)
        self.assertTrue(result.stop)

    def test_loop_wraps(self):
        result = advance_playback(10, 1, 1, 0, 10, "loop")
        self.assertEqual(result.frame, 0)
        self.assertFalse(result.stop)

    def test_bounce_reverses(self):
        result = advance_playback(10, 1, 1, 0, 10, "bounce")
        self.assertEqual(result.frame, 9)
        self.assertEqual(result.direction, -1)
        self.assertFalse(result.stop)


class TestAdvancePlaybackRealtime(unittest.TestCase):
    def test_multi_step_jump(self):
        result = advance_playback(0, 1, 5, 0, 100, "loop")
        self.assertEqual(result.frame, 5)
        self.assertEqual(result.direction, 1)
        self.assertFalse(result.stop)

    def test_realtime_catch_up_can_skip_frames(self):
        # Wall-clock behind by 10 frames → jump past intermediates
        steps = realtime_steps(10.0 / 24.0, 24.0)
        self.assertEqual(steps, 10)
        result = advance_playback(0, 1, steps, 0, 100, "loop")
        self.assertEqual(result.frame, 10)

    def test_once_catch_up_stops_on_last_frame(self):
        result = advance_playback(0, 1, 50, 0, 10, "once")
        self.assertEqual(result.frame, 10)
        self.assertTrue(result.stop)

    def test_loop_catch_up_wraps_multiple_times(self):
        # Range length 11 (0..10); 25 steps from 0 → frame 3
        result = advance_playback(0, 1, 25, 0, 10, "loop")
        self.assertEqual(result.frame, 3)
        self.assertFalse(result.stop)

    def test_reverse_realtime(self):
        result = advance_playback(20, -1, 4, 0, 20, "loop")
        self.assertEqual(result.frame, 16)
        self.assertEqual(result.direction, -1)


class TestEveryFrameCanAdvance(unittest.TestCase):
    def test_waits_for_next_decode(self):
        self.assertFalse(every_frame_can_advance(next_decode_ready=False))

    def test_advances_when_decode_ready(self):
        self.assertTrue(every_frame_can_advance(next_decode_ready=True))


class TestPlaybackTimingSettingsRoundTrip(unittest.TestCase):
    def test_settings_default_and_persist(self):
        import os
        import shutil
        from unittest.mock import patch

        from src.framecycler.core.settings import Settings
        from src.framecycler.core.system_memory import PlatformCacheLimits

        test_dir = os.path.abspath("./tests_playback_timing_config_temp")
        if os.path.exists(test_dir):
            shutil.rmtree(test_dir)

        mock_limits = PlatformCacheLimits(
            decode_max_gb=64.0,
            display_max_gb=16.0,
            coupled=False,
            combined_max_gb=80.0,
            system_memory_gb=64.0,
            vram_gb=16.0,
            platform_label="MockPlatform",
        )
        with patch(
            "src.framecycler.core.settings.get_platform_cache_limits",
            return_value=mock_limits,
        ):
            settings = Settings(config_dir=test_dir)
            self.assertEqual(settings.playback_timing, PLAYBACK_TIMING_EVERY_FRAME)
            settings.playback_timing = PLAYBACK_TIMING_REALTIME
            settings.save()
            reloaded = Settings(config_dir=test_dir)
            self.assertEqual(reloaded.playback_timing, PLAYBACK_TIMING_REALTIME)

        shutil.rmtree(test_dir)


if __name__ == "__main__":
    unittest.main()
