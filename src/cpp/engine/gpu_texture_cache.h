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
    /// Optional chroma plane for NV12 direct-sample entries (texB bind).
    QRhiTexture* texture_uv = nullptr;
    /// 0 = RGBA16F, 1 = NV12 planes, 2 = BGRA8 wrapped.
    int sample_mode = 0;
    /// Retained CVPixelBuffer (Metal) keeping plane textures alive; CFRelease on destroy.
    void* retained_cv_pixel_buffer = nullptr;
    int width = 0;
    int height = 0;
    int channels = 0;
    size_t bytes = 0;
    /// When true, entry does not require matching CPU CacheManager residency.
    bool gpu_only = false;
};

struct SourcePlayhead {
    int playhead = 0;
    int direction = 1;
    int in_point = 0;
    int out_point = 0;
};

struct TexturePoolKey {
    int width = 0;
    int height = 0;
    QRhiTexture::Format format = QRhiTexture::RGBA16F;

    bool operator==(const TexturePoolKey& other) const {
        return width == other.width && height == other.height && format == other.format;
    }
};

struct TexturePoolKeyHash {
    std::size_t operator()(const TexturePoolKey& key) const noexcept {
        return (static_cast<std::size_t>(key.width) * 1315423911u)
            ^ (static_cast<std::size_t>(key.height) * 2654435761u)
            ^ static_cast<std::size_t>(key.format);
    }
};

class GpuTextureCache {
public:
    static constexpr size_t kMaxPooledTextures = 8;

    struct Stats {
        int hits = 0;
        int misses = 0;
        int evictions = 0;
        size_t resident_bytes = 0;
        int resident_frames = 0;
        int textures_created = 0;
        int textures_pooled_reuses = 0;
        int textures_pooled = 0;
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

    // Returns cached texture or nullptr on miss. Validates CPU cache + dimensions
    // unless the entry was inserted as gpu_only.
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
        size_t bytes,
        bool gpu_only = false);

    /// Planar / wrapped HW entry (sample_mode 1=NV12, 2=BGRA). texture_uv may be null.
    void put_planar(
        int source_index,
        int decoder_frame,
        int width,
        int height,
        int channels,
        QRhiTexture* texture,
        QRhiTexture* texture_uv,
        int sample_mode,
        void* retained_cv_pixel_buffer,
        size_t bytes);

    /// Like try_get, but also returns planar bind info when present.
    QRhiTexture* try_get_ex(
        int source_index,
        int decoder_frame,
        int width,
        int height,
        int channels,
        CacheManager* cpu_cache,
        QRhiTexture** texture_uv_out,
        int* sample_mode_out);

    void evict_before_insert(size_t incoming_bytes, int source_index);
    Stats stats() const { return _stats; }

    // Decoder frame indices currently resident for a viewport source slot.
    std::vector<int> cached_frames_for_source(int source_index) const;

    bool contains(int source_index, int decoder_frame) const;

    /// Dimensions for a resident GPU entry (incl. gpu_only). Returns false on miss.
    bool try_get_dimensions(
        int source_index,
        int decoder_frame,
        int& width,
        int& height,
        int& channels) const;

    size_t max_bytes() const { return _max_bytes; }
    size_t resident_bytes() const { return _resident_bytes; }
    bool playhead_for_source(int source_index, SourcePlayhead& out) const;

    /// Acquire a texture of the given size/format from the free-list, or create one.
    QRhiTexture* acquire(int width, int height, QRhiTexture::Format format);
    /// Return a texture to the free-list (or destroy if the pool is full / mismatched).
    void release_to_pool(QRhiTexture* texture, int width, int height, QRhiTexture::Format format);
    void clear_pool();

private:
    void destroy_entry(GpuCacheEntry& entry);
    void erase_key(const GpuFrameKey& key);
    int playhead_distance(const GpuFrameKey& key, int source_index) const;
    void destroy_texture(QRhiTexture* texture);

    QRhi* _rhi = nullptr;
    double _limit_gb = 0.0;
    size_t _max_bytes = 0;
    size_t _resident_bytes = 0;
    Stats _stats;
    std::unordered_map<GpuFrameKey, GpuCacheEntry, GpuFrameKeyHash> _entries;
    std::unordered_map<int, SourcePlayhead> _playheads;
    std::unordered_map<TexturePoolKey, std::vector<QRhiTexture*>, TexturePoolKeyHash> _pool;
    size_t _pooled_count = 0;
};
