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
    _resident_bytes = 0;
    _stats.resident_bytes = 0;
    _stats.resident_frames = 0;
}

void GpuTextureCache::destroy_entry(GpuCacheEntry& entry)
{
    if (entry.texture) {
        entry.texture->destroy();
        delete entry.texture;
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

    if (!cpu_cache || !cpu_cache->has_frame(decoder_frame)) {
        erase_key(key);
        _stats.misses++;
        return nullptr;
    }

    const GpuCacheEntry& entry = it->second;
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
    size_t bytes)
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
    _entries[key] = entry;
    _resident_bytes += bytes;
    _stats.resident_bytes = _resident_bytes;
    _stats.resident_frames = static_cast<int>(_entries.size());
}
