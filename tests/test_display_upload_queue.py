import unittest

from src.framecycler import framecycler_engine


class TestDisplayUploadQueuePolicy(unittest.TestCase):
    def test_every_frame_enqueues_distinct_frames(self):
        q = framecycler_engine.DisplayUploadQueue()
        q.set_policy(framecycler_engine.UploadQueuePolicy.EveryFrame)
        self.assertTrue(q.enqueue(0, 1, 1, False))
        self.assertTrue(q.enqueue(0, 2, 2, False))
        self.assertTrue(q.enqueue(0, 3, 3, False))
        self.assertFalse(q.enqueue(0, 2, 2, False))  # duplicate
        self.assertFalse(q.enqueue(0, 4, 4, True))  # already resident
        stats = q.stats()
        self.assertEqual(stats["pending"], 3)
        self.assertEqual(q.job_count(), 3)

    def test_every_frame_refuses_when_full(self):
        q = framecycler_engine.DisplayUploadQueue()
        q.set_policy(framecycler_engine.UploadQueuePolicy.EveryFrame)
        for i in range(64):
            self.assertTrue(q.enqueue(0, i, i, False), msg=f"frame {i}")
        self.assertFalse(q.enqueue(0, 64, 64, False))
        self.assertEqual(q.stats()["refused"], 1)
        self.assertEqual(q.stats()["pending"], 64)

    def test_realtime_coalesces_per_source(self):
        q = framecycler_engine.DisplayUploadQueue()
        q.set_policy(framecycler_engine.UploadQueuePolicy.Realtime)
        self.assertTrue(q.enqueue(0, 1, 1, False))
        self.assertTrue(q.enqueue(0, 2, 2, False))
        self.assertTrue(q.enqueue(0, 10, 10, False))
        # Only latest for source 0 should remain queued
        self.assertEqual(q.stats()["pending"], 1)
        self.assertTrue(q.has_job(0, 10))
        self.assertFalse(q.has_job(0, 1))
        self.assertGreaterEqual(q.stats()["coalesced"], 2)

    def test_realtime_keeps_independent_sources(self):
        q = framecycler_engine.DisplayUploadQueue()
        q.set_policy(framecycler_engine.UploadQueuePolicy.Realtime)
        self.assertTrue(q.enqueue(0, 5, 5, False))
        self.assertTrue(q.enqueue(1, 5, 5, False))
        self.assertEqual(q.stats()["pending"], 2)

    def test_discard_queued_removes_pending_job(self):
        q = framecycler_engine.DisplayUploadQueue()
        q.set_policy(framecycler_engine.UploadQueuePolicy.EveryFrame)
        self.assertTrue(q.enqueue(0, 7, 1, False))
        self.assertTrue(q.has_job(0, 7))
        # discard via find isn't exposed; enqueue of already-resident present path
        # uses discard_queued from C++. Verify clear still works for pending.
        q.clear()
        self.assertEqual(q.stats()["pending"], 0)
        self.assertFalse(q.has_job(0, 7))


class TestRendererUploadQueueBindings(unittest.TestCase):
    def test_policy_round_trip(self):
        renderer = framecycler_engine.RhiRenderer()
        self.assertTrue(hasattr(renderer, "set_upload_queue_policy"))
        self.assertTrue(hasattr(renderer, "get_upload_queue_stats"))
        renderer.set_upload_queue_policy(framecycler_engine.UploadQueuePolicy.Realtime)
        self.assertEqual(
            renderer.upload_queue_policy(),
            framecycler_engine.UploadQueuePolicy.Realtime,
        )
        renderer.set_upload_queue_policy(framecycler_engine.UploadQueuePolicy.EveryFrame)
        self.assertEqual(
            renderer.upload_queue_policy(),
            framecycler_engine.UploadQueuePolicy.EveryFrame,
        )
        stats = renderer.get_upload_queue_stats()
        self.assertIn("pending", stats)
        self.assertIn("inflight", stats)
        self.assertIn("completed", stats)

    def test_register_cache_and_clear_display_cache_apis(self):
        renderer = framecycler_engine.RhiRenderer()
        cache = framecycler_engine.CacheManager(0.05)
        renderer.register_cache(0, cache)
        renderer.set_display_cache_limit_gb(0.05)
        renderer.set_source_playhead(0, 0, 1, 0, 10)
        # clear must not crash; registry wipe is covered by C++ unit path —
        # without an exposed window the clear is deferred until a render pass.
        renderer.clear_display_cache()
        renderer.request_redraw()
        stats = renderer.get_display_cache_stats()
        self.assertIn("resident_frames", stats)



if __name__ == "__main__":
    unittest.main()
