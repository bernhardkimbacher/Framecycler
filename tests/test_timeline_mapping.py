import unittest

from src.framecycler.core.media_source import (
    MediaSource,
    decoder_frame_for_source,
    decoder_frame_to_local_index,
    global_to_local,
    local_frame_for_source,
    local_index_to_decoder_frame,
    local_playback_range,
    rebuild_timeline_offsets,
    total_frame_count,
)


class _StubDecoder:
    def __init__(self, frame_numbers=None, start_frame=0):
        self.frame_numbers = frame_numbers or []
        self.start_frame = start_frame

    def get_metadata(self):
        return {}


class _StubCache:
    def close(self):
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


class TestTimelineMapping(unittest.TestCase):
    def test_rebuild_offsets_and_total(self):
        sources = [_make_source(10, "a.exr"), _make_source(5, "b.exr")]
        total = rebuild_timeline_offsets(sources)
        self.assertEqual(total, 15)
        self.assertEqual(sources[0].timeline_offset, 0)
        self.assertEqual(sources[1].timeline_offset, 10)
        self.assertEqual(total_frame_count(sources), 15)

    def test_global_to_local_within_and_past_segments(self):
        sources = [_make_source(10, "a.exr"), _make_source(5, "b.exr")]
        rebuild_timeline_offsets(sources)

        self.assertEqual(global_to_local(sources, 0), (0, 0))
        self.assertEqual(global_to_local(sources, 9), (0, 9))
        self.assertEqual(global_to_local(sources, 10), (1, 0))
        self.assertEqual(global_to_local(sources, 12), (1, 2))
        self.assertEqual(global_to_local(sources, 99), (1, 4))

    def test_local_frame_for_source_clamps(self):
        sources = [_make_source(10, "a.exr"), _make_source(5, "b.exr")]
        rebuild_timeline_offsets(sources)

        self.assertEqual(local_frame_for_source(sources, 0, 12), 9)
        self.assertEqual(local_frame_for_source(sources, 1, 3), 0)

    def test_decoder_frame_mapping_for_high_start_numbers(self):
        sources = [
            _make_source(3, "a.exr", frame_numbers=[108000, 108001, 108002]),
            _make_source(2, "b.exr", frame_numbers=[200, 201]),
        ]
        rebuild_timeline_offsets(sources)

        self.assertEqual(decoder_frame_for_source(sources, 0, 0), 108000)
        self.assertEqual(decoder_frame_for_source(sources, 0, 2), 108002)
        self.assertEqual(decoder_frame_for_source(sources, 1, 3), 200)
        self.assertEqual(decoder_frame_to_local_index(sources[0], 108001), 1)
        self.assertEqual(local_index_to_decoder_frame(sources[0], 1), 108001)

    def test_local_playback_range_maps_global_in_out_to_decoder_frames(self):
        sources = [
            _make_source(10, "a.exr", frame_numbers=[108000 + i for i in range(10)]),
            _make_source(10, "b.exr", frame_numbers=[200 + i for i in range(10)]),
        ]
        rebuild_timeline_offsets(sources)

        self.assertEqual(local_playback_range(sources, 0, 2, 12), (108002, 108009))
        self.assertEqual(local_playback_range(sources, 1, 12, 15), (202, 205))


if __name__ == "__main__":
    unittest.main()
