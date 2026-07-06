#pragma once
#include <vector>
#include <unordered_map>
#include <mutex>
#include <set>
#include <cstdint>

struct FrameBuffer {
    int width = 0;
    int height = 0;
    int channels = 0;
    std::vector<uint16_t> data;
    bool active = false;
};

class CacheManager {
public:
    CacheManager(double ram_limit_gb);
    ~CacheManager() = default;

    void set_playhead(int playhead, int direction, int in_point, int out_point);
    bool has_frame(int frame_index);

    void write_frame(int frame_index, int width, int height, int channels, const uint16_t* pixel_data, size_t data_size);

    const uint16_t* get_frame_data(int frame_index, int& width, int& height, int& channels);

    std::vector<int> get_cached_frames();
    void clear();
    void set_ram_limit(double ram_limit_gb);

private:
    size_t _find_slot_to_evict(int frame_count);

    double _ram_limit_gb;
    size_t _max_bytes;
    size_t _allocated_bytes;

    std::vector<FrameBuffer> _slots;
    std::unordered_map<int, size_t> _frame_to_slot;
    std::unordered_map<size_t, int> _slot_to_frame;

    int _current_playhead;
    int _play_direction;
    int _in_point;
    int _out_point;

    std::mutex _mutex;
};
