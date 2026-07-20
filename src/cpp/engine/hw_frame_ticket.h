#pragma once

#include <cstddef>
#include <cstdint>
#include <functional>
#include <mutex>
#include <unordered_map>
#include <utility>

class CacheManager;

/// Retained native HW decode surface (CVPixelBuffer / D3D11 / DRM PRIME).
/// Thread-safe retain/release; move-only.
class HwFrameTicket {
public:
    enum class Kind {
        None = 0,
        CVPixelBuffer = 1,
        D3D11Texture2D = 2,
        DrmPrimeFrame = 3, // owns AVFrame* with AV_PIX_FMT_DRM_PRIME
    };

    HwFrameTicket() = default;
    ~HwFrameTicket();

    HwFrameTicket(const HwFrameTicket&) = delete;
    HwFrameTicket& operator=(const HwFrameTicket&) = delete;
    HwFrameTicket(HwFrameTicket&& other) noexcept;
    HwFrameTicket& operator=(HwFrameTicket&& other) noexcept;

    static HwFrameTicket from_cv_pixel_buffer(void* cv_pixel_buffer, int width, int height);
    /// ``texture`` is AddRef'd; ``array_index`` is the D3D11VA texture array slice.
    static HwFrameTicket from_d3d11_texture(
        void* texture,
        int array_index,
        int width,
        int height);
    /// Takes ownership of ``av_frame`` (AVFrame* with DRM_PRIME data).
    static HwFrameTicket from_drm_prime_avframe(void* av_frame, int width, int height);

    bool valid() const { return _kind != Kind::None && _native != nullptr; }
    Kind kind() const { return _kind; }
    void* native() const { return _native; }
    int array_index() const { return _array_index; }
    int width() const { return _width; }
    int height() const { return _height; }

    void reset();

private:
    Kind _kind = Kind::None;
    void* _native = nullptr;
    int _array_index = 0;
    int _width = 0;
    int _height = 0;
};

/// Routes PrefetchEngine HW tickets to the RhiRenderer for a given CacheManager.
class HwFrameDispatch {
public:
    /// Return true if the ticket was accepted (queued for GPU import).
    using Sink = std::function<bool(int decoder_frame, HwFrameTicket ticket)>;

    static void bind(CacheManager* cache, Sink sink);
    static void unbind(CacheManager* cache);
    /// True when a sink accepted the ticket. False → caller should CPU-decode.
    static bool emit(CacheManager* cache, int decoder_frame, HwFrameTicket ticket);

private:
    static std::mutex& _mutex();
    static std::unordered_map<CacheManager*, Sink>& _sinks();
};

/// True when FRAMECYCLER_MOVIE_CPU_UPLOAD is set to a truthy value.
bool movie_force_cpu_upload();
