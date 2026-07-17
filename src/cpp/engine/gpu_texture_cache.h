#pragma once

#include <rhi/qrhi.h>
#include <cstdint>
#include <unordered_map>
#include <vector>

class CacheManager;

struct GpuFrameKey {
    int source_index = 0;
    int decoder_frame = 0;

    bool operator==(const GpuFrameKey& other) const {
        return source_index == other.source_index && decoder_frame == other.decoder_frame;
    }
};

struct GpuFrameKeyHash {
    std::size_t operator()(const GpuFrameKey& key) const noexcept {
        return static_cast<std::size_t>(key.source_index) << 32
            ^ static_cast<std::size_t>(key.decoder_frame);
    }
};

struct GpuCacheEntry {
    QRhiTexture* texture = nullptr;
    int width = 0;
    int height = 0;
    int channels = 0;
    size_t bytes = 0;
};

struct SourcePlayhead {
    int playhead = 0;
    int direction = 1;
    int in_point = 0;
    int out_point = 0;
};

class GpuTextureCache {
public:
    struct Stats {
        int hits = 0;
        int misses = 0;
        int evictions = 0;
        size_t resident_bytes = 0;
        int resident_frames = 0;
    };

    GpuTextureCache() = default;
    ~GpuTextureCache();

    void set_rhi(QRhi* rhi);
    void set_limit_gb(double limit_gb);
    double limit_gb() const { return _limit_gb; }
    bool enabled() const { return _max_bytes > 0; }

    void set_source_playhead(int source_index, const SourcePlayhead& playhead);
    void invalidate_source(int source_index);
    void clear();

    // Returns cached texture or nullptr on miss. Validates CPU cache + dimensions.
    QRhiTexture* try_get(
        int source_index,
        int decoder_frame,
        int width,
        int height,
        int channels,
        CacheManager* cpu_cache);

    void put(
        int source_index,
        int decoder_frame,
        int width,
        int height,
        int channels,
        QRhiTexture* texture,
        size_t bytes);

    void evict_before_insert(size_t incoming_bytes, int source_index);
    Stats stats() const { return _stats; }

    // Decoder frame indices currently resident for a viewport source slot.
    std::vector<int> cached_frames_for_source(int source_index) const;

private:
    void destroy_entry(GpuCacheEntry& entry);
    void erase_key(const GpuFrameKey& key);
    int playhead_distance(const GpuFrameKey& key, int source_index) const;

    QRhi* _rhi = nullptr;
    double _limit_gb = 0.0;
    size_t _max_bytes = 0;
    size_t _resident_bytes = 0;
    Stats _stats;
    std::unordered_map<GpuFrameKey, GpuCacheEntry, GpuFrameKeyHash> _entries;
    std::unordered_map<int, SourcePlayhead> _playheads;
};
