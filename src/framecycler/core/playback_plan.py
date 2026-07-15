"""Flatten an OTIO shot-stack timeline into a fast integer-frame playback plan."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, List, Optional, Sequence, Tuple

from .media_source import (
    MediaSource,
    local_index_to_decoder_frame,
)
from . import otio_model

if TYPE_CHECKING:
    import opentimelineio as otio
    from .media_pool import MediaPool


@dataclass
class VersionSlot:
    """One version inside a shot stack, with its runtime media (if online)."""

    clip: "otio.schema.Clip"
    source: Optional[MediaSource]
    is_active: bool = False
    is_compare: bool = False
    offline: bool = False


@dataclass
class Segment:
    """One shot on the global timeline."""

    index: int
    global_start: int
    global_end: int
    stack: "otio.schema.Stack"
    versions: List[VersionSlot] = field(default_factory=list)
    rate: float = 24.0

    @property
    def frame_count(self) -> int:
        return max(0, self.global_end - self.global_start + 1)

    @property
    def active(self) -> Optional[VersionSlot]:
        for version in self.versions:
            if version.is_active:
                return version
        return self.versions[0] if self.versions else None

    @property
    def compare(self) -> Optional[VersionSlot]:
        for version in self.versions:
            if version.is_compare:
                return version
        return self.active

    def display_versions(self) -> List[VersionSlot]:
        """Ordered slots for the viewport: active, compare (if different), then others."""
        if not self.versions:
            return []
        active = self.active
        compare = self.compare
        ordered: List[VersionSlot] = []
        if active is not None:
            ordered.append(active)
        if compare is not None and compare is not active:
            ordered.append(compare)
        for version in self.versions:
            if version is active or version is compare:
                continue
            ordered.append(version)
        return ordered


@dataclass
class PlaybackPlan:
    segments: List[Segment] = field(default_factory=list)
    global_start: int = 0
    global_end: int = 0

    @property
    def empty(self) -> bool:
        return not self.segments

    def segment_at(self, global_frame: int) -> Optional[Segment]:
        if not self.segments:
            return None
        if global_frame < self.segments[0].global_start:
            return self.segments[0]
        for segment in self.segments:
            if segment.global_start <= global_frame <= segment.global_end:
                return segment
        return self.segments[-1]

    def segment_index_at(self, global_frame: int) -> int:
        segment = self.segment_at(global_frame)
        return segment.index if segment is not None else 0

    def adjacent_segments(self, global_frame: int) -> List[Segment]:
        segment = self.segment_at(global_frame)
        if segment is None:
            return []
        result = [segment]
        if segment.index > 0:
            result.append(self.segments[segment.index - 1])
        if segment.index + 1 < len(self.segments):
            result.append(self.segments[segment.index + 1])
        return result

    def local_index(self, segment: Segment, global_frame: int) -> int:
        if segment.frame_count <= 0:
            return 0
        local = global_frame - segment.global_start
        return max(0, min(segment.frame_count - 1, local))

    def decoder_frame_for_version(self, segment: Segment, version: VersionSlot, global_frame: int) -> int:
        local = self.local_index(segment, global_frame)
        if version.source is None:
            clip_start = otio_model.clip_start_frame(version.clip)
            return clip_start + local
        # Clamp local to the source's own frame count (compare version may be shorter/longer)
        if version.source.frame_count > 0:
            local = max(0, min(version.source.frame_count - 1, local))
        return local_index_to_decoder_frame(version.source, local)

    def global_range_of(self, stack: "otio.schema.Stack") -> Optional[Tuple[int, int]]:
        for segment in self.segments:
            if segment.stack is stack:
                return segment.global_start, segment.global_end
        return None

    def playback_range_for_version(
        self,
        segment: Segment,
        version: VersionSlot,
        global_in: int,
        global_out: int,
    ) -> Tuple[int, int]:
        if version.source is None:
            start = otio_model.clip_start_frame(version.clip)
            return start, start
        local_in = max(0, min(version.source.frame_count - 1, global_in - segment.global_start))
        local_out = max(0, min(version.source.frame_count - 1, global_out - segment.global_start))
        if local_out < local_in:
            local_out = local_in
        return (
            local_index_to_decoder_frame(version.source, local_in),
            local_index_to_decoder_frame(version.source, local_out),
        )


def build(timeline: "otio.schema.Timeline", media_pool: "MediaPool") -> PlaybackPlan:
    stacks = otio_model.shot_stacks(timeline)
    if not stacks:
        return PlaybackPlan()

    first_clip = otio_model.active_clip(stacks[0])
    global_start = otio_model.clip_start_frame(first_clip) if first_clip is not None else 0
    cursor = global_start
    segments: List[Segment] = []

    for index, stack in enumerate(stacks):
        clips = otio_model.version_clips(stack)
        active_idx = otio_model.active_index(stack)
        compare_idx = otio_model.compare_index(stack)
        versions: List[VersionSlot] = []
        active_source: Optional[MediaSource] = None

        for clip_index, clip in enumerate(clips):
            path = otio_model.media_path_from_clip(clip)
            source = media_pool.get(path) if path else None
            offline = (
                source is None
                or otio_model.is_offline_clip(clip)
                or (source is not None and source.offline)
            )
            slot = VersionSlot(
                clip=clip,
                source=source,
                is_active=(clip_index == active_idx),
                is_compare=(clip_index == compare_idx),
                offline=bool(offline),
            )
            versions.append(slot)
            if clip_index == active_idx:
                active_source = source if source is not None and not source.offline else None

        # Segment length follows the active version (frame-conform, 1:1).
        if active_source is not None:
            frame_count = max(0, active_source.frame_count)
            rate = active_source.fps
        else:
            active = otio_model.active_clip(stack)
            frame_count = otio_model.clip_duration_frames(active) if active else 0
            rate = otio_model.clip_rate(active) if active else 24.0

        global_end = cursor + max(0, frame_count - 1) if frame_count > 0 else cursor - 1
        if frame_count <= 0:
            # Skip empty shots but keep indexing stable by still recording zero-length?
            # Prefer skipping empty segments.
            continue

        segments.append(
            Segment(
                index=len(segments),
                global_start=cursor,
                global_end=global_end,
                stack=stack,
                versions=versions,
                rate=rate,
            )
        )
        cursor = global_end + 1

    # Re-index after possible skips
    for i, segment in enumerate(segments):
        segment.index = i

    if not segments:
        return PlaybackPlan()

    return PlaybackPlan(
        segments=segments,
        global_start=segments[0].global_start,
        global_end=segments[-1].global_end,
    )
