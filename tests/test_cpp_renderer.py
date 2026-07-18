import unittest
from src.framecycler import framecycler_engine


class TestCppRenderer(unittest.TestCase):
    def test_renderer_instantiation(self):
        renderer = framecycler_engine.RhiRenderer()
        self.assertIsNotNone(renderer)

    def test_render_params_and_specs(self):
        params = framecycler_engine.RenderParams()
        params.compare_mode = 1
        params.wipe_pos = 0.75
        params.channel_mask = 2
        params.scale_x = 1.5
        params.scale_y = 1.5

        slot = framecycler_engine.FrameSlotSpec()
        slot.source_index = 0
        slot.frame_index = 101
        slot.upload_token = 5
        params.slots = [slot]

        tile = framecycler_engine.TileSpec()
        tile.source_index = 1
        tile.scale_x = 0.5
        tile.scale_y = 0.5
        tile.offset_x = -0.5
        tile.offset_y = 0.5
        params.tiles = [tile]

        self.assertEqual(params.compare_mode, 1)
        self.assertAlmostEqual(params.wipe_pos, 0.75)
        self.assertEqual(params.slots[0].frame_index, 101)
        self.assertEqual(params.tiles[0].source_index, 1)

    def test_grading_uniforms(self):
        renderer = framecycler_engine.RhiRenderer()
        renderer.set_grading_uniform("exposure", 1.2)
        renderer.set_grading_uniform_vec3("tint", 1.0, 0.9, 0.8)
        renderer.clear_grading_uniforms()

    def test_request_redraw_is_bound(self):
        renderer = framecycler_engine.RhiRenderer()
        self.assertTrue(hasattr(renderer, "request_redraw"))
        self.assertTrue(hasattr(renderer, "sync_and_render"))
        self.assertTrue(hasattr(renderer, "set_display_cache_limit_gb"))
        self.assertTrue(hasattr(renderer, "get_display_cache_stats"))
        self.assertTrue(hasattr(renderer, "set_upload_queue_policy"))
        self.assertTrue(hasattr(renderer, "get_upload_queue_stats"))
        renderer.request_redraw()
        renderer.set_upload_queue_policy(framecycler_engine.UploadQueuePolicy.EveryFrame)
        stats = renderer.get_upload_queue_stats()
        self.assertEqual(stats["pending"], 0)


if __name__ == "__main__":
    unittest.main()
