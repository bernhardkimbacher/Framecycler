import unittest

from src.framecycler.ui.timeline_editor import (
    LANE_H,
    TimelineEditor,
    active_index_for_stack_offset,
    stack_offset_for_active,
)


class TestStackOffsetMapping(unittest.TestCase):
    def test_rest_offset_places_active_on_display_lane(self):
        for active in range(5):
            offset = stack_offset_for_active(active)
            self.assertEqual(active_index_for_stack_offset(offset, 5), active)

    def test_drag_snap_selects_neighbor(self):
        base = stack_offset_for_active(1)
        # Drag up by roughly one lane → higher index
        self.assertEqual(
            active_index_for_stack_offset(base - LANE_H, 4),
            2,
        )
        # Drag down → lower index
        self.assertEqual(
            active_index_for_stack_offset(base + LANE_H, 4),
            0,
        )

    def test_clamps_to_version_count(self):
        self.assertEqual(active_index_for_stack_offset(-1000, 3), 2)
        self.assertEqual(active_index_for_stack_offset(1000, 3), 0)
        self.assertEqual(active_index_for_stack_offset(0, 0), 0)


class TestCacheFrameCoalesce(unittest.TestCase):
    def test_coalesce_empty(self):
        self.assertEqual(TimelineEditor.coalesce_frame_runs(set()), [])

    def test_coalesce_runs(self):
        frames = {1, 2, 3, 10, 11, 20}
        self.assertEqual(
            TimelineEditor.coalesce_frame_runs(frames),
            [(1, 3), (10, 11), (20, 20)],
        )


class TestDisplayCacheBinding(unittest.TestCase):
    def test_get_display_cached_frames_exists(self):
        from src.framecycler import framecycler_engine

        renderer = framecycler_engine.RhiRenderer()
        self.assertTrue(hasattr(renderer, "get_display_cached_frames"))
        self.assertEqual(list(renderer.get_display_cached_frames(0)), [])


if __name__ == "__main__":
    unittest.main()
