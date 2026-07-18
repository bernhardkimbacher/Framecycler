#include "cache_manager.h"
#include "native_decoder.h"
#include <cmath>
#include <algorithm>
#include <iostream>
#include <mutex>

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
    return _frame_to_slot.find(frame_index) != _frame_to_slot.end();
}

void CacheManager::write_frame(int frame_index, int width, int height, int channels, const uint16_t* pixel_data, size_t data_size) {
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
        return;
    }

    size_t req_bytes = data_size * sizeof(uint16_t);

    size_t target_slot_idx = static_cast<size_t>(-1);
    if (_allocated_bytes + req_bytes > _max_bytes) {
        int frame_count = std::max(1, _out_point - _in_point + 1);
        target_slot_idx = _find_slot_to_evict(frame_count);
    }

    if (target_slot_idx == static_cast<size_t>(-1)) {
        FrameBuffer new_slot;
        new_slot.data.resize(data_size);
        std::copy(pixel_data, pixel_data + data_size, new_slot.data.begin());
        new_slot.width = width;
        new_slot.height = height;
        new_slot.channels = channels;
        new_slot.active = true;

        _slots.push_back(new_slot);
        size_t new_idx = _slots.size() - 1;
        _frame_to_slot[frame_index] = new_idx;
        _slot_to_frame[new_idx] = frame_index;
        _allocated_bytes += req_bytes;
    } else {
        auto& slot = _slots[target_slot_idx];

        int old_frame = _slot_to_frame[target_slot_idx];
        _frame_to_slot.erase(old_frame);

        _allocated_bytes -= slot.data.size() * sizeof(uint16_t);

        if (slot.data.size() < data_size) {
            slot.data.resize(data_size);
        }
        std::copy(pixel_data, pixel_data + data_size, slot.data.begin());

        slot.width = width;
        slot.height = height;
        slot.channels = channels;
        slot.active = true;

        _frame_to_slot[frame_index] = target_slot_idx;
        _slot_to_frame[target_slot_idx] = frame_index;
        _allocated_bytes += slot.data.size() * sizeof(uint16_t);
    }
}

bool CacheManager::try_claim_decode(int frame_index) {
    std::unique_lock<std::shared_mutex> lock(_mutex);
    if (_frame_to_slot.find(frame_index) != _frame_to_slot.end()) {
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
    NativeDecoder::DecodeResult res;
    try {
        res = NativeDecoder::decode_frame(file_path, resolution_scale, layer, fallback_mode, placeholder_width, placeholder_height);
        write_frame(frame_index, res.width, res.height, res.channels, res.pixel_data.data(), res.pixel_data.size());
    } catch (...) {
        release_decode_claim(frame_index);
        throw;
    }
    release_decode_claim(frame_index);
    return res.success;
}

const uint16_t* CacheManager::get_frame_data(int frame_index, int& width, int& height, int& channels) {
    std::shared_lock<std::shared_mutex> lock(_mutex);
    auto it = _frame_to_slot.find(frame_index);
    if (it == _frame_to_slot.end()) {
        return nullptr;
    }
    size_t idx = it->second;
    const auto& slot = _slots[idx];
    width = slot.width;
    height = slot.height;
    channels = slot.channels;
    return slot.data.data();
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

std::vector<int> CacheManager::get_cached_frames() {
    std::shared_lock<std::shared_mutex> lock(_mutex);
    std::vector<int> frames;
    frames.reserve(_frame_to_slot.size());
    for (const auto& pair : _frame_to_slot) {
        frames.push_back(pair.first);
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
    _slots.clear();
    _frame_to_slot.clear();
    _slot_to_frame.clear();
    _inflight_decodes.clear();
    _allocated_bytes = 0;
}

size_t CacheManager::_find_slot_to_evict(int frame_count) {
    if (_slots.empty()) {
        return static_cast<size_t>(-1);
    }

    for (size_t i = 0; i < _slots.size(); ++i) {
        if (!_slots[i].active) {
            return i;
        }
    }

    size_t furthest_idx = 0;
    int max_distance = -1;

    for (size_t i = 0; i < _slots.size(); ++i) {
        int frame_num = _slot_to_frame[i];
        int direct_dist = std::abs(frame_num - _current_playhead);
        int wrapped_dist = std::abs(frame_count - direct_dist);
        int dist = std::min(direct_dist, wrapped_dist);

        if (dist > max_distance) {
            max_distance = dist;
            furthest_idx = i;
        }
    }

    return furthest_idx;
}
