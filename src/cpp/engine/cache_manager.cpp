#include "cache_manager.h"
#include <cmath>
#include <algorithm>
#include <iostream>

CacheManager::CacheManager(double ram_limit_gb)
    : _current_playhead(0), _play_direction(1), _in_point(0), _out_point(100), _allocated_bytes(0) {
    set_ram_limit(ram_limit_gb);
}

void CacheManager::set_ram_limit(double ram_limit_gb) {
    std::unique_lock<std::shared_mutex> lock(_mutex);
    _ram_limit_gb = ram_limit_gb;
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

std::vector<int> CacheManager::get_cached_frames() {
    std::shared_lock<std::shared_mutex> lock(_mutex);
    std::vector<int> frames;
    frames.reserve(_frame_to_slot.size());
    for (const auto& pair : _frame_to_slot) {
        frames.push_back(pair.first);
    }
    return frames;
}

void CacheManager::clear() {
    std::unique_lock<std::shared_mutex> lock(_mutex);
    _slots.clear();
    _frame_to_slot.clear();
    _slot_to_frame.clear();
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
