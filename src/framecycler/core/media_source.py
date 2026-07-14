from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, List, Tuple

if TYPE_CHECKING:
    from ..decoders.base import BaseDecoder
    from .cache import CacheEngine


@dataclass
class MediaSource:
    path: str
    decoder: BaseDecoder
    cache: CacheEngine
    frame_count: int
    fps: float
    timeline_offset: int = 0
    decoder_start_frame: int = 0
    width: int = 0
    height: int = 0
    pixel_aspect_ratio: float = 1.0
    resolution_scale: float = 1.0
    metadata: dict = field(default_factory=dict)

    @property
    def display_name(self) -> str:
        return os.path.basename(self.path)

    @property
    def timeline_end(self) -> int:
        return self.timeline_offset + max(0, self.frame_count - 1)


def _decoder_frame_numbers(source: MediaSource) -> List[int] | None:
    frame_numbers = getattr(source.decoder, "frame_numbers", None)
    if frame_numbers:
        return list(frame_numbers)
    return None


def local_index_to_decoder_frame(source: MediaSource, local_index: int) -> int:
    """Map a 0-based index within a source segment to the decoder frame number."""
    if source.frame_count <= 0:
        return source.decoder_start_frame
    local_index = max(0, min(source.frame_count - 1, local_index))
    frame_numbers = _decoder_frame_numbers(source)
    if frame_numbers and local_index < len(frame_numbers):
        return frame_numbers[local_index]
    return source.decoder_start_frame + local_index


def decoder_frame_to_local_index(source: MediaSource, decoder_frame: int) -> int:
    frame_numbers = _decoder_frame_numbers(source)
    if frame_numbers:
        try:
            return frame_numbers.index(decoder_frame)
        except ValueError:
            closest = min(range(len(frame_numbers)), key=lambda i: abs(frame_numbers[i] - decoder_frame))
            return closest
    return max(0, min(source.frame_count - 1, decoder_frame - source.decoder_start_frame))


def rebuild_timeline_offsets(sources: List[MediaSource]) -> int:
    """Recompute timeline offsets and return total concatenated frame count."""
    if not sources:
        return 0
    offset = sources[0].decoder_start_frame
    for source in sources:
        source.timeline_offset = offset
        offset += max(0, source.frame_count)
    return offset


def total_frame_count(sources: List[MediaSource]) -> int:
    return sum(max(0, source.frame_count) for source in sources)


def global_to_local(sources: List[MediaSource], global_frame: int) -> Tuple[int, int]:
    """Map a global timeline frame to (source_index, local_frame)."""
    if not sources:
        return 0, 0

    for index, source in enumerate(sources):
        if source.frame_count <= 0:
            continue
        segment_end = source.timeline_offset + source.frame_count - 1
        if global_frame < source.timeline_offset:
            return index, 0
        if global_frame <= segment_end:
            return index, global_frame - source.timeline_offset

    last_index = len(sources) - 1
    last_source = sources[last_index]
    return last_index, max(0, last_source.frame_count - 1)


def local_to_global(sources: List[MediaSource], source_index: int, local_frame: int) -> int:
    if not sources or source_index < 0 or source_index >= len(sources):
        return 0
    source = sources[source_index]
    local_frame = max(0, min(max(0, source.frame_count - 1), local_frame))
    return source.timeline_offset + local_frame


def local_frame_for_source(sources: List[MediaSource], source_index: int, global_frame: int) -> int:
    """Return the 0-based local index for a source at a global playhead (clamped)."""
    if not sources or source_index < 0 or source_index >= len(sources):
        return 0
    source = sources[source_index]
    if source.frame_count <= 0:
        return 0
    local = global_frame - source.timeline_offset
    return max(0, min(source.frame_count - 1, local))


def decoder_frame_for_source(sources: List[MediaSource], source_index: int, global_frame: int) -> int:
    """Return the decoder frame number for a source at a global playhead."""
    local_index = local_frame_for_source(sources, source_index, global_frame)
    return local_index_to_decoder_frame(sources[source_index], local_index)


def local_playback_range(
    sources: List[MediaSource],
    source_index: int,
    global_in: int,
    global_out: int,
) -> Tuple[int, int]:
    """Map global in/out points to decoder frame numbers for cache prefetch."""
    if not sources or source_index < 0 or source_index >= len(sources):
        return 0, 0
    source = sources[source_index]
    if source.frame_count <= 0:
        return source.decoder_start_frame, source.decoder_start_frame
    local_in = max(0, min(source.frame_count - 1, global_in - source.timeline_offset))
    local_out = max(0, min(source.frame_count - 1, global_out - source.timeline_offset))
    if local_out < local_in:
        local_out = local_in
    return (
        local_index_to_decoder_frame(source, local_in),
        local_index_to_decoder_frame(source, local_out),
    )
