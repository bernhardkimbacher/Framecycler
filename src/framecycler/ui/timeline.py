"""Back-compat export: Timeline is the NLE TimelineEditor."""

from .timeline_editor import (
    TimelineEditor as Timeline,
    TimelineSegmentInfo,
    TimelineVersionInfo,
    active_index_for_stack_offset,
    stack_offset_for_active,
)

__all__ = [
    "Timeline",
    "TimelineSegmentInfo",
    "TimelineVersionInfo",
    "active_index_for_stack_offset",
    "stack_offset_for_active",
]
