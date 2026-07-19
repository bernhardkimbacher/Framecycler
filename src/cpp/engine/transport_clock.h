#pragma once

#include <chrono>
#include <cstdint>
#include <functional>
#include <string>
#include <vector>

enum class TransportLoopMode {
    Once = 0,
    Loop = 1,
    Bounce = 2,
};

enum class TransportTimingMode {
    EveryFrame = 0,
    Realtime = 1,
};

struct TransportSlotMapping {
    int source_index = 0;
    int segment_global_start = 0;
    int segment_global_end = 0;
    /// Contiguous mapping: decoder_frame = decoder_start_frame + local_index.
    int decoder_start_frame = 0;
    /// When non-empty, local_index indexes this table instead of the offset.
    std::vector<int> decoder_frames;
    int playback_in = 0;
    int playback_out = 0;
};

struct TransportProgram {
    bool playing = false;
    int direction = 1;
    double fps = 24.0;
    int in_point = 0;
    int out_point = 0;
    TransportLoopMode loop_mode = TransportLoopMode::Loop;
    TransportTimingMode timing_mode = TransportTimingMode::EveryFrame;
    int current_frame = 0;
    /// Active segment bounds. When hold_at_segment_bounds is true and the
    /// clock would leave this range (while in/out still continues), pause and
    /// signal a segment boundary for Python to push the next program.
    int segment_global_start = 0;
    int segment_global_end = 0;
    bool hold_at_segment_bounds = false;
    std::vector<TransportSlotMapping> slots;
};

struct TransportAdvanceResult {
    int frame = 0;
    int direction = 1;
    bool moved = false;
    bool stop = false;
    bool segment_boundary = false;
    int steps_taken = 0;
};

/// Pure playback state machine (mirrors Python playback_timing.py).
/// Thread-agnostic; the render thread owns the instance.
class TransportClock {
public:
    using Clock = std::chrono::steady_clock;
    using TimePoint = Clock::time_point;
    /// Predicate: true when every_frame mode may advance to ``global_frame``.
    using CanAdvanceFn = std::function<bool(int global_frame)>;

    void set_program(const TransportProgram& program);
    TransportProgram program() const { return _program; }

    void play(TimePoint now = Clock::now());
    void pause();
    void seek(int frame, TimePoint now = Clock::now());

    bool is_playing() const { return _program.playing; }
    int current_frame() const { return _program.current_frame; }
    int direction() const { return _program.direction; }
    double fps() const { return _program.fps; }

    /// Wall-clock deadline for the next every_frame / realtime tick.
    TimePoint next_deadline(TimePoint now = Clock::now()) const;

    /// Advance based on elapsed wall time (realtime) or one step (every_frame).
    TransportAdvanceResult tick(TimePoint now, const CanAdvanceFn& can_advance = {});

    /// Map a global timeline frame to a decoder frame for ``source_index``.
    /// Returns -1 when the source is not in the program.
    int decoder_frame_for_source(int source_index, int global_frame) const;

    /// Static helpers (also exposed for parity tests).
    static int realtime_steps(double elapsed_seconds, double fps);
    static TransportAdvanceResult advance_playback(
        int current_frame,
        int direction,
        int steps,
        int in_point,
        int out_point,
        TransportLoopMode loop_mode);
    static TransportLoopMode parse_loop_mode(const std::string& mode);
    static TransportTimingMode parse_timing_mode(const std::string& mode);

private:
    void reanchor(TimePoint now);
    bool would_leave_segment(int from_frame, int to_frame) const;

    TransportProgram _program;
    TimePoint _anchor_time{};
    int _anchor_frame = 0;
    int _anchor_direction = 1;
    bool _anchored = false;
};
