#include "cache_manager.h"
#include "native_decoder.h"
#include <cmath>
#include <algorithm>
#include <iostream>
#include <mutex>
#include <deque>

CacheManager::CacheManager(double ram_limit_gb)
    : _current_playhead(0), _play_direction(1), _in_point(0), _out_point(100), _allocated_bytes(0) {
    set_ram_limit(ram_limit_gb);
}

void CacheManager::set_ram_limit(double ram_limit_gb) {
    std::unique_lock<std::shared_mutex> lock(_mutex);
    _ram_limit_gb = ram_limit_gb;
    if (ram_limit_gb <= 0.0) {
        _max_bytes = 0;
        _slots.clear();
        _frame_to_slot.clear();
        _slot_to_frame.clear();
        _inflight_decodes.clear();
        _allocated_bytes = 0;
        ++_epoch;
        return;
    }
    _max_bytes = static_cast<size_t>(ram_limit_gb * 1024.0 * 1024.0 * 1024.0);
}

void CacheManager::set_playhead(int playhead, int direction, int in_point, int out_point) {
    std::unique_lock<std::shared_mutex> lock(_mutex);
    _current_playhead = playhead;
    _play_direction = direction;
    _in_point = in_point;
    _out_point = out_point;
}

bool CacheManager::has_frame(int frame_index) {
    std::shared_lock<std::shared_mutex> lock(_mutex);
    auto it = _frame_to_slot.find(frame_index);
    if (it == _frame_to_slot.end()) {
        return false;
    }
    return _slots[it->second].active;
}

void CacheManager::_unmap_slot(size_t slot_idx) {
    auto sit = _slot_to_frame.find(slot_idx);
    if (sit == _slot_to_frame.end()) {
        return;
    }
    const int frame = sit->second;
    auto fit = _frame_to_slot.find(frame);
    if (fit != _frame_to_slot.end() && fit->second == slot_idx) {
        _frame_to_slot.erase(fit);
    }
    _slot_to_frame.erase(sit);
}

void CacheManager::_release_slot_capacity(size_t slot_idx) {
    auto& slot = _slots[slot_idx];
    if (slot.pin_count > 0) {
        // Pinned for GPU upload — keep backing store; mark inactive for readers.
        slot.active = false;
        return;
    }
    _allocated_bytes -= slot.data.size() * sizeof(uint16_t);
    slot.data.clear();
    slot.data.shrink_to_fit();
    slot.width = 0;
    slot.height = 0;
    slot.channels = 0;
    slot.active = false;
    slot.pin_count = 0;
}

size_t CacheManager::_find_unmapped_inactive_slot() const {
    for (size_t i = 0; i < _slots.size(); ++i) {
        if (_slots[i].active) {
            continue;
        }
        if (_slot_to_frame.find(i) != _slot_to_frame.end()) {
            continue;
        }
        return i;
    }
    return static_cast<size_t>(-1);
}

void CacheManager::write_frame(int frame_index, int width, int height, int channels, const uint16_t* pixel_data, size_t data_size) {
    const size_t expected =
        static_cast<size_t>(width) * static_cast<size_t>(height) * static_cast<size_t>(channels);
    if (!pixel_data || width <= 0 || height <= 0 || channels <= 0 || data_size == 0
        || data_size != expected) {
        // Reject empty/mismatched payloads so has_frame never becomes a sticky poison hit.
        return;
    }

    std::unique_lock<std::shared_mutex> lock(_mutex);

    if (_max_bytes == 0) {
        _slots.clear();
        _frame_to_slot.clear();
        _slot_to_frame.clear();
        _allocated_bytes = 0;

        FrameBuffer transient;
        transient.data.resize(data_size);
        std::copy(pixel_data, pixel_data + data_size, transient.data.begin());
        transient.width = width;
        transient.height = height;
        transient.channels = channels;
        transient.active = true;
        transient.epoch = _epoch;
        _slots.push_back(transient);
        _frame_to_slot[frame_index] = 0;
        _slot_to_frame[0] = frame_index;
        _allocated_bytes = data_size * sizeof(uint16_t);
        return;
    }

    auto it = _frame_to_slot.find(frame_index);
    if (it != _frame_to_slot.end()) {
        size_t slot_idx = it->second;
        auto& slot = _slots[slot_idx];
        if (slot.data.size() < data_size) {
            _allocated_bytes -= slot.data.size() * sizeof(uint16_t);
            slot.data.resize(data_size);
            _allocated_bytes += data_size * sizeof(uint16_t);
        }
        std::copy(pixel_data, pixel_data + data_size, slot.data.begin());
        slot.width = width;
        slot.height = height;
        slot.channels = channels;
        slot.active = true;
        slot.epoch = _epoch;
        return;
    }

    size_t req_bytes = data_size * sizeof(uint16_t);

    size_t target_slot_idx = static_cast<size_t>(-1);
    if (_allocated_bytes + req_bytes > _max_bytes) {
        int frame_count = std::max(1, _out_point - _in_point + 1);
        target_slot_idx = _find_slot_to_evict(frame_count);
    }
    if (target_slot_idx == static_cast<size_t>(-1)) {
        target_slot_idx = _find_unmapped_inactive_slot();
    }

    if (target_slot_idx == static_cast<size_t>(-1)) {
        FrameBuffer new_slot;
        new_slot.data.resize(data_size);
        std::copy(pixel_data, pixel_data + data_size, new_slot.data.begin());
        new_slot.width = width;
        new_slot.height = height;
        new_slot.channels = channels;
        new_slot.active = true;
        new_slot.epoch = _epoch;

        _slots.push_back(new_slot);
        size_t new_idx = _slots.size() - 1;
        _frame_to_slot[frame_index] = new_idx;
        _slot_to_frame[new_idx] = frame_index;
        _allocated_bytes += req_bytes;
    } else {
        auto& slot = _slots[target_slot_idx];
        _unmap_slot(target_slot_idx);

        _allocated_bytes -= slot.data.size() * sizeof(uint16_t);

        if (slot.data.size() < data_size) {
            slot.data.resize(data_size);
        }
        std::copy(pixel_data, pixel_data + data_size, slot.data.begin());

        slot.width = width;
        slot.height = height;
        slot.channels = channels;
        slot.active = true;
        slot.epoch = _epoch;

        _frame_to_slot[frame_index] = target_slot_idx;
        _slot_to_frame[target_slot_idx] = frame_index;
        _allocated_bytes += slot.data.size() * sizeof(uint16_t);
    }
}

uint16_t* CacheManager::acquire_write_slot(int frame_index, int width, int height, int channels)
{
    std::unique_lock<std::shared_mutex> lock(_mutex);

    if (_inflight_decodes.find(frame_index) == _inflight_decodes.end()) {
        // Caller must claim first so eviction can protect this slot.
        return nullptr;
    }

    const size_t data_size =
        static_cast<size_t>(width) * static_cast<size_t>(height) * static_cast<size_t>(channels);
    if (data_size == 0) {
        return nullptr;
    }
    const size_t req_bytes = data_size * sizeof(uint16_t);

    // Already mapped (e.g. retry) — resize and deactivate for rewrite.
    auto it = _frame_to_slot.find(frame_index);
    if (it != _frame_to_slot.end()) {
        size_t slot_idx = it->second;
        auto& slot = _slots[slot_idx];
        if (slot.data.size() < data_size) {
            _allocated_bytes -= slot.data.size() * sizeof(uint16_t);
            slot.data.resize(data_size);
            _allocated_bytes += data_size * sizeof(uint16_t);
        }
        slot.width = width;
        slot.height = height;
        slot.channels = channels;
        slot.active = false;
        slot.epoch = _epoch;
        return slot.data.data();
    }

    if (_max_bytes == 0) {
        _slots.clear();
        _frame_to_slot.clear();
        _slot_to_frame.clear();
        _allocated_bytes = 0;

        FrameBuffer transient;
        transient.data.resize(data_size);
        transient.width = width;
        transient.height = height;
        transient.channels = channels;
        transient.active = false;
        transient.epoch = _epoch;
        _slots.push_back(std::move(transient));
        _frame_to_slot[frame_index] = 0;
        _slot_to_frame[0] = frame_index;
        _allocated_bytes = req_bytes;
        return _slots[0].data.data();
    }

    size_t target_slot_idx = static_cast<size_t>(-1);
    if (_allocated_bytes + req_bytes > _max_bytes) {
        int frame_count = std::max(1, _out_point - _in_point + 1);
        target_slot_idx = _find_slot_to_evict(frame_count);
    }
    // Prefer recycling failed-commit / unmapped capacity before growing.
    if (target_slot_idx == static_cast<size_t>(-1)) {
        target_slot_idx = _find_unmapped_inactive_slot();
    }

    if (target_slot_idx == static_cast<size_t>(-1)) {
        FrameBuffer new_slot;
        new_slot.data.resize(data_size);
        new_slot.width = width;
        new_slot.height = height;
        new_slot.channels = channels;
        new_slot.active = false;
        new_slot.epoch = _epoch;
        _slots.push_back(std::move(new_slot));
        size_t new_idx = _slots.size() - 1;
        _frame_to_slot[frame_index] = new_idx;
        _slot_to_frame[new_idx] = frame_index;
        _allocated_bytes += req_bytes;
        return _slots[new_idx].data.data();
    }

    auto& slot = _slots[target_slot_idx];
    _unmap_slot(target_slot_idx);
    _allocated_bytes -= slot.data.size() * sizeof(uint16_t);

    if (slot.data.size() < data_size) {
        slot.data.resize(data_size);
    }
    slot.width = width;
    slot.height = height;
    slot.channels = channels;
    slot.active = false;
    slot.epoch = _epoch;

    _frame_to_slot[frame_index] = target_slot_idx;
    _slot_to_frame[target_slot_idx] = frame_index;
    _allocated_bytes += slot.data.size() * sizeof(uint16_t);
    return slot.data.data();
}

void CacheManager::commit_write_slot(int frame_index, bool success)
{
    std::unique_lock<std::shared_mutex> lock(_mutex);
    auto it = _frame_to_slot.find(frame_index);
    if (it == _frame_to_slot.end()) {
        return;
    }
    size_t slot_idx = it->second;
    auto& slot = _slots[slot_idx];
    // Only surface the frame if it was acquired in the current epoch. A clear()
    // that ran mid-decode bumps the epoch so stale layer/resolution pixels stay hidden.
    const size_t expected =
        static_cast<size_t>(slot.width) * static_cast<size_t>(slot.height)
        * static_cast<size_t>(slot.channels);
    if (success && slot.epoch == _epoch && slot.width > 0 && slot.height > 0 && slot.channels > 0
        && slot.data.size() == expected) {
        slot.active = true;
    } else {
        // Unmap failed / stale / empty decode so the slot can be reused; keep capacity.
        slot.active = false;
        _unmap_slot(slot_idx);
    }
}

bool CacheManager::try_claim_decode(int frame_index) {
    std::unique_lock<std::shared_mutex> lock(_mutex);
    auto it = _frame_to_slot.find(frame_index);
    if (it != _frame_to_slot.end() && _slots[it->second].active) {
        return false;
    }
    if (_inflight_decodes.find(frame_index) != _inflight_decodes.end()) {
        return false;
    }
    _inflight_decodes.insert(frame_index);
    return true;
}

void CacheManager::release_decode_claim(int frame_index) {
    std::unique_lock<std::shared_mutex> lock(_mutex);
    _inflight_decodes.erase(frame_index);
}

bool CacheManager::is_decode_claimed(int frame_index) const {
    std::shared_lock<std::shared_mutex> lock(_mutex);
    return _inflight_decodes.find(frame_index) != _inflight_decodes.end();
}

bool CacheManager::decode_and_cache_frame(int frame_index, const std::string& file_path, float resolution_scale, const std::string& layer, const std::string& fallback_mode, int placeholder_width, int placeholder_height)
{
    if (has_frame(frame_index)) {
        return true;
    }
    if (!try_claim_decode(frame_index)) {
        return has_frame(frame_index);
    }

    bool ok = false;
    try {
        int out_w = 0;
        int out_h = 0;
        int out_ch = 0;
        bool wrote_placeholder = false;
        ok = NativeDecoder::decode_with_allocator(
            file_path,
            resolution_scale,
            layer,
            [this, frame_index](int w, int h, int ch) -> uint16_t* {
                return acquire_write_slot(frame_index, w, h, ch);
            },
            out_w,
            out_h,
            out_ch,
            wrote_placeholder,
            fallback_mode,
            placeholder_width,
            placeholder_height);

        const bool commit_ok = ok || wrote_placeholder;
        commit_write_slot(frame_index, commit_ok);
        ok = commit_ok;
    } catch (...) {
        commit_write_slot(frame_index, false);
        release_decode_claim(frame_index);
        throw;
    }
    release_decode_claim(frame_index);
    return ok;
}

bool CacheManager::get_frame_dimensions(int frame_index, int& width, int& height, int& channels) {
    std::shared_lock<std::shared_mutex> lock(_mutex);
    auto it = _frame_to_slot.find(frame_index);
    if (it == _frame_to_slot.end()) {
        return false;
    }
    size_t idx = it->second;
    const auto& slot = _slots[idx];
    if (!slot.active) {
        return false;
    }
    width = slot.width;
    height = slot.height;
    channels = slot.channels;
    return true;
}

bool CacheManager::copy_frame_data(int frame_index, uint16_t* dest_ptr, size_t dest_size_elements) {
    std::shared_lock<std::shared_mutex> lock(_mutex);
    auto it = _frame_to_slot.find(frame_index);
    if (it == _frame_to_slot.end()) {
        return false;
    }
    size_t idx = it->second;
    const auto& slot = _slots[idx];
    if (!slot.active) {
        return false;
    }
    size_t req_elements = static_cast<size_t>(slot.width * slot.height * slot.channels);
    if (dest_size_elements < req_elements) {
        return false;
    }
    std::copy(slot.data.begin(), slot.data.begin() + req_elements, dest_ptr);
    return true;
}

bool CacheManager::pin_frame(int frame_index, FramePin& out)
{
    out = FramePin{};
    std::unique_lock<std::shared_mutex> lock(_mutex);
    auto it = _frame_to_slot.find(frame_index);
    if (it == _frame_to_slot.end()) {
        return false;
    }
    size_t idx = it->second;
    auto& slot = _slots[idx];
    if (!slot.active || slot.data.empty()) {
        return false;
    }
    const size_t elements =
        static_cast<size_t>(slot.width) * static_cast<size_t>(slot.height)
        * static_cast<size_t>(slot.channels);
    if (elements == 0 || slot.data.size() < elements) {
        return false;
    }
    ++slot.pin_count;
    out.frame_index = frame_index;
    out.epoch = slot.epoch;
    out.slot_idx = idx;
    out.data = slot.data.data();
    out.width = slot.width;
    out.height = slot.height;
    out.channels = slot.channels;
    out.element_count = elements;
    return true;
}

void CacheManager::unpin_frame(const FramePin& pin)
{
    if (!pin.valid() || pin.slot_idx == static_cast<size_t>(-1)) {
        return;
    }
    std::unique_lock<std::shared_mutex> lock(_mutex);
    if (pin.slot_idx >= _slots.size()) {
        return;
    }
    auto& slot = _slots[pin.slot_idx];
    // Only unpin if this pin still matches the slot identity.
    if (slot.epoch != pin.epoch || slot.pin_count <= 0) {
        return;
    }
    --slot.pin_count;
}

bool CacheManager::with_active_frame(
    int frame_index,
    const std::function<void(const uint16_t* data, int width, int height, int channels)>& fn)
{
    std::shared_lock<std::shared_mutex> lock(_mutex);
    auto it = _frame_to_slot.find(frame_index);
    if (it == _frame_to_slot.end()) {
        return false;
    }
    size_t idx = it->second;
    const auto& slot = _slots[idx];
    if (!slot.active || slot.data.empty()) {
        return false;
    }
    fn(slot.data.data(), slot.width, slot.height, slot.channels);
    return true;
}

std::vector<int> CacheManager::get_cached_frames() {
    std::shared_lock<std::shared_mutex> lock(_mutex);
    std::vector<int> frames;
    frames.reserve(_frame_to_slot.size());
    for (const auto& pair : _frame_to_slot) {
        if (_slots[pair.second].active) {
            frames.push_back(pair.first);
        }
    }
    return frames;
}

size_t CacheManager::allocated_bytes() const {
    std::shared_lock<std::shared_mutex> lock(_mutex);
    return _allocated_bytes;
}

size_t CacheManager::max_bytes() const {
    std::shared_lock<std::shared_mutex> lock(_mutex);
    return _max_bytes;
}

size_t CacheManager::bytes_per_frame() const {
    std::shared_lock<std::shared_mutex> lock(_mutex);
    for (const auto& slot : _slots) {
        if (slot.active && !slot.data.empty()) {
            return slot.data.size() * sizeof(uint16_t);
        }
    }
    return 0;
}

void CacheManager::clear() {
    std::unique_lock<std::shared_mutex> lock(_mutex);
    ++_epoch;

    // Keep _inflight_decodes intact so mid-decode workers can still acquire/commit.
    // Free every slot that is not currently being written; leave in-flight buffers
    // mapped so their pointers stay valid until commit (which will then unmap due
    // to the epoch mismatch).
    for (size_t i = 0; i < _slots.size(); ++i) {
        auto sit = _slot_to_frame.find(i);
        const bool inflight =
            sit != _slot_to_frame.end() &&
            _inflight_decodes.find(sit->second) != _inflight_decodes.end();
        const bool pinned = _slots[i].pin_count > 0;
        if (inflight || pinned) {
            // Keep mapping and capacity; mark inactive so readers cannot see it.
            // Pinned slots stay until unpin (GPU staging generation completes).
            _slots[i].active = false;
            continue;
        }
        if (sit != _slot_to_frame.end()) {
            const int frame = sit->second;
            auto fit = _frame_to_slot.find(frame);
            if (fit != _frame_to_slot.end() && fit->second == i) {
                _frame_to_slot.erase(fit);
            }
            _slot_to_frame.erase(sit);
        }
        _release_slot_capacity(i);
    }
}

size_t CacheManager::_find_slot_to_evict(int frame_count) {
    if (_slots.empty()) {
        return static_cast<size_t>(-1);
    }

    // Prefer inactive slots that are NOT mid-write (inflight claim) or pinned.
    for (size_t i = 0; i < _slots.size(); ++i) {
        if (_slots[i].pin_count > 0) {
            continue; // GPU upload pin — never evict
        }
        if (!_slots[i].active) {
            auto sit = _slot_to_frame.find(i);
            if (sit != _slot_to_frame.end() &&
                _inflight_decodes.find(sit->second) != _inflight_decodes.end()) {
                continue; // mid-write — never evict
            }
            // Unmapped inactive capacity, or failed commit leftover.
            if (sit == _slot_to_frame.end()) {
                return i;
            }
            // Mapped but inactive and not inflight — safe to reuse.
            if (_inflight_decodes.find(sit->second) == _inflight_decodes.end()) {
                return i;
            }
        }
    }

    size_t furthest_idx = static_cast<size_t>(-1);
    int best_score = -1;
    const int span = std::max(1, frame_count);
    const int dir = (_play_direction >= 0) ? 1 : -1;
    const int in_pt = _in_point;

    for (size_t i = 0; i < _slots.size(); ++i) {
        auto sit = _slot_to_frame.find(i);
        if (sit == _slot_to_frame.end()) {
            continue;
        }
        const int frame_num = sit->second;
        if (_inflight_decodes.find(frame_num) != _inflight_decodes.end()) {
            continue; // never evict in-flight writes
        }
        if (_slots[i].pin_count > 0) {
            continue; // never evict pinned upload frames
        }
        if (!_slots[i].active) {
            continue; // handled above
        }

        const int f = ((frame_num - in_pt) % span + span) % span;
        const int p = ((_current_playhead - in_pt) % span + span) % span;
        const int steps_along_play =
            (dir >= 0) ? ((f - p + span) % span) : ((p - f + span) % span);
        const int circ = std::min(steps_along_play, span - steps_along_play);
        // Prefer victims behind the playhead (opposite play direction).
        bool behind = false;
        if (steps_along_play != 0) {
            const int steps_opposite = span - steps_along_play;
            behind = steps_opposite <= steps_along_play;
        }
        const int score = (behind ? 1'000'000 : 0) + circ;

        if (score > best_score) {
            best_score = score;
            furthest_idx = i;
        }
    }

    return furthest_idx;
}
