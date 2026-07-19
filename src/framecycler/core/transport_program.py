"""Build C++ TransportProgram snapshots from the Python session/plan."""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from .playback_timing import (
    PLAYBACK_TIMING_REALTIME,
    normalize_playback_timing,
)
from .media_source import local_index_to_decoder_frame
from . import otio_model

if TYPE_CHECKING:
    from .playback_plan import PlaybackPlan, Segment, VersionSlot
    from .. import framecycler_engine as engine_type


def _loop_mode(engine, loop_mode: str):
    if loop_mode == "bounce":
        return engine.TransportLoopMode.Bounce
    if loop_mode == "once":
        return engine.TransportLoopMode.Once
    return engine.TransportLoopMode.Loop


def _timing_mode(engine, timing: str):
    if normalize_playback_timing(timing) == PLAYBACK_TIMING_REALTIME:
        return engine.TransportTimingMode.Realtime
    return engine.TransportTimingMode.EveryFrame


def build_slot_mapping(
    engine,
    plan: "PlaybackPlan",
    segment: "Segment",
    version: "VersionSlot",
    source_index: int,
    global_in: int,
    global_out: int,
):
    """Map one viewport slot onto decoder frames for the active segment."""
    mapping = engine.TransportSlotMapping()
    mapping.source_index = int(source_index)
    mapping.segment_global_start = int(segment.global_start)
    mapping.segment_global_end = int(segment.global_end)
    play_in, play_out = plan.playback_range_for_version(
        segment, version, global_in, global_out
    )
    mapping.playback_in = int(play_in)
    mapping.playback_out = int(play_out)

    if version.source is None:
        mapping.decoder_start_frame = 0
        return mapping

    src_offset = otio_model.clip_source_start_frames(version.clip)
    # Prefer an explicit table so non-contiguous EXR frame numbers stay correct.
    frames = []
    for local in range(max(0, segment.frame_count)):
        global_frame = segment.global_start + local
        frames.append(int(plan.decoder_frame_for_version(segment, version, global_frame)))
    if frames:
        mapping.decoder_frames = frames
        mapping.decoder_start_frame = frames[0]
    else:
        mapping.decoder_start_frame = int(
            local_index_to_decoder_frame(version.source, src_offset)
        )
    return mapping


def build_transport_program(
    engine,
    *,
    plan: "PlaybackPlan",
    segment: Optional["Segment"],
    current_frame: int,
    direction: int,
    fps: float,
    in_point: int,
    out_point: int,
    loop_mode: str,
    playback_timing: str,
    playing: bool,
):
    """Construct a TransportProgram for the active shot segment."""
    prog = engine.TransportProgram()
    prog.playing = bool(playing)
    prog.direction = 1 if direction >= 0 else -1
    prog.fps = float(fps) if fps > 0 else 24.0
    prog.in_point = int(in_point)
    prog.out_point = int(out_point)
    prog.loop_mode = _loop_mode(engine, loop_mode)
    prog.timing_mode = _timing_mode(engine, playback_timing)
    prog.current_frame = int(current_frame)

    if segment is None or plan.empty:
        prog.segment_global_start = prog.in_point
        prog.segment_global_end = prog.out_point
        prog.hold_at_segment_bounds = False
        return prog

    prog.segment_global_start = int(segment.global_start)
    prog.segment_global_end = int(segment.global_end)
    # Pause for Python when the playhead would leave this shot while in/out
    # still continues across neighboring shots.
    prog.hold_at_segment_bounds = (
        segment.global_start > in_point or segment.global_end < out_point
    )

    slots = []
    for index, version in enumerate(segment.display_versions()):
        if version.source is None or version.source.cache is None or version.offline:
            continue
        slots.append(
            build_slot_mapping(
                engine,
                plan,
                segment,
                version,
                index,
                in_point,
                out_point,
            )
        )
    prog.slots = slots
    return prog
