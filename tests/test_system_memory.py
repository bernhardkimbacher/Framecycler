import unittest
from unittest.mock import patch

from src.framecycler.core.system_memory import (
    PlatformCacheLimits,
    cache_warning_text,
    clamp_cache_limits,
    gb_to_slider_ticks,
    slider_ticks_to_gb,
)


class TestSystemMemory(unittest.TestCase):
    def test_slider_round_trip(self):
        self.assertEqual(slider_ticks_to_gb(80), 8.0)
        self.assertEqual(gb_to_slider_ticks(8.0), 80)

    def test_clamp_uncoupled(self):
        limits = PlatformCacheLimits(
            decode_max_gb=32.0,
            display_max_gb=8.0,
            coupled=False,
            combined_max_gb=40.0,
            system_memory_gb=32.0,
            vram_gb=8.0,
            platform_label="Linux",
        )
        decode, display = clamp_cache_limits(40.0, 12.0, limits)
        self.assertEqual(decode, 32.0)
        self.assertEqual(display, 8.0)

    def test_clamp_coupled_lowers_display(self):
        limits = PlatformCacheLimits(
            decode_max_gb=32.0,
            display_max_gb=32.0,
            coupled=True,
            combined_max_gb=32.0,
            system_memory_gb=32.0,
            vram_gb=32.0,
            platform_label="Darwin",
        )
        decode, display = clamp_cache_limits(20.0, 20.0, limits)
        self.assertEqual(decode, 20.0)
        self.assertEqual(display, 12.0)

    def test_warning_coupled(self):
        limits = PlatformCacheLimits(
            decode_max_gb=32.0,
            display_max_gb=32.0,
            coupled=True,
            combined_max_gb=32.0,
            system_memory_gb=32.0,
            vram_gb=32.0,
            platform_label="Darwin",
        )
        text = cache_warning_text(20.0, 10.0, limits)
        self.assertIn("Warning", text)

    def test_warning_uncoupled_decode(self):
        limits = PlatformCacheLimits(
            decode_max_gb=10.0,
            display_max_gb=8.0,
            coupled=False,
            combined_max_gb=18.0,
            system_memory_gb=10.0,
            vram_gb=8.0,
            platform_label="Windows",
        )
        text = cache_warning_text(9.0, 1.0, limits)
        self.assertIn("Decode cache", text)

    @patch("src.framecycler.core.system_memory.get_total_system_memory_gb", return_value=64.0)
    @patch("src.framecycler.core.system_memory.get_total_vram_gb", return_value=12.0)
    @patch("src.framecycler.core.system_memory.sys.platform", "linux")
    def test_platform_limits_linux(self, _vram, _ram):
        from src.framecycler.core.system_memory import get_platform_cache_limits

        limits = get_platform_cache_limits()
        self.assertFalse(limits.coupled)
        self.assertEqual(limits.decode_max_gb, 64.0)
        self.assertEqual(limits.display_max_gb, 12.0)

    @patch("src.framecycler.core.system_memory.get_total_system_memory_gb", return_value=36.0)
    @patch("src.framecycler.core.system_memory.get_total_vram_gb", return_value=36.0)
    @patch("src.framecycler.core.system_memory.sys.platform", "darwin")
    def test_platform_limits_macos(self, _vram, _ram):
        from src.framecycler.core.system_memory import get_platform_cache_limits

        limits = get_platform_cache_limits()
        self.assertTrue(limits.coupled)
        self.assertEqual(limits.combined_max_gb, 36.0)


if __name__ == "__main__":
    unittest.main()
