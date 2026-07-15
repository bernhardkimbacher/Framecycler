import os
import tempfile
import unittest

from src.framecycler.core import otio_model
from src.framecycler.core.media_source import (
    MediaSource,
    decoder_frame_to_local_index,
    local_index_to_decoder_frame,
)
from src.framecycler.core.playback_plan import PlaybackPlan, Segment, VersionSlot, build
from src.framecycler.core.settings import Settings
from src.framecycler.core.session import Session


class _StubDecoder:
    def __init__(self, frame_numbers=None, start_frame=0):
        self.frame_numbers = frame_numbers or []
        self.start_frame = start_frame

    def get_metadata(self):
        return {}


class _StubCache:
    def close(self):
        pass

    def add_frame_ready_callback(self, callback):
        pass


def _make_source(
    frame_count: int,
    path: str = "shot.exr",
    *,
    frame_numbers: list[int] | None = None,
    decoder_start_frame: int = 0,
) -> MediaSource:
    if frame_numbers is None and decoder_start_frame:
        frame_numbers = [decoder_start_frame + i for i in range(frame_count)]
    decoder = _StubDecoder(frame_numbers=frame_numbers or [], start_frame=decoder_start_frame)
    return MediaSource(
        path=path,
        decoder=decoder,
        cache=_StubCache(),
        frame_count=frame_count,
        fps=24.0,
        decoder_start_frame=decoder_start_frame or (frame_numbers[0] if frame_numbers else 0),
    )


class _FakePool:
    """Minimal media pool stand-in keyed by path."""

    def __init__(self, sources: dict[str, MediaSource]):
        self._sources = {os.path.abspath(k) if not k.startswith("/") else k: v for k, v in sources.items()}
        # Also keep original keys
        self._sources.update(sources)

    def get(self, path: str):
        if path in self._sources:
            return self._sources[path]
        abs_path = os.path.abspath(path)
        return self._sources.get(abs_path)


def _plan_from_sources(sources: list[MediaSource]) -> PlaybackPlan:
    timeline = otio_model.new_timeline()
    pool_map = {}
    for source in sources:
        clip = otio_model.clip_from_media(
            source.path,
            {
                "fps": source.fps,
                "frame_count": source.frame_count,
                "start_frame": source.decoder_start_frame,
                "width": source.width,
                "height": source.height,
                "pixel_aspect_ratio": source.pixel_aspect_ratio,
            },
        )
        otio_model.append_shot(timeline, clip)
        pool_map[source.path] = source
        pool_map[os.path.abspath(source.path)] = source
    return build(timeline, _FakePool(pool_map))


class TestPlaybackPlanMapping(unittest.TestCase):
    def test_plan_segments_and_range(self):
        sources = [
            _make_source(10, "a.exr", decoder_start_frame=0),
            _make_source(5, "b.exr", decoder_start_frame=0),
        ]
        plan = _plan_from_sources(sources)
        self.assertEqual(len(plan.segments), 2)
        self.assertEqual(plan.global_start, 0)
        self.assertEqual(plan.global_end, 14)
        self.assertEqual(plan.segments[0].global_start, 0)
        self.assertEqual(plan.segments[1].global_start, 10)

    def test_segment_at_within_and_past(self):
        sources = [
            _make_source(10, "a.exr"),
            _make_source(5, "b.exr"),
        ]
        plan = _plan_from_sources(sources)
        self.assertEqual(plan.segment_at(0).index, 0)
        self.assertEqual(plan.segment_at(9).index, 0)
        self.assertEqual(plan.segment_at(10).index, 1)
        self.assertEqual(plan.segment_at(12).index, 1)
        self.assertEqual(plan.segment_at(99).index, 1)

    def test_local_index_clamps(self):
        sources = [
            _make_source(10, "a.exr"),
            _make_source(5, "b.exr"),
        ]
        plan = _plan_from_sources(sources)
        self.assertEqual(plan.local_index(plan.segments[0], 12), 9)
        self.assertEqual(plan.local_index(plan.segments[1], 3), 0)

    def test_decoder_frame_mapping_for_high_start_numbers(self):
        sources = [
            _make_source(3, "a.exr", frame_numbers=[108000, 108001, 108002], decoder_start_frame=108000),
            _make_source(2, "b.exr", frame_numbers=[200, 201], decoder_start_frame=200),
        ]
        plan = _plan_from_sources(sources)
        # Global timeline is anchored at first clip start (108000)
        self.assertEqual(plan.global_start, 108000)
        seg0 = plan.segments[0]
        seg1 = plan.segments[1]
        self.assertEqual(
            plan.decoder_frame_for_version(seg0, seg0.active, 108000),
            108000,
        )
        self.assertEqual(
            plan.decoder_frame_for_version(seg0, seg0.active, 108002),
            108002,
        )
        self.assertEqual(
            plan.decoder_frame_for_version(seg1, seg1.active, 108003),
            200,
        )
        self.assertEqual(decoder_frame_to_local_index(sources[0], 108001), 1)
        self.assertEqual(local_index_to_decoder_frame(sources[0], 1), 108001)

    def test_playback_range_maps_global_in_out(self):
        sources = [
            _make_source(10, "a.exr", frame_numbers=[108000 + i for i in range(10)], decoder_start_frame=108000),
            _make_source(10, "b.exr", frame_numbers=[200 + i for i in range(10)], decoder_start_frame=200),
        ]
        plan = _plan_from_sources(sources)
        seg0 = plan.segments[0]
        seg1 = plan.segments[1]
        self.assertEqual(
            plan.playback_range_for_version(seg0, seg0.active, 108002, 108012),
            (108002, 108009),
        )
        self.assertEqual(
            plan.playback_range_for_version(seg1, seg1.active, 108012, 108015),
            (202, 205),
        )


class TestOtioModel(unittest.TestCase):
    def test_version_stack_and_roundtrip(self):
        timeline = otio_model.new_timeline()
        clip_a = otio_model.clip_from_media(
            "/tmp/a.mov", {"fps": 24.0, "frame_count": 10, "start_frame": 1001}
        )
        stack = otio_model.append_shot(timeline, clip_a)
        clip_b = otio_model.clip_from_media(
            "/tmp/b.mov", {"fps": 24.0, "frame_count": 8, "start_frame": 1001}
        )
        otio_model.add_version(stack, clip_b, make_active=True)
        self.assertEqual(otio_model.active_index(stack), 1)
        self.assertEqual(otio_model.compare_index(stack), 0)

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "session.otio")
            otio_model.save_timeline(timeline, path)
            loaded = otio_model.load_timeline(path)
            stacks = otio_model.shot_stacks(loaded)
            self.assertEqual(len(stacks), 1)
            self.assertEqual(len(otio_model.version_clips(stacks[0])), 2)
            self.assertEqual(otio_model.active_index(stacks[0]), 1)


if __name__ == "__main__":
    unittest.main()
