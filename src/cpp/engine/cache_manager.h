#pragma once
#include <deque>
#include <cstdint>
#include <set>
#include <shared_mutex>
#include <string>
#include <unordered_map>
#include <vector>

struct FrameBuffer {
    int width = 0;
    int height = 0;
    int channels = 0;
    std::vector<uint16_t> data;
    bool active = false;
    uint64_t epoch = 0;
};

class CacheManager {
public:
    CacheManager(double ram_limit_gb);
    ~CacheManager() = default;

    void set_playhead(int playhead, int direction, int in_point, int out_point);
    bool has_frame(int frame_index);

    void write_frame(int frame_index, int width, int height, int channels, const uint16_t* pixel_data, size_t data_size);
    bool decode_and_cache_frame(int frame_index, const std::string& file_path, float resolution_scale, const std::string& layer = "", const std::string& fallback_mode = "Flat Gray", int placeholder_width = 0, int placeholder_height = 0);

    // Claim exclusive decode ownership for a frame. Returns false if already cached or claimed.
    bool try_claim_decode(int frame_index);
    void release_decode_claim(int frame_index);
    bool is_decode_claimed(int frame_index) const;

    /// Reserve a slot for ``frame_index`` and return a writable pointer to its
    /// pixel storage. The frame must already be claimed via try_claim_decode.
    /// The returned pointer stays valid until commit_write_slot / release, and
    /// is not visible to readers until commit_write_slot(..., true).
    uint16_t* acquire_write_slot(int frame_index, int width, int height, int channels);
    void commit_write_slot(int frame_index, bool success);

    const uint16_t* get_frame_data(int frame_index, int& width, int& height, int& channels);
    bool get_frame_dimensions(int frame_index, int& width, int& height, int& channels);
    bool copy_frame_data(int frame_index, uint16_t* dest_ptr, size_t dest_size_elements);

    std::vector<int> get_cached_frames();
    void clear();
    void set_ram_limit(double ram_limit_gb);

    size_t allocated_bytes() const;
    size_t max_bytes() const;
    // Bytes of one resident frame (0 if cache empty).
    size_t bytes_per_frame() const;

private:
    size_t _find_slot_to_evict(int frame_count);
    size_t _find_unmapped_inactive_slot() const;
    void _unmap_slot(size_t slot_idx);
    void _release_slot_capacity(size_t slot_idx);

    double _ram_limit_gb;
    size_t _max_bytes;
    size_t _allocated_bytes;
    uint64_t _epoch = 0;

    // deque keeps existing FrameBuffer addresses stable across push_back so
    // in-flight readers of other frames are not invalidated by growth.
    std::deque<FrameBuffer> _slots;
    std::unordered_map<int, size_t> _frame_to_slot;
    std::unordered_map<size_t, int> _slot_to_frame;
    std::set<int> _inflight_decodes;

    int _current_playhead;
    int _play_direction;
    int _in_point;
    int _out_point;

    mutable std::shared_mutex _mutex;
};
