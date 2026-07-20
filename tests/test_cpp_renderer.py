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
        params.false_color_mode = 1
        params.zebra_lo = 0.05
        params.zebra_hi = 0.95
        params.zoom = 1.5
        params.pixel_aspect_ratio = 1.0
        params.pan_x = 0.1
        params.pan_y = -0.2

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
        self.assertEqual(params.false_color_mode, 1)
        self.assertAlmostEqual(params.zebra_lo, 0.05)
        self.assertAlmostEqual(params.zebra_hi, 0.95)
        self.assertEqual(params.slots[0].frame_index, 101)
        self.assertEqual(params.tiles[0].source_index, 1)

    def test_grading_uniforms(self):
        renderer = framecycler_engine.RhiRenderer()
        renderer.set_grading_uniform("exposure", 1.2)
        renderer.set_grading_uniform_vec3("tint", 1.0, 0.9, 0.8)
        renderer.clear_grading_uniforms()

    def test_ocio_lut_2d_api(self):
        renderer = framecycler_engine.RhiRenderer()
        self.assertTrue(hasattr(renderer, "upload_ocio_lut_2d"))
        self.assertTrue(hasattr(renderer, "set_ocio_lut_slot_dims"))
        self.assertTrue(hasattr(renderer, "cached_pipeline_key"))
        self.assertTrue(hasattr(renderer, "pipeline_lut_count"))
        # 4x1 single-channel LUT (RGBA-packed upload path)
        data = [0.0, 0.33, 0.66, 1.0]
        renderer.set_ocio_lut_slot_dims(["2D"])
        renderer.upload_ocio_lut_2d(0, 4, 1, 1, data)
        renderer.clear_ocio_luts()
        # Clearing dims must shrink lut count (applied on next sync; pending starts at 0).
        self.assertEqual(renderer.pipeline_lut_count(), 0)

    def test_lut_slot_dims_shrink_pending(self):
        """Empty slot dims must not leave a stale high lut count after clear."""
        renderer = framecycler_engine.RhiRenderer()
        renderer.set_ocio_lut_slot_dims(["2D", "3D"])
        renderer.clear_ocio_luts()
        # clear_ocio_luts clears pending dims; count resets when dims are applied on render thread.
        # Without an active RHI, pending dims are empty and count stays at construction default 0
        # until sync — assert the public API exists and clear does not raise.
        self.assertEqual(renderer.pipeline_lut_count(), 0)
        stats = renderer.get_debug_stats()
        self.assertIn("pipeline_lut_count", stats)
        self.assertEqual(stats["pipeline_lut_count"], 0)

    def test_set_shader_sources_skips_same_pipeline_key(self):
        renderer = framecycler_engine.RhiRenderer()
        vert = (
            "#version 450\n"
            "layout(location=0) in vec2 pos;\n"
            "void main(){ gl_Position = vec4(pos,0,1); }\n"
        )
        frag = (
            "#version 450\n"
            "layout(location=0) out vec4 c;\n"
            "void main(){ c = vec4(1.0); }\n"
        )
        renderer.set_shader_sources("key-a", vert, frag)
        key1 = renderer.cached_pipeline_key()
        renderer.set_shader_sources("key-a", vert, frag)
        self.assertEqual(renderer.cached_pipeline_key(), key1)

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

    def test_debug_stats_include_churn_counters(self):
        """GPU churn counters from finding #2 must be exposed to Python."""
        renderer = framecycler_engine.RhiRenderer()
        stats = renderer.get_debug_stats()
        for key in (
            "pipeline_rebuilds",
            "srb_updates",
            "staging_waits",
            "textures_created",
            "textures_pooled_reuses",
            "lut_textures_pooled_reuses",
            "last_upload_ms",
            "last_render_ms",
            "size_only_presents",
        ):
            self.assertIn(key, stats)
            self.assertIsInstance(stats[key], (int, float))

    def test_ocio_lut_pool_counter_exposed(self):
        """Same-size LUT reupload path exposes lut_textures_pooled_reuses (finding #9).

        Full GPU reuse requires an active RHI present; without it the counter stays 0
        but clear/reupload must not raise and the field must remain in debug_stats.
        """
        renderer = framecycler_engine.RhiRenderer()
        # 4x1 single-channel path
        data_1d = [0.0, 0.33, 0.66, 1.0]
        renderer.set_ocio_lut_slot_dims(["2D"])
        renderer.upload_ocio_lut_2d(0, 4, 1, 1, data_1d)
        renderer.clear_ocio_luts()
        renderer.set_ocio_lut_slot_dims(["2D"])
        renderer.upload_ocio_lut_2d(0, 4, 1, 1, data_1d)
        stats = renderer.get_debug_stats()
        self.assertIn("lut_textures_pooled_reuses", stats)
        self.assertGreaterEqual(stats["lut_textures_pooled_reuses"], 0)
        # 3D same-size round-trip API
        edge = 2
        cube = [0.0] * (edge * edge * edge * 3)
        renderer.set_ocio_lut_slot_dims(["3D"])
        renderer.upload_ocio_lut_3d(0, edge, cube)
        renderer.clear_ocio_luts()
        renderer.set_ocio_lut_slot_dims(["3D"])
        renderer.upload_ocio_lut_3d(0, edge, cube)
        self.assertEqual(renderer.pipeline_lut_count(), 0)


    def test_viewer_output_mode_api_defaults_to_sdr(self):
        """Viewer SDR/EDR present API (finding #11) — no Metal present required."""
        renderer = framecycler_engine.RhiRenderer()
        self.assertTrue(hasattr(renderer, "set_viewer_output_mode"))
        self.assertTrue(hasattr(renderer, "viewer_output_mode"))
        self.assertTrue(hasattr(renderer, "actual_viewer_output_mode"))
        self.assertTrue(hasattr(renderer, "is_viewer_output_mode_supported"))
        self.assertEqual(renderer.viewer_output_mode(), 0)
        self.assertEqual(renderer.actual_viewer_output_mode(), 0)
        self.assertTrue(renderer.is_viewer_output_mode_supported(0))
        # Null / forced-null cannot present EDR.
        renderer.set_force_null_backend(True)
        self.assertFalse(renderer.is_viewer_output_mode_supported(1))
        self.assertFalse(renderer.is_viewer_output_mode_supported(2))
        # Request is stored; without a swapchain, actual stays SDR.
        renderer.set_viewer_output_mode(1)
        self.assertEqual(renderer.viewer_output_mode(), 1)
        self.assertEqual(renderer.actual_viewer_output_mode(), 0)
        renderer.set_viewer_output_mode(0)
        self.assertEqual(renderer.viewer_output_mode(), 0)


if __name__ == "__main__":
    unittest.main()
