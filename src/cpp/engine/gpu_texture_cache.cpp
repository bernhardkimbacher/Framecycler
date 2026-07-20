#include "gpu_texture_cache.h"
#include "cache_manager.h"

#include <algorithm>
#include <cmath>

GpuTextureCache::~GpuTextureCache()
{
    clear();
}

void GpuTextureCache::set_rhi(QRhi* rhi)
{
    _rhi = rhi;
}

void GpuTextureCache::set_limit_gb(double limit_gb)
{
    _limit_gb = limit_gb;
    if (limit_gb <= 0.0) {
        _max_bytes = 0;
        clear();
        return;
    }
    _max_bytes = static_cast<size_t>(limit_gb * 1024.0 * 1024.0 * 1024.0);
    // Free pooled textures first when shrinking budget.
    clear_pool();
    evict_before_insert(0, -1);
}

void GpuTextureCache::set_source_playhead(int source_index, const SourcePlayhead& playhead)
{
    _playheads[source_index] = playhead;
}

void GpuTextureCache::invalidate_source(int source_index)
{
    std::vector<GpuFrameKey> to_remove;
    to_remove.reserve(_entries.size());
    for (const auto& pair : _entries) {
        if (pair.first.source_index == source_index) {
            to_remove.push_back(pair.first);
        }
    }
    for (const auto& key : to_remove) {
        erase_key(key);
    }
    _playheads.erase(source_index);
}

std::vector<int> GpuTextureCache::cached_frames_for_source(int source_index) const
{
    std::vector<int> frames;
    frames.reserve(_entries.size());
    for (const auto& pair : _entries) {
        if (pair.first.source_index == source_index) {
            frames.push_back(pair.first.decoder_frame);
        }
    }
    std::sort(frames.begin(), frames.end());
    return frames;
}

bool GpuTextureCache::contains(int source_index, int decoder_frame) const
{
    return _entries.find(GpuFrameKey{source_index, decoder_frame}) != _entries.end();
}

bool GpuTextureCache::try_get_dimensions(
    int source_index,
    int decoder_frame,
    int& width,
    int& height,
    int& channels) const
{
    auto it = _entries.find(GpuFrameKey{source_index, decoder_frame});
    if (it == _entries.end() || !it->second.texture) {
        return false;
    }
    width = it->second.width;
    height = it->second.height;
    channels = it->second.channels;
    return width > 0 && height > 0 && channels > 0;
}

bool GpuTextureCache::playhead_for_source(int source_index, SourcePlayhead& out) const
{
    auto it = _playheads.find(source_index);
    if (it == _playheads.end()) {
        return false;
    }
    out = it->second;
    return true;
}

void GpuTextureCache::clear()
{
    for (auto& pair : _entries) {
        destroy_entry(pair.second);
    }
    _entries.clear();
    clear_pool();
    _resident_bytes = 0;
    _stats.resident_bytes = 0;
    _stats.resident_frames = 0;
}

void GpuTextureCache::destroy_texture(QRhiTexture* texture)
{
    if (!texture) {
        return;
    }
    texture->destroy();
    delete texture;
}

void GpuTextureCache::clear_pool()
{
    for (auto& pair : _pool) {
        for (QRhiTexture* tex : pair.second) {
            destroy_texture(tex);
        }
    }
    _pool.clear();
    _pooled_count = 0;
    _stats.textures_pooled = 0;
}

QRhiTexture* GpuTextureCache::acquire(int width, int height, QRhiTexture::Format format)
{
    if (!_rhi || width <= 0 || height <= 0) {
        return nullptr;
    }
    TexturePoolKey key{width, height, format};
    auto it = _pool.find(key);
    if (it != _pool.end() && !it->second.empty()) {
        QRhiTexture* tex = it->second.back();
        it->second.pop_back();
        --_pooled_count;
        _stats.textures_pooled = static_cast<int>(_pooled_count);
        _stats.textures_pooled_reuses++;
        return tex;
    }

    QRhiTexture* texture = _rhi->newTexture(format, QSize(width, height));
    if (!texture || !texture->create()) {
        delete texture;
        return nullptr;
    }
    _stats.textures_created++;
    return texture;
}

void GpuTextureCache::release_to_pool(
    QRhiTexture* texture, int width, int height, QRhiTexture::Format format)
{
    if (!texture) {
        return;
    }
    if (_pooled_count >= kMaxPooledTextures || width <= 0 || height <= 0) {
        destroy_texture(texture);
        return;
    }
    TexturePoolKey key{width, height, format};
    _pool[key].push_back(texture);
    ++_pooled_count;
    _stats.textures_pooled = static_cast<int>(_pooled_count);
}

void GpuTextureCache::destroy_entry(GpuCacheEntry& entry)
{
    if (entry.texture) {
        QRhiTexture::Format format =
            (entry.channels == 1) ? QRhiTexture::R16F : QRhiTexture::RGBA16F;
        release_to_pool(entry.texture, entry.width, entry.height, format);
        entry.texture = nullptr;
    }
    entry.bytes = 0;
}

void GpuTextureCache::erase_key(const GpuFrameKey& key)
{
    auto it = _entries.find(key);
    if (it == _entries.end()) {
        return;
    }
    _resident_bytes -= it->second.bytes;
    destroy_entry(it->second);
    _entries.erase(it);
    _stats.evictions++;
    _stats.resident_bytes = _resident_bytes;
    _stats.resident_frames = static_cast<int>(_entries.size());
}

int GpuTextureCache::playhead_distance(const GpuFrameKey& key, int source_index) const
{
    auto it = _playheads.find(source_index);
    if (it == _playheads.end()) {
        return 0;
    }
    const SourcePlayhead& ph = it->second;
    int frame_count = std::max(1, ph.out_point - ph.in_point + 1);
    int direct_dist = std::abs(key.decoder_frame - ph.playhead);
    int wrapped_dist = std::abs(frame_count - direct_dist);
    return std::min(direct_dist, wrapped_dist);
}

void GpuTextureCache::evict_before_insert(size_t incoming_bytes, int source_index)
{
    if (_max_bytes == 0) {
        clear();
        return;
    }

    while (!_entries.empty() && (_resident_bytes + incoming_bytes > _max_bytes)) {
        GpuFrameKey victim_key{};
        int max_distance = -1;
        bool found = false;

        for (const auto& pair : _entries) {
            if (source_index >= 0 && pair.first.source_index != source_index) {
                continue;
            }
            int dist = playhead_distance(pair.first, pair.first.source_index);
            if (dist > max_distance) {
                max_distance = dist;
                victim_key = pair.first;
                found = true;
            }
        }

        if (!found) {
            for (const auto& pair : _entries) {
                int dist = playhead_distance(pair.first, pair.first.source_index);
                if (dist > max_distance) {
                    max_distance = dist;
                    victim_key = pair.first;
                    found = true;
                }
            }
        }

        if (!found) {
            break;
        }
        erase_key(victim_key);
    }
}

QRhiTexture* GpuTextureCache::try_get(
    int source_index,
    int decoder_frame,
    int width,
    int height,
    int channels,
    CacheManager* cpu_cache)
{
    if (_max_bytes == 0) {
        return nullptr;
    }

    GpuFrameKey key{source_index, decoder_frame};
    auto it = _entries.find(key);
    if (it == _entries.end()) {
        _stats.misses++;
        return nullptr;
    }

    const GpuCacheEntry& entry = it->second;
    if (!entry.gpu_only) {
        if (!cpu_cache || !cpu_cache->has_frame(decoder_frame)) {
            erase_key(key);
            _stats.misses++;
            return nullptr;
        }
    }

    if (entry.width != width || entry.height != height || entry.channels != channels || !entry.texture) {
        erase_key(key);
        _stats.misses++;
        return nullptr;
    }

    _stats.hits++;
    return entry.texture;
}

void GpuTextureCache::put(
    int source_index,
    int decoder_frame,
    int width,
    int height,
    int channels,
    QRhiTexture* texture,
    size_t bytes,
    bool gpu_only)
{
    if (_max_bytes == 0 || !texture) {
        return;
    }

    GpuFrameKey key{source_index, decoder_frame};
    auto it = _entries.find(key);
    if (it != _entries.end()) {
        _resident_bytes -= it->second.bytes;
        destroy_entry(it->second);
        _entries.erase(it);
    } else {
        evict_before_insert(bytes, source_index);
    }

    GpuCacheEntry entry;
    entry.texture = texture;
    entry.width = width;
    entry.height = height;
    entry.channels = channels;
    entry.bytes = bytes;
    entry.gpu_only = gpu_only;
    _entries[key] = entry;
    _resident_bytes += bytes;
    _stats.resident_bytes = _resident_bytes;
    _stats.resident_frames = static_cast<int>(_entries.size());
}
