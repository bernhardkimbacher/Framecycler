"""Playback timing helpers: every-frame vs realtime step selection."""

from __future__ import annotations

from dataclasses import dataclass

PLAYBACK_TIMING_EVERY_FRAME = "every_frame"
PLAYBACK_TIMING_REALTIME = "realtime"
VALID_PLAYBACK_TIMINGS = frozenset(
    {PLAYBACK_TIMING_EVERY_FRAME, PLAYBACK_TIMING_REALTIME}
)


def normalize_playback_timing(value: str | None) -> str:
    if value in VALID_PLAYBACK_TIMINGS:
        return str(value)
    return PLAYBACK_TIMING_EVERY_FRAME


@dataclass(frozen=True)
class PlaybackAdvanceResult:
    """Result of advancing playback by one or more steps."""

    frame: int | None
    direction: int
    stop: bool


def advance_playback(
    current_frame: int,
    direction: int,
    steps: int,
    in_point: int,
    out_point: int,
    loop_mode: str,
) -> PlaybackAdvanceResult:
    """Advance ``steps`` frames applying loop / bounce / once bounds.

    Returns ``frame=None`` when no movement should occur (``steps <= 0``) or when
    ``loop_mode == "once"`` hits the end (``stop=True``).
    """
    if steps <= 0:
        return PlaybackAdvanceResult(frame=None, direction=direction, stop=False)

    frame = int(current_frame)
    direction = 1 if direction >= 0 else -1
    in_point = int(in_point)
    out_point = int(out_point)
    if out_point < in_point:
        in_point, out_point = out_point, in_point

    for _ in range(int(steps)):
        next_frame = frame + direction
        if direction > 0 and next_frame > out_point:
            if loop_mode == "loop":
                next_frame = in_point
            elif loop_mode == "bounce":
                direction = -1
                next_frame = out_point - 1 if out_point > in_point else in_point
            else:
                # Stay on the last in-range frame, then stop.
                return PlaybackAdvanceResult(frame=frame, direction=direction, stop=True)
        elif direction < 0 and next_frame < in_point:
            if loop_mode == "loop":
                next_frame = out_point
            elif loop_mode == "bounce":
                direction = 1
                next_frame = in_point + 1 if out_point > in_point else in_point
            else:
                return PlaybackAdvanceResult(frame=frame, direction=direction, stop=True)
        frame = next_frame

    return PlaybackAdvanceResult(frame=frame, direction=direction, stop=False)


def realtime_steps(elapsed_seconds: float, fps: float) -> int:
    """Whole frames that should have elapsed for wall-clock realtime playback."""
    if elapsed_seconds <= 0.0 or fps <= 0.0:
        return 0
    return int(elapsed_seconds * fps)


def every_frame_can_advance(
    *,
    next_decode_ready: bool,
    current_display_ready: bool,
    display_cache_enabled: bool,
) -> bool:
    """Whether Play Every Frame may leave the current playhead.

    Decode readiness alone is not enough: the render thread coalesces pending
    params, so advancing before the current frame is uploaded leaves gaps in
    the display cache.
    """
    if display_cache_enabled and not current_display_ready:
        return False
    return bool(next_decode_ready)
