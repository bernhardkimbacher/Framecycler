"""GPU churn counter API tests (finding #2).

Full offscreen RHI present loops are skipped on macOS: RhiRenderer.initialize()
requires a real hosted window (confirmed pre-existing on master — SIGBUS with a
bare QWindow). Counter exposure is still verified here.
"""
from __future__ import annotations

import unittest

from src.framecycler import framecycler_engine


class TestGpuChurnCounters(unittest.TestCase):
    def test_debug_stats_expose_churn_fields(self):
        renderer = framecycler_engine.RhiRenderer()
        stats = renderer.get_debug_stats()
        for key in (
            "pipeline_rebuilds",
            "srb_updates",
            "staging_waits",
            "textures_created",
            "textures_pooled_reuses",
            "last_upload_jobs",
            "upload_ms_total",
            "end_frame_ms_max",
        ):
            self.assertIn(key, stats)
            self.assertEqual(stats[key], 0)

    def test_force_null_backend_api_exists(self):
        renderer = framecycler_engine.RhiRenderer()
        self.assertTrue(hasattr(renderer, "set_force_null_backend"))
        renderer.set_force_null_backend(True)  # must not throw pre-init


if __name__ == "__main__":
    unittest.main()
