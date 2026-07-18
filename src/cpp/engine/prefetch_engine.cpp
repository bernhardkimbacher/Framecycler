#include "prefetch_engine.h"

#include <algorithm>
#include <chrono>
#include <cmath>
#include <iostream>
#include <utility>

PrefetchEngine::PrefetchEngine(std::shared_ptr<CacheManager> cache, int max_workers)
    : _cache(std::move(cache))
    , _max_workers(std::max(1, max_workers))
{
    _running = true;
    {
        std::lock_guard<std::mutex> lock(_mutex);
        _start_workers_locked();
    }
    _planner = std::thread([this]() { _planner_loop(); });
}

PrefetchEngine::~PrefetchEngine()
{
    stop();
}

void PrefetchEngine::_start_workers_locked()
{
    const int n = std::max(1, _max_workers);
    _workers.reserve(static_cast<size_t>(n));
    for (int i = 0; i < n; ++i) {
        _workers.emplace_back([this]() { _worker_loop(); });
    }
}

void PrefetchEngine::stop()
{
    if (!_running.exchange(false)) {
        return;
    }
    {
        std::lock_guard<std::mutex> lock(_mutex);
        _wake = true;
        // Do not destroy Python callbacks here — py::object teardown needs the GIL.
        // Bindings / CacheEngine.clear callbacks under the GIL before calling stop().
    }
    _wake_cv.notify_all();
    _job_cv.notify_all();

    if (_planner.joinable()) {
        _planner.join();
    }

    std::vector<std::thread> workers;
    {
        std::lock_guard<std::mutex> lock(_mutex);
        workers.swap(_workers);
    }
    for (auto& t : workers) {
        if (t.joinable()) {
            t.join();
        }
    }

    std::lock_guard<std::mutex> lock(_mutex);
    while (!_heap.empty()) {
        _heap.pop();
    }
    _queued.clear();
    _decoding.clear();
    _python_inflight = 0;
}

void PrefetchEngine::set_path_table(
    const std::unordered_map<int, std::string>& paths,
    const std::vector<int>& sorted_frames)
{
    std::lock_guard<std::mutex> lock(_mutex);
    _paths = paths;
    _sorted_frames = sorted_frames;
    _wake = true;
    _wake_cv.notify_one();
}

void PrefetchEngine::set_options(
    float resolution_scale,
    const std::string& layer,
    const std::string& fallback_mode,
    int placeholder_width,
    int placeholder_height,
    bool native_path_decode)
{
    std::lock_guard<std::mutex> lock(_mutex);
    _resolution_scale = resolution_scale;
    _layer = layer;
    _fallback_mode = fallback_mode;
    _placeholder_width = placeholder_width;
    _placeholder_height = placeholder_height;
    _native_path_decode = native_path_decode;
    _wake = true;
    _wake_cv.notify_one();
}

void PrefetchEngine::set_enabled(bool enabled)
{
    std::lock_guard<std::mutex> lock(_mutex);
    _enabled = enabled;
    if (enabled) {
        _wake = true;
        _wake_cv.notify_one();
    }
}

void PrefetchEngine::set_lookahead(int lookahead)
{
    std::lock_guard<std::mutex> lock(_mutex);
    _lookahead = std::max(0, lookahead);
    _wake = true;
    _wake_cv.notify_one();
}

void PrefetchEngine::set_max_workers(int max_workers)
{
    max_workers = std::max(1, max_workers);
    std::lock_guard<std::mutex> lock(_mutex);
    _max_workers = max_workers;
    const int current = static_cast<int>(_workers.size());
    // Growing: spawn more workers. Shrinking: concurrency gate in
    // _can_take_job_locked; extras idle until stop().
    if (_max_workers > current) {
        for (int i = current; i < _max_workers; ++i) {
            _workers.emplace_back([this]() { _worker_loop(); });
        }
    }
    _job_cv.notify_all();
}

void PrefetchEngine::set_playback_range(int start, int end)
{
    if (end < start) {
        std::swap(start, end);
    }
    {
        std::lock_guard<std::mutex> lock(_mutex);
        _range_start = start;
        _range_end = end;
        _cache->set_playhead(_playhead, _direction, _range_start, _range_end);
        _drop_stale_queued_locked();
        _wake = true;
    }
    _wake_cv.notify_one();
}

void PrefetchEngine::set_playhead(int frame_index, int direction)
{
    if (direction == 0) {
        direction = 1;
    }
    {
        std::lock_guard<std::mutex> lock(_mutex);
        _playhead = frame_index;
        _direction = direction > 0 ? 1 : -1;
        _cache->set_playhead(_playhead, _direction, _range_start, _range_end);
        _drop_stale_queued_locked();
        _enqueue_locked(_playhead, 0);
        _wake = true;
    }
    _wake_cv.notify_one();
    _job_cv.notify_all();
}

void PrefetchEngine::schedule(int frame_index, int priority)
{
    {
        std::lock_guard<std::mutex> lock(_mutex);
        _enqueue_locked(frame_index, priority);
    }
    _job_cv.notify_all();
    _wake_cv.notify_one();
}

void PrefetchEngine::set_frame_ready_callback(FrameReadyCallback cb)
{
    std::lock_guard<std::mutex> lock(_mutex);
    _frame_ready_cb = std::move(cb);
}

void PrefetchEngine::set_python_decode_callback(PythonDecodeCallback cb)
{
    std::lock_guard<std::mutex> lock(_mutex);
    _python_decode_cb = std::move(cb);
}

void PrefetchEngine::clear()
{
    {
        std::lock_guard<std::mutex> lock(_mutex);
        while (!_heap.empty()) {
            _heap.pop();
        }
        _queued.clear();
        // Keep _decoding — in-flight workers will finish and drop themselves.
    }
    _cache->clear();
}

void PrefetchEngine::_planner_loop()
{
    while (_running.load()) {
        {
            std::unique_lock<std::mutex> lock(_mutex);
            _wake_cv.wait_for(lock, std::chrono::milliseconds(100), [this]() {
                return !_running.load() || _wake;
            });
            if (!_running.load()) {
                break;
            }
            _wake = false;
            _fill_requests_locked();
        }
        _job_cv.notify_all();
    }
}

void PrefetchEngine::_worker_loop()
{
    while (_running.load()) {
        Job job;
        {
            std::unique_lock<std::mutex> lock(_mutex);
            _job_cv.wait(lock, [this]() {
                return !_running.load() || _can_take_job_locked();
            });
            if (!_running.load()) {
                break;
            }
            if (!_pop_job_locked(job)) {
                continue;
            }
        }
        _process_job(job);
    }
}

void PrefetchEngine::_fill_requests_locked()
{
    if (!_enabled) {
        return;
    }
    if (_range_end < _range_start) {
        return;
    }

    const int window = _fill_window_locked();
    if (window <= 0) {
        return;
    }

    // Always ensure the playhead itself is scheduled.
    _enqueue_locked(_playhead, 0);

    int curr = _playhead;
    const int direction = _direction;
    for (int distance = 1; distance <= window; ++distance) {
        curr += direction;
        if (curr > _range_end) {
            curr = _range_start;
        } else if (curr < _range_start) {
            curr = _range_end;
        }
        _enqueue_locked(curr, distance);
    }
}

int PrefetchEngine::_fill_window_locked() const
{
    if (_lookahead > 0) {
        return _lookahead;
    }
    if (_range_end < _range_start) {
        return 0;
    }
    const int range_size = _range_end - _range_start + 1;
    const size_t max_bytes = _cache->max_bytes();
    if (max_bytes == 0) {
        return 0;
    }

    size_t bytes_per = _cache->bytes_per_frame();
    if (bytes_per == 0) {
        // Estimate from placeholder metadata until the first frame lands.
        const int w = std::max(1, _placeholder_width);
        const int h = std::max(1, _placeholder_height);
        bytes_per = static_cast<size_t>(w) * static_cast<size_t>(h) * 4u * sizeof(uint16_t);
    }
    if (bytes_per == 0) {
        return range_size;
    }

    // Leave a small margin so near-playhead frames are not immediately evicted.
    const size_t usable = (max_bytes * 95) / 100;
    const int capacity = static_cast<int>(usable / bytes_per);
    if (capacity <= 0) {
        return 1;
    }
    return std::min(range_size, capacity);
}

void PrefetchEngine::_drop_stale_queued_locked()
{
    std::priority_queue<Job, std::vector<Job>, JobCompare> kept;
    std::unordered_set<int> kept_queued;
    while (!_heap.empty()) {
        Job job = _heap.top();
        _heap.pop();
        if (job.priority == 0 || _frame_in_lookahead_window_locked(job.frame_index)) {
            kept.push(job);
            kept_queued.insert(job.frame_index);
        }
    }
    _heap.swap(kept);
    _queued.swap(kept_queued);
}

bool PrefetchEngine::_frame_in_lookahead_window_locked(int frame_index) const
{
    if (_range_end < _range_start) {
        return frame_index == _playhead;
    }
    const int window = _fill_window_locked();
    if (window <= 0) {
        return frame_index == _playhead;
    }
    int curr = _playhead;
    if (frame_index == curr) {
        return true;
    }
    const int direction = _direction;
    for (int distance = 1; distance <= window; ++distance) {
        curr += direction;
        if (curr > _range_end) {
            curr = _range_start;
        } else if (curr < _range_start) {
            curr = _range_end;
        }
        if (curr == frame_index) {
            return true;
        }
    }
    return false;
}

bool PrefetchEngine::_enqueue_locked(int frame_index, int priority)
{
    if (!_enabled) {
        return false;
    }
    if (_cache->has_frame(frame_index)) {
        return false;
    }
    if (_queued.count(frame_index) || _decoding.count(frame_index)) {
        return false;
    }
    if (_cache->is_decode_claimed(frame_index)) {
        return false;
    }
    _queued.insert(frame_index);
    _heap.push(Job{priority, _heap_seq++, frame_index});
    return true;
}

bool PrefetchEngine::_can_take_job_locked() const
{
    if (_heap.empty()) {
        return false;
    }
    if (static_cast<int>(_decoding.size()) >= _max_workers) {
        return false;
    }
    if (!_native_path_decode && _python_inflight >= kMaxPythonDecodeConcurrent) {
        return false;
    }
    return true;
}

bool PrefetchEngine::_pop_job_locked(Job& out)
{
    while (!_heap.empty()) {
        if (!_can_take_job_locked()) {
            return false;
        }
        out = _heap.top();
        _heap.pop();
        _queued.erase(out.frame_index);

        if (_cache->has_frame(out.frame_index)) {
            continue;
        }
        if (_decoding.count(out.frame_index)) {
            continue;
        }
        _decoding.insert(out.frame_index);
        if (!_native_path_decode) {
            ++_python_inflight;
        }
        return true;
    }
    return false;
}

std::string PrefetchEngine::_resolve_path_locked(int frame_index) const
{
    auto it = _paths.find(frame_index);
    if (it != _paths.end()) {
        return it->second;
    }
    if (_fallback_mode == "Nearest Frame" && !_sorted_frames.empty()) {
        auto lower = std::lower_bound(_sorted_frames.begin(), _sorted_frames.end(), frame_index);
        int nearest = _sorted_frames.front();
        if (lower == _sorted_frames.end()) {
            nearest = _sorted_frames.back();
        } else if (lower == _sorted_frames.begin()) {
            nearest = *lower;
        } else {
            const int hi = *lower;
            const int lo = *std::prev(lower);
            nearest = (std::abs(hi - frame_index) < std::abs(frame_index - lo)) ? hi : lo;
        }
        auto nit = _paths.find(nearest);
        if (nit != _paths.end()) {
            return nit->second;
        }
    }
    return {};
}

void PrefetchEngine::_process_job(const Job& job)
{
    const int frame_index = job.frame_index;
    bool ok = false;

    try {
        if (_cache->has_frame(frame_index)) {
            ok = true;
        } else if (_native_path_decode) {
            std::string path;
            float scale = 1.0f;
            std::string layer;
            std::string fallback;
            int ph_w = 0;
            int ph_h = 0;
            {
                std::lock_guard<std::mutex> lock(_mutex);
                path = _resolve_path_locked(frame_index);
                scale = _resolution_scale;
                layer = _layer;
                fallback = _fallback_mode;
                ph_w = _placeholder_width;
                ph_h = _placeholder_height;
            }
            ok = _cache->decode_and_cache_frame(
                frame_index, path, scale, layer, fallback, ph_w, ph_h);
            // Placeholder paths still write pixels even when success is false.
            ok = ok || _cache->has_frame(frame_index);
        } else {
            PythonDecodeCallback cb;
            {
                std::lock_guard<std::mutex> lock(_mutex);
                cb = _python_decode_cb;
            }
            if (cb) {
                if (_cache->try_claim_decode(frame_index)) {
                    try {
                        ok = cb(frame_index);
                    } catch (...) {
                        _cache->release_decode_claim(frame_index);
                        throw;
                    }
                    _cache->release_decode_claim(frame_index);
                    ok = ok || _cache->has_frame(frame_index);
                } else {
                    ok = _cache->has_frame(frame_index);
                }
            }
        }
    } catch (const std::exception& exc) {
        std::cerr << "PrefetchEngine: decode failed for frame " << frame_index
                  << ": " << exc.what() << std::endl;
        ok = _cache->has_frame(frame_index);
    } catch (...) {
        std::cerr << "PrefetchEngine: decode failed for frame " << frame_index
                  << " (unknown error)" << std::endl;
        ok = _cache->has_frame(frame_index);
    }

    {
        std::lock_guard<std::mutex> lock(_mutex);
        _decoding.erase(frame_index);
        if (!_native_path_decode) {
            _python_inflight = std::max(0, _python_inflight - 1);
        }
    }
    _job_cv.notify_all();

    if (ok) {
        _notify_ready(frame_index);
    }
}

void PrefetchEngine::_notify_ready(int frame_index)
{
    FrameReadyCallback cb;
    {
        std::lock_guard<std::mutex> lock(_mutex);
        cb = _frame_ready_cb;
    }
    if (cb) {
        try {
            cb(frame_index);
        } catch (const std::exception& exc) {
            std::cerr << "PrefetchEngine: frame-ready callback failed for frame "
                      << frame_index << ": " << exc.what() << std::endl;
        } catch (...) {
            std::cerr << "PrefetchEngine: frame-ready callback failed for frame "
                      << frame_index << std::endl;
        }
    }
}
