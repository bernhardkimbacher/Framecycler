import os
import tempfile
import unittest

import opentimelineio as otio

from src.framecycler.core import otio_model
from src.framecycler.core.playback_plan import build as build_plan
from src.framecycler.core.session import Session
from src.framecycler.core.settings import Settings


def _clip(path: str, frames: int = 20) -> otio.schema.Clip:
    return otio_model.clip_from_media(
        path,
        {
            "fps": 24.0,
            "frame_count": frames,
            "start_frame": 1001,
            "width": 64,
            "height": 32,
            "pixel_aspect_ratio": 1.0,
        },
    )


class TestOtioTrim(unittest.TestCase):
    def test_trim_clamps_and_updates_duration(self):
        timeline = otio_model.new_timeline()
        clip = _clip("/tmp/plate_trim.exr", frames=20)
        stack = otio_model.append_shot(timeline, clip)

        self.assertTrue(otio_model.trim_active_version(stack, 5, 8))
        self.assertEqual(otio_model.clip_source_start_frames(clip), 5)
        self.assertEqual(otio_model.clip_duration_frames(clip), 8)

        # Clamp past available end
        self.assertTrue(otio_model.trim_active_version(stack, 18, 50))
        self.assertEqual(otio_model.clip_source_start_frames(clip), 18)
        self.assertEqual(otio_model.clip_duration_frames(clip), 2)

        # Clamp negative / zero duration to at least 1
        self.assertTrue(otio_model.trim_active_version(stack, -10, 0))
        self.assertEqual(otio_model.clip_source_start_frames(clip), 0)
        self.assertEqual(otio_model.clip_duration_frames(clip), 1)

    def test_plan_segment_length_follows_trim(self):
        timeline = otio_model.new_timeline()
        clip = _clip("/tmp/plate_plan.exr", frames=30)
        stack = otio_model.append_shot(timeline, clip)
        otio_model.trim_active_version(stack, 2, 10)

        class _EmptyPool:
            def get(self, path):
                return None

        plan = build_plan(timeline, _EmptyPool())
        self.assertEqual(len(plan.segments), 1)
        self.assertEqual(plan.segments[0].frame_count, 10)

    def test_session_trim_active_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(config_dir=tmp)
            session = Session(settings)
            # Avoid real media decode: patch media pool acquire path via add through OTIO
            clip = _clip(os.path.join(tmp, "v.exr"), frames=16)
            otio_model.append_shot(session.timeline, clip)
            session._notify()
            self.assertEqual(session.plan.segments[0].frame_count, 16)

            session.trim_active_version(0, 4, 6)
            self.assertEqual(otio_model.clip_source_start_frames(clip), 4)
            self.assertEqual(otio_model.clip_duration_frames(clip), 6)
            self.assertEqual(session.plan.segments[0].frame_count, 6)


if __name__ == "__main__":
    unittest.main()
