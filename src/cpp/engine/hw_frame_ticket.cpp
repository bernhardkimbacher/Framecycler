#include "hw_frame_ticket.h"

#include <cstdlib>
#include <cstring>

#if defined(__APPLE__)
#include <CoreVideo/CoreVideo.h>
#endif

#if defined(_WIN32)
#ifndef NOMINMAX
#define NOMINMAX
#endif
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <d3d11.h>
#endif

#if defined(__linux__)
extern "C" {
#include <libavutil/frame.h>
}
#endif

HwFrameTicket::~HwFrameTicket()
{
    reset();
}

HwFrameTicket::HwFrameTicket(HwFrameTicket&& other) noexcept
{
    _kind = other._kind;
    _native = other._native;
    _array_index = other._array_index;
    _width = other._width;
    _height = other._height;
    other._kind = Kind::None;
    other._native = nullptr;
    other._array_index = 0;
    other._width = 0;
    other._height = 0;
}

HwFrameTicket& HwFrameTicket::operator=(HwFrameTicket&& other) noexcept
{
    if (this != &other) {
        reset();
        _kind = other._kind;
        _native = other._native;
        _array_index = other._array_index;
        _width = other._width;
        _height = other._height;
        other._kind = Kind::None;
        other._native = nullptr;
        other._array_index = 0;
        other._width = 0;
        other._height = 0;
    }
    return *this;
}

HwFrameTicket HwFrameTicket::from_cv_pixel_buffer(void* cv_pixel_buffer, int width, int height)
{
    HwFrameTicket t;
    if (!cv_pixel_buffer || width <= 0 || height <= 0) {
        return t;
    }
#if defined(__APPLE__)
    CFRetain(static_cast<CVPixelBufferRef>(cv_pixel_buffer));
    t._kind = Kind::CVPixelBuffer;
    t._native = cv_pixel_buffer;
    t._width = width;
    t._height = height;
#else
    (void)cv_pixel_buffer;
    (void)width;
    (void)height;
#endif
    return t;
}

HwFrameTicket HwFrameTicket::from_d3d11_texture(
    void* texture,
    int array_index,
    int width,
    int height)
{
    HwFrameTicket t;
    if (!texture || width <= 0 || height <= 0 || array_index < 0) {
        return t;
    }
#if defined(_WIN32)
    auto* tex = static_cast<ID3D11Texture2D*>(texture);
    tex->AddRef();
    t._kind = Kind::D3D11Texture2D;
    t._native = texture;
    t._array_index = array_index;
    t._width = width;
    t._height = height;
#else
    (void)texture;
    (void)array_index;
    (void)width;
    (void)height;
#endif
    return t;
}

HwFrameTicket HwFrameTicket::from_drm_prime_avframe(void* av_frame, int width, int height)
{
    HwFrameTicket t;
    if (!av_frame || width <= 0 || height <= 0) {
        return t;
    }
#if defined(__linux__)
    t._kind = Kind::DrmPrimeFrame;
    t._native = av_frame;
    t._width = width;
    t._height = height;
#else
    (void)av_frame;
    (void)width;
    (void)height;
#endif
    return t;
}

void HwFrameTicket::reset()
{
#if defined(__APPLE__)
    if (_kind == Kind::CVPixelBuffer && _native) {
        CFRelease(static_cast<CVPixelBufferRef>(_native));
    }
#endif
#if defined(_WIN32)
    if (_kind == Kind::D3D11Texture2D && _native) {
        static_cast<ID3D11Texture2D*>(_native)->Release();
    }
#endif
#if defined(__linux__)
    if (_kind == Kind::DrmPrimeFrame && _native) {
        AVFrame* frame = static_cast<AVFrame*>(_native);
        av_frame_free(&frame);
        _native = nullptr;
    }
#endif
    _kind = Kind::None;
    _native = nullptr;
    _array_index = 0;
    _width = 0;
    _height = 0;
}

std::mutex& HwFrameDispatch::_mutex()
{
    static std::mutex m;
    return m;
}

std::unordered_map<CacheManager*, HwFrameDispatch::Sink>& HwFrameDispatch::_sinks()
{
    static std::unordered_map<CacheManager*, Sink> sinks;
    return sinks;
}

void HwFrameDispatch::bind(CacheManager* cache, Sink sink)
{
    if (!cache) {
        return;
    }
    std::lock_guard<std::mutex> lock(_mutex());
    if (sink) {
        _sinks()[cache] = std::move(sink);
    } else {
        _sinks().erase(cache);
    }
}

void HwFrameDispatch::unbind(CacheManager* cache)
{
    bind(cache, nullptr);
}

bool HwFrameDispatch::emit(CacheManager* cache, int decoder_frame, HwFrameTicket ticket)
{
    Sink sink;
    {
        std::lock_guard<std::mutex> lock(_mutex());
        auto it = _sinks().find(cache);
        if (it == _sinks().end() || !it->second) {
            return false;
        }
        sink = it->second;
    }
    return sink(decoder_frame, std::move(ticket));
}

bool movie_force_cpu_upload()
{
    const char* v = std::getenv("FRAMECYCLER_MOVIE_CPU_UPLOAD");
    if (!v || !*v) {
        return false;
    }
    return !(std::strcmp(v, "0") == 0 || std::strcmp(v, "false") == 0
        || std::strcmp(v, "False") == 0 || std::strcmp(v, "off") == 0);
}
