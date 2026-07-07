import unittest
import os
import shutil
from src.framecycler.core.settings import Settings

class TestSettings(unittest.TestCase):
    def setUp(self):
        # Override config dir to use a temporary tests location
        self.test_dir = os.path.abspath("./tests_config_temp")
        if os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir)
        self.settings = Settings(config_dir=self.test_dir)

    def tearDown(self):
        if os.path.exists(self.settings.config_dir):
            shutil.rmtree(self.settings.config_dir)

    def test_default_values(self):
        self.assertEqual(self.settings.ram_cache_limit_gb, 8.0)
        self.assertEqual(self.settings.default_fps, 24.0)

    def test_save_and_load(self):
        self.settings.reader_threads = 12
        self.settings.ram_cache_limit_gb = 16.0
        self.settings.default_fps = 30.0
        self.settings.resolution_scale = 0.5
        self.settings.save()

        # Load into new instance
        new_settings = Settings(config_dir=self.settings.config_dir)
        new_settings.load()

        self.assertEqual(new_settings.reader_threads, 12)
        self.assertEqual(new_settings.ram_cache_limit_gb, 16.0)
        self.assertEqual(new_settings.default_fps, 30.0)
        self.assertEqual(new_settings.resolution_scale, 1.0)

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


if __name__ == "__main__":
    unittest.main()
