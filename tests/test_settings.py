import unittest
import os
import shutil
import json
from unittest.mock import patch
from src.framecycler.core.system_memory import PlatformCacheLimits
from src.framecycler.core.settings import Settings
from src.framecycler.core.playback_timing import PLAYBACK_TIMING_EVERY_FRAME


class TestSettings(unittest.TestCase):
    def setUp(self):
        self.test_dir = os.path.abspath("./tests_config_temp")
        if os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir)
        
        self.mock_limits = PlatformCacheLimits(
            decode_max_gb=64.0,
            display_max_gb=16.0,
            coupled=False,
            combined_max_gb=80.0,
            system_memory_gb=64.0,
            vram_gb=16.0,
            platform_label="MockPlatform"
        )
        self.patcher = patch("src.framecycler.core.settings.get_platform_cache_limits", return_value=self.mock_limits)
        self.patcher.start()
        
        self.settings = Settings(config_dir=self.test_dir)

    def tearDown(self):
        self.patcher.stop()
        if os.path.exists(self.settings.config_dir):
            shutil.rmtree(self.settings.config_dir)

    def test_default_values(self):
        self.assertGreaterEqual(self.settings.decode_cache_limit_gb, 0.0)
        self.assertGreaterEqual(self.settings.display_cache_limit_gb, 0.0)
        self.assertEqual(self.settings.default_fps, 24.0)
        self.assertEqual(self.settings.playback_timing, PLAYBACK_TIMING_EVERY_FRAME)
        self.assertEqual(self.settings.ram_cache_limit_gb, self.settings.decode_cache_limit_gb)

    def test_save_and_load(self):
        self.settings.reader_threads = 12
        self.settings.decode_cache_limit_gb = 16.0
        self.settings.display_cache_limit_gb = 4.0
        self.settings.default_fps = 30.0
        self.settings.resolution_scale = 0.5
        self.settings.pixel_probe_geometry = [120, 80, 300, 400]
        self.settings.save()

        new_settings = Settings(config_dir=self.settings.config_dir)
        new_settings.load()

        self.assertEqual(new_settings.reader_threads, 12)
        self.assertEqual(new_settings.decode_cache_limit_gb, 16.0)
        self.assertEqual(new_settings.display_cache_limit_gb, 4.0)
        self.assertEqual(new_settings.default_fps, 30.0)
        self.assertEqual(new_settings.resolution_scale, 1.0)
        self.assertEqual(new_settings.pixel_probe_geometry, [120, 80, 300, 400])

    def test_legacy_ram_key_migration(self):
        config_path = os.path.join(self.settings.config_dir, "settings.json")
        os.makedirs(self.settings.config_dir, exist_ok=True)
        with open(config_path, "w", encoding="utf-8") as handle:
            json.dump({"ram_cache_limit_gb": 6.5}, handle)

        migrated = Settings(config_dir=self.settings.config_dir)
        self.assertEqual(migrated.decode_cache_limit_gb, 6.5)

    def test_zero_cache_limits_allowed(self):
        self.settings.decode_cache_limit_gb = 0.0
        self.settings.display_cache_limit_gb = 0.0
        self.settings.save()
        reloaded = Settings(config_dir=self.settings.config_dir)
        self.assertEqual(reloaded.decode_cache_limit_gb, 0.0)
        self.assertEqual(reloaded.display_cache_limit_gb, 0.0)

    def test_resolution_scale_not_persisted(self):
        self.settings.resolution_scale = 0.5
        self.settings.save()
        config_path = os.path.join(self.settings.config_dir, "settings.json")
        with open(config_path, "r", encoding="utf-8") as handle:
            saved = handle.read()
        self.assertNotIn("resolution_scale", saved)

        new_settings = Settings(config_dir=self.settings.config_dir)
        self.assertEqual(new_settings.resolution_scale, 1.0)

    def test_resolution_scale_clamped_in_session(self):
        self.settings.resolution_scale = Settings.clamp_resolution_scale(2.0)
        self.assertEqual(self.settings.resolution_scale, 1.0)

    def test_package_enabled_round_trip(self):
        self.settings.package_enabled = {
            "framecycler.example_apply_cdl": True,
            "framecycler.ocio_api_loader": False,
        }
        self.settings.save()
        reloaded = Settings(config_dir=self.settings.config_dir)
        self.assertEqual(
            reloaded.package_enabled,
            {
                "framecycler.example_apply_cdl": True,
                "framecycler.ocio_api_loader": False,
            },
        )


if __name__ == "__main__":
    unittest.main()
