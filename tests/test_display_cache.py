import unittest

from src.framecycler.core.settings import Settings
from src.framecycler.core.system_memory import clamp_cache_limits, PlatformCacheLimits


class TestDisplayCacheSettings(unittest.TestCase):
    def test_display_cache_setting_exists(self):
        settings = Settings(config_dir="./tests_config_temp_display")
        self.assertGreaterEqual(settings.display_cache_limit_gb, 0.0)

    def test_clamp_preserves_zero_display_cache(self):
        limits = PlatformCacheLimits(
            decode_max_gb=32.0,
            display_max_gb=12.0,
            coupled=False,
            combined_max_gb=44.0,
            system_memory_gb=32.0,
            vram_gb=12.0,
            platform_label="Linux",
        )
        decode, display = clamp_cache_limits(8.0, 0.0, limits)
        self.assertEqual(display, 0.0)


if __name__ == "__main__":
    unittest.main()
