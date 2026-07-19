#include "transport_clock.h"

#include <algorithm>
#include <cmath>

int TransportClock::realtime_steps(double elapsed_seconds, double fps)
{
    if (elapsed_seconds <= 0.0 || fps <= 0.0) {
        return 0;
    }
    return static_cast<int>(elapsed_seconds * fps);
}

TransportLoopMode TransportClock::parse_loop_mode(const std::string& mode)
{
    if (mode == "bounce") {
        return TransportLoopMode::Bounce;
    }
    if (mode == "once") {
        return TransportLoopMode::Once;
    }
    return TransportLoopMode::Loop;
}

TransportTimingMode TransportClock::parse_timing_mode(const std::string& mode)
{
    if (mode == "realtime") {
        return TransportTimingMode::Realtime;
    }
    return TransportTimingMode::EveryFrame;
}

TransportAdvanceResult TransportClock::advance_playback(
    int current_frame,
    int direction,
    int steps,
    int in_point,
    int out_point,
    TransportLoopMode loop_mode)
{
    TransportAdvanceResult result;
    result.frame = current_frame;
    result.direction = direction >= 0 ? 1 : -1;
    result.moved = false;
    result.stop = false;
    result.segment_boundary = false;
    result.steps_taken = 0;

    if (steps <= 0) {
        result.frame = -1; // match Python: frame=None
        return result;
    }

    int frame = current_frame;
    int dir = result.direction;
    if (out_point < in_point) {
        std::swap(in_point, out_point);
    }

    for (int i = 0; i < steps; ++i) {
        int next_frame = frame + dir;
        if (dir > 0 && next_frame > out_point) {
            if (loop_mode == TransportLoopMode::Loop) {
                next_frame = in_point;
            } else if (loop_mode == TransportLoopMode::Bounce) {
                dir = -1;
                next_frame = out_point > in_point ? out_point - 1 : in_point;
            } else {
                result.frame = frame;
                result.direction = dir;
                result.moved = result.steps_taken > 0;
                result.stop = true;
                return result;
            }
        } else if (dir < 0 && next_frame < in_point) {
            if (loop_mode == TransportLoopMode::Loop) {
                next_frame = out_point;
            } else if (loop_mode == TransportLoopMode::Bounce) {
                dir = 1;
                next_frame = out_point > in_point ? in_point + 1 : in_point;
            } else {
                result.frame = frame;
                result.direction = dir;
                result.moved = result.steps_taken > 0;
                result.stop = true;
                return result;
            }
        }
        frame = next_frame;
        ++result.steps_taken;
    }

    result.frame = frame;
    result.direction = dir;
    result.moved = result.steps_taken > 0 && frame != current_frame;
    // Even if bounced back to same numeric frame unlikely; treat any step as moved
    // when steps_taken > 0 (Python compares frame/direction change at call site).
    if (result.steps_taken > 0) {
        result.moved = true;
    }
    return result;
}

void TransportClock::set_program(const TransportProgram& program)
{
    _program = program;
    if (_program.out_point < _program.in_point) {
        std::swap(_program.in_point, _program.out_point);
    }
    if (_program.fps < 1e-6) {
        _program.fps = 24.0;
    }
    _program.direction = _program.direction >= 0 ? 1 : -1;
    _program.current_frame = std::clamp(
        _program.current_frame, _program.in_point, _program.out_point);
    _anchored = false;
}

void TransportClock::reanchor(TimePoint now)
{
    _anchor_time = now;
    _anchor_frame = _program.current_frame;
    _anchor_direction = _program.direction;
    _anchored = true;
}

void TransportClock::play(TimePoint now)
{
    _program.playing = true;
    reanchor(now);
}

void TransportClock::pause()
{
    _program.playing = false;
    _anchored = false;
}

void TransportClock::seek(int frame, TimePoint now)
{
    _program.current_frame = std::clamp(frame, _program.in_point, _program.out_point);
    if (_program.playing) {
        reanchor(now);
    } else {
        _anchored = false;
    }
}

TransportClock::TimePoint TransportClock::next_deadline(TimePoint now) const
{
    if (!_program.playing || _program.fps <= 0.0) {
        return now;
    }
    const auto frame_ns = std::chrono::duration_cast<std::chrono::nanoseconds>(
        std::chrono::duration<double>(1.0 / _program.fps));
    if (!_anchored) {
        return now;
    }
    if (_program.timing_mode == TransportTimingMode::EveryFrame) {
        return _anchor_time + frame_ns;
    }
    // Realtime: wake each frame period from anchor + steps already due.
    const double elapsed = std::chrono::duration<double>(now - _anchor_time).count();
    const int due = realtime_steps(elapsed, _program.fps);
    return _anchor_time + frame_ns * static_cast<std::int64_t>(due + 1);
}

bool TransportClock::would_leave_segment(int from_frame, int to_frame) const
{
    if (!_program.hold_at_segment_bounds) {
        return false;
    }
    const int seg_in = _program.segment_global_start;
    const int seg_out = _program.segment_global_end;
    if (seg_out < seg_in) {
        return false;
    }
    // Boundary when destination is outside the active segment while still
    // inside the broader in/out range (or wrapping past the segment edge).
    const bool from_inside = from_frame >= seg_in && from_frame <= seg_out;
    const bool to_inside = to_frame >= seg_in && to_frame <= seg_out;
    if (from_inside && !to_inside) {
        return true;
    }
    return false;
}

TransportAdvanceResult TransportClock::tick(TimePoint now, const CanAdvanceFn& can_advance)
{
    TransportAdvanceResult result;
    result.frame = _program.current_frame;
    result.direction = _program.direction;
    result.moved = false;
    result.stop = false;
    result.segment_boundary = false;
    result.steps_taken = 0;

    if (!_program.playing) {
        return result;
    }
    if (!_anchored) {
        reanchor(now);
        return result;
    }

    const double elapsed = std::chrono::duration<double>(now - _anchor_time).count();

    if (_program.timing_mode == TransportTimingMode::EveryFrame) {
        if (elapsed + 1e-9 < (1.0 / _program.fps)) {
            return result;
        }

        auto peek = advance_playback(
            _program.current_frame,
            _program.direction,
            1,
            _program.in_point,
            _program.out_point,
            _program.loop_mode);
        if (peek.frame < 0) {
            return result;
        }
        if (peek.stop && peek.frame == _program.current_frame) {
            // Once-mode end: stop without moving.
            result.frame = _program.current_frame;
            result.direction = peek.direction;
            result.stop = true;
            _program.playing = false;
            _anchored = false;
            return result;
        }
        if (would_leave_segment(_program.current_frame, peek.frame)) {
            result.frame = _program.current_frame;
            result.direction = _program.direction;
            result.segment_boundary = true;
            _program.playing = false;
            _anchored = false;
            return result;
        }
        if (can_advance && !can_advance(peek.frame)) {
            return result;
        }

        result = peek;
        _program.current_frame = result.frame;
        _program.direction = result.direction;
        if (result.stop) {
            _program.playing = false;
            _anchored = false;
            return result;
        }
        reanchor(now);
        return result;
    }

    // Realtime: catch up from fixed anchor.
    const int steps = realtime_steps(elapsed, _program.fps);
    if (steps <= 0) {
        return result;
    }

    result = advance_playback(
        _anchor_frame,
        _anchor_direction,
        steps,
        _program.in_point,
        _program.out_point,
        _program.loop_mode);

    if (result.frame < 0) {
        result.frame = _program.current_frame;
        result.moved = false;
        return result;
    }

    if (_program.hold_at_segment_bounds
        && (result.frame < _program.segment_global_start
            || result.frame > _program.segment_global_end)
        && _program.current_frame >= _program.segment_global_start
        && _program.current_frame <= _program.segment_global_end) {
        if (_anchor_direction >= 0) {
            result.frame = _program.segment_global_end;
        } else {
            result.frame = _program.segment_global_start;
        }
        result.segment_boundary = true;
        result.stop = false;
        result.moved = result.frame != _program.current_frame;
        _program.current_frame = result.frame;
        _program.direction = result.direction;
        _program.playing = false;
        _anchored = false;
        return result;
    }

    _program.current_frame = result.frame;
    _program.direction = result.direction;

    if (result.stop) {
        _program.playing = false;
        _anchored = false;
    }
    return result;
}

int TransportClock::decoder_frame_for_source(int source_index, int global_frame) const
{
    for (const auto& slot : _program.slots) {
        if (slot.source_index != source_index) {
            continue;
        }
        int seg_start = slot.segment_global_start;
        int seg_end = slot.segment_global_end;
        if (seg_end < seg_start) {
            std::swap(seg_start, seg_end);
        }
        int local = global_frame - seg_start;
        if (local < 0) {
            local = 0;
        }
        if (seg_end > seg_start) {
            local = std::min(local, seg_end - seg_start);
        }
        if (!slot.decoder_frames.empty()) {
            if (local >= static_cast<int>(slot.decoder_frames.size())) {
                local = static_cast<int>(slot.decoder_frames.size()) - 1;
            }
            return slot.decoder_frames[static_cast<size_t>(local)];
        }
        return slot.decoder_start_frame + local;
    }
    return -1;
}
