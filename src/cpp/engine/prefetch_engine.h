#pragma once

#include "cache_manager.h"

#include <atomic>
#include <condition_variable>
#include <cstdint>
#include <functional>
#include <memory>
#include <mutex>
#include <queue>
#include <string>
#include <thread>
#include <unordered_map>
#include <unordered_set>
#include <vector>

class PrefetchEngine {
public:
    using FrameReadyCallback = std::function<void(int)>;
    using PythonDecodeCallback = std::function<bool(int)>;

    PrefetchEngine(std::shared_ptr<CacheManager> cache, int max_workers);
    ~PrefetchEngine();

    PrefetchEngine(const PrefetchEngine&) = delete;
    PrefetchEngine& operator=(const PrefetchEngine&) = delete;

    void set_path_table(
        const std::unordered_map<int, std::string>& paths,
        const std::vector<int>& sorted_frames);
    void set_options(
        float resolution_scale,
        const std::string& layer,
        const std::string& fallback_mode,
        int placeholder_width,
        int placeholder_height,
        bool native_path_decode);
    void set_enabled(bool enabled);
    void set_lookahead(int lookahead);
    void set_max_workers(int max_workers);

    void set_playback_range(int start, int end);
    void set_playhead(int frame_index, int direction = 1);
    void schedule(int frame_index, int priority = 0);

    void set_frame_ready_callback(FrameReadyCallback cb);
    void set_python_decode_callback(PythonDecodeCallback cb);

    void clear();
    void stop();

private:
    struct Job {
        int priority = 0;
        uint64_t seq = 0;
        int frame_index = 0;
    };

    struct JobCompare {
        bool operator()(const Job& a, const Job& b) const {
            if (a.priority != b.priority) {
                return a.priority > b.priority;
            }
            return a.seq > b.seq;
        }
    };

    void _start_workers_locked();
    void _planner_loop();
    void _worker_loop();
    void _fill_requests_locked();
    void _drop_stale_queued_locked();
    int _fill_window_locked() const;
    bool _enqueue_locked(int frame_index, int priority);
    bool _can_take_job_locked() const;
    bool _pop_job_locked(Job& out);
    void _process_job(const Job& job);
    std::string _resolve_path_locked(int frame_index) const;
    bool _frame_in_lookahead_window_locked(int frame_index) const;
    void _notify_ready(int frame_index);

    std::shared_ptr<CacheManager> _cache;

    mutable std::mutex _mutex;
    std::condition_variable _wake_cv;
    std::condition_variable _job_cv;

    std::priority_queue<Job, std::vector<Job>, JobCompare> _heap;
    std::unordered_set<int> _queued;
    std::unordered_set<int> _decoding;
    uint64_t _heap_seq = 0;

    std::unordered_map<int, std::string> _paths;
    std::vector<int> _sorted_frames;

    float _resolution_scale = 1.0f;
    std::string _layer;
    std::string _fallback_mode = "Flat Gray";
    int _placeholder_width = 0;
    int _placeholder_height = 0;
    bool _native_path_decode = true;
    // Disabled until CacheEngine.start() / set_enabled(true).
    bool _enabled = false;
    // 0 = budget-aware fill (default). >0 = explicit test override horizon.
    int _lookahead = 0;
    int _max_workers = 1;
    int _python_inflight = 0;
    static constexpr int kMaxPythonDecodeConcurrent = 1;

    int _playhead = 0;
    int _direction = 1;
    int _range_start = 0;
    int _range_end = 0;

    bool _wake = false;
    std::atomic<bool> _running{false};

    FrameReadyCallback _frame_ready_cb;
    PythonDecodeCallback _python_decode_cb;

    std::thread _planner;
    std::vector<std::thread> _workers;
};
