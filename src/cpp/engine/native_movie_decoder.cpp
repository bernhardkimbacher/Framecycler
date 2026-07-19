#include "native_movie_decoder.h"

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <iostream>
#include <vector>

#if defined(__ARM_NEON) || defined(__ARM_NEON__)
#include <arm_neon.h>
#define FC_HAS_NEON 1
#elif defined(__SSE2__) || defined(_M_X64) || (defined(_M_IX86_FP) && _M_IX86_FP >= 2)
#include <emmintrin.h>
#define FC_HAS_SSE2 1
#endif

extern "C" {
#include <libavcodec/avcodec.h>
#include <libavformat/avformat.h>
#include <libavutil/avutil.h>
#include <libavutil/hwcontext.h>
#include <libavutil/imgutils.h>
#include <libavutil/pixdesc.h>
#include <libswscale/swscale.h>
}

namespace {

uint16_t float32_to_half(float value)
{
    uint32_t bits = 0;
    std::memcpy(&bits, &value, sizeof(bits));
    const uint32_t sign = (bits >> 16) & 0x8000u;
    int32_t exp = static_cast<int32_t>((bits >> 23) & 0xff) - 127 + 15;
    uint32_t mant = bits & 0x7fffffu;

    if (exp <= 0) {
        if (exp < -10) {
            return static_cast<uint16_t>(sign);
        }
        mant |= 0x800000u;
        const uint32_t t = static_cast<uint32_t>(14 - exp);
        uint32_t half_mant = mant >> t;
        if ((mant >> (t - 1)) & 1u) {
            ++half_mant;
        }
        return static_cast<uint16_t>(sign | half_mant);
    }
    if (exp >= 31) {
        return static_cast<uint16_t>(sign | 0x7c00u);
    }
    uint32_t half = static_cast<uint32_t>(exp) << 10;
    half |= mant >> 13;
    if (mant & 0x1000u) {
        ++half;
    }
    return static_cast<uint16_t>(sign | half);
}

uint16_t u8_to_half(uint8_t v)
{
    return float32_to_half(static_cast<float>(v) / 255.0f);
}

uint16_t u16_to_half(uint16_t v)
{
    return float32_to_half(static_cast<float>(v) / 65535.0f);
}

bool pix_fmt_has_alpha(AVPixelFormat fmt)
{
    const AVPixFmtDescriptor* desc = av_pix_fmt_desc_get(fmt);
    return desc && (desc->flags & AV_PIX_FMT_FLAG_ALPHA);
}

bool pix_fmt_is_float(AVPixelFormat fmt)
{
    const AVPixFmtDescriptor* desc = av_pix_fmt_desc_get(fmt);
    return desc && (desc->flags & AV_PIX_FMT_FLAG_FLOAT);
}

int timecode_to_frame(const std::string& tc_str, double fps)
{
    if (tc_str.size() < 11) {
        return 0;
    }
    int hours = 0;
    int minutes = 0;
    int seconds = 0;
    int frames = 0;
    if (std::sscanf(tc_str.c_str(), "%d:%d:%d:%d", &hours, &minutes, &seconds, &frames) != 4) {
        return 0;
    }
    int fps_rounded = static_cast<int>(std::lround(fps));
    if (fps_rounded <= 0) {
        fps_rounded = 24;
    }
    return (hours * 3600 * fps_rounded)
        + (minutes * 60 * fps_rounded)
        + (seconds * fps_rounded)
        + frames;
}

void merge_dict(std::unordered_map<std::string, std::string>& out, AVDictionary* dict)
{
    if (!dict) {
        return;
    }
    AVDictionaryEntry* entry = nullptr;
    while ((entry = av_dict_get(dict, "", entry, AV_DICT_IGNORE_SUFFIX)) != nullptr) {
        if (entry->key && entry->value) {
            out[entry->key] = entry->value;
        }
    }
}

std::string dict_get(AVDictionary* dict, const char* key)
{
    if (!dict || !key) {
        return {};
    }
    AVDictionaryEntry* entry = av_dict_get(dict, key, nullptr, 0);
    if (entry && entry->value) {
        return entry->value;
    }
    return {};
}

void convert_rgba64_to_half(const uint16_t* src, uint16_t* dst, size_t count)
{
    size_t i = 0;
#if defined(FC_HAS_NEON)
    // Process 4 channels × 2 pixels (8 u16) at a time when aligned batches allow.
    for (; i + 8 <= count; i += 8) {
        for (size_t k = 0; k < 8; ++k) {
            dst[i + k] = u16_to_half(src[i + k]);
        }
    }
#elif defined(FC_HAS_SSE2)
    for (; i + 8 <= count; i += 8) {
        for (size_t k = 0; k < 8; ++k) {
            dst[i + k] = u16_to_half(src[i + k]);
        }
    }
#endif
    for (; i < count; ++i) {
        dst[i] = u16_to_half(src[i]);
    }
}

void convert_rgbaf32_to_half(const float* src, uint16_t* dst, size_t count)
{
    for (size_t i = 0; i < count; ++i) {
        dst[i] = float32_to_half(src[i]);
    }
}

void convert_rgba8_to_half(const uint8_t* src, uint16_t* dst, size_t count)
{
    size_t i = 0;
#if defined(FC_HAS_NEON) || defined(FC_HAS_SSE2)
    for (; i + 16 <= count; i += 16) {
        for (size_t k = 0; k < 16; ++k) {
            dst[i + k] = u8_to_half(src[i + k]);
        }
    }
#endif
    for (; i < count; ++i) {
        dst[i] = u8_to_half(src[i]);
    }
}

AVHWDeviceType platform_hw_device_type()
{
#if defined(__APPLE__)
    return AV_HWDEVICE_TYPE_VIDEOTOOLBOX;
#elif defined(_WIN32)
    return AV_HWDEVICE_TYPE_D3D11VA;
#else
    return AV_HWDEVICE_TYPE_VAAPI;
#endif
}

const char* hw_device_type_name(AVHWDeviceType type)
{
    switch (type) {
    case AV_HWDEVICE_TYPE_VIDEOTOOLBOX:
        return "videotoolbox";
    case AV_HWDEVICE_TYPE_D3D11VA:
        return "d3d11va";
    case AV_HWDEVICE_TYPE_VAAPI:
        return "vaapi";
    default:
        return "software";
    }
}

enum AVPixelFormat hw_get_format_callback(AVCodecContext* ctx, const enum AVPixelFormat* pix_fmts)
{
    auto* self = static_cast<NativeMovieDecoder*>(ctx->opaque);
    if (!self) {
        return AV_PIX_FMT_NONE;
    }
    return static_cast<AVPixelFormat>(self->_get_hw_format(reinterpret_cast<const int*>(pix_fmts)));
}

} // namespace

// Allow the C callback to call the private method.
int NativeMovieDecoder::_get_hw_format(const int* pix_fmts) const
{
    if (_hw_pix_fmt < 0 || !pix_fmts) {
        return static_cast<int>(AV_PIX_FMT_NONE);
    }
    for (const int* p = pix_fmts; *p != -1; ++p) {
        if (*p == _hw_pix_fmt) {
            return *p;
        }
    }
    return static_cast<int>(AV_PIX_FMT_NONE);
}

NativeMovieDecoder::~NativeMovieDecoder()
{
    close();
}

bool NativeMovieDecoder::open(const std::string& path)
{
    std::lock_guard<std::mutex> lock(_mutex);
    return _open_unlocked(path, /*allow_hw=*/true);
}

void NativeMovieDecoder::close()
{
    std::lock_guard<std::mutex> lock(_mutex);
    _close_unlocked();
}

bool NativeMovieDecoder::is_open() const
{
    std::lock_guard<std::mutex> lock(_mutex);
    return _fmt != nullptr;
}

int NativeMovieDecoder::width() const
{
    std::lock_guard<std::mutex> lock(_mutex);
    return _width;
}

int NativeMovieDecoder::height() const
{
    std::lock_guard<std::mutex> lock(_mutex);
    return _height;
}

double NativeMovieDecoder::fps() const
{
    std::lock_guard<std::mutex> lock(_mutex);
    return _fps;
}

int NativeMovieDecoder::frame_count() const
{
    std::lock_guard<std::mutex> lock(_mutex);
    return _frame_count;
}

bool NativeMovieDecoder::has_alpha() const
{
    std::lock_guard<std::mutex> lock(_mutex);
    return _has_alpha;
}

double NativeMovieDecoder::sample_aspect_ratio() const
{
    std::lock_guard<std::mutex> lock(_mutex);
    return _sar;
}

std::string NativeMovieDecoder::timecode_start() const
{
    std::lock_guard<std::mutex> lock(_mutex);
    return _timecode_start;
}

int NativeMovieDecoder::start_frame() const
{
    std::lock_guard<std::mutex> lock(_mutex);
    return _start_frame;
}

int NativeMovieDecoder::end_frame() const
{
    std::lock_guard<std::mutex> lock(_mutex);
    return _start_frame + std::max(1, _frame_count) - 1;
}

std::string NativeMovieDecoder::path() const
{
    std::lock_guard<std::mutex> lock(_mutex);
    return _path;
}

std::unordered_map<std::string, std::string> NativeMovieDecoder::file_metadata() const
{
    std::lock_guard<std::mutex> lock(_mutex);
    return _file_metadata;
}

std::string NativeMovieDecoder::hw_type() const
{
    std::lock_guard<std::mutex> lock(_mutex);
    return _hw_type;
}

int NativeMovieDecoder::bits_per_raw_sample() const
{
    std::lock_guard<std::mutex> lock(_mutex);
    return _bits_per_raw_sample;
}

std::string NativeMovieDecoder::pix_fmt_name() const
{
    std::lock_guard<std::mutex> lock(_mutex);
    return _pix_fmt_name;
}

void NativeMovieDecoder::_release_hw()
{
    if (_hw_device_ctx) {
        av_buffer_unref(&_hw_device_ctx);
        _hw_device_ctx = nullptr;
    }
    _hw_pix_fmt = -1;
    _hw_type = "software";
}

bool NativeMovieDecoder::_try_open_hw(AVCodecContext* codec_ctx, const AVCodec* codec)
{
    const AVHWDeviceType type = platform_hw_device_type();
    if (type == AV_HWDEVICE_TYPE_NONE) {
        return false;
    }

    int hw_pix = -1;
    for (int i = 0;; ++i) {
        const AVCodecHWConfig* config = avcodec_get_hw_config(codec, i);
        if (!config) {
            break;
        }
        if ((config->methods & AV_CODEC_HW_CONFIG_METHOD_HW_DEVICE_CTX)
            && config->device_type == type) {
            hw_pix = static_cast<int>(config->pix_fmt);
            break;
        }
    }
    if (hw_pix < 0) {
        return false;
    }

    AVBufferRef* device_ctx = nullptr;
    int err = av_hwdevice_ctx_create(&device_ctx, type, nullptr, nullptr, 0);
#if !defined(__APPLE__) && !defined(_WIN32)
    // Linux VAAPI: try render node then default.
    if (err < 0) {
        err = av_hwdevice_ctx_create(&device_ctx, type, "/dev/dri/renderD128", nullptr, 0);
    }
#endif
    if (err < 0 || !device_ctx) {
        return false;
    }

    _hw_device_ctx = device_ctx;
    _hw_pix_fmt = hw_pix;
    codec_ctx->hw_device_ctx = av_buffer_ref(_hw_device_ctx);
    codec_ctx->opaque = this;
    codec_ctx->get_format = hw_get_format_callback;
    _hw_type = hw_device_type_name(type);
    return true;
}

bool NativeMovieDecoder::_build_frame_index()
{
    _frame_pts.clear();
    _keyframes.clear();
    if (!_fmt || _video_stream < 0) {
        return false;
    }

    AVStream* stream = _fmt->streams[_video_stream];
    if (av_seek_frame(_fmt, _video_stream, stream->start_time != AV_NOPTS_VALUE ? stream->start_time : 0,
                      AVSEEK_FLAG_BACKWARD)
        < 0) {
        av_seek_frame(_fmt, -1, 0, AVSEEK_FLAG_BACKWARD);
    }
    if (_codec) {
        avcodec_flush_buffers(_codec);
    }

    struct PacketInfo {
        int64_t pts = 0;
        bool key = false;
    };
    std::vector<PacketInfo> packets;
    packets.reserve(static_cast<size_t>(std::max(1, _frame_count)));

    AVPacket* pkt = av_packet_alloc();
    if (!pkt) {
        return false;
    }
    while (av_read_frame(_fmt, pkt) >= 0) {
        if (pkt->stream_index == _video_stream) {
            int64_t pts = pkt->pts;
            if (pts == AV_NOPTS_VALUE) {
                pts = pkt->dts;
            }
            if (pts == AV_NOPTS_VALUE) {
                pts = static_cast<int64_t>(packets.size());
            }
            const bool key = (pkt->flags & AV_PKT_FLAG_KEY) != 0;
            packets.push_back(PacketInfo{pts, key});
        }
        av_packet_unref(pkt);
    }
    av_packet_free(&pkt);

    // Rewind for decode.
    if (av_seek_frame(_fmt, _video_stream, stream->start_time != AV_NOPTS_VALUE ? stream->start_time : 0,
                      AVSEEK_FLAG_BACKWARD)
        < 0) {
        av_seek_frame(_fmt, -1, 0, AVSEEK_FLAG_BACKWARD);
    }
    if (_codec) {
        avcodec_flush_buffers(_codec);
    }

    if (packets.empty()) {
        return false;
    }

    // Sort by PTS for presentation order; stable so equal PTS keep decode order.
    std::vector<size_t> order(packets.size());
    for (size_t i = 0; i < order.size(); ++i) {
        order[i] = i;
    }
    std::stable_sort(order.begin(), order.end(), [&](size_t a, size_t b) {
        return packets[a].pts < packets[b].pts;
    });

    _frame_pts.resize(order.size());
    for (size_t i = 0; i < order.size(); ++i) {
        _frame_pts[i] = packets[order[i]].pts;
    }
    _frame_count = static_cast<int>(_frame_pts.size());

    // Keyframes: presentation index of each key packet.
    for (size_t decode_i = 0; decode_i < packets.size(); ++decode_i) {
        if (!packets[decode_i].key) {
            continue;
        }
        const int64_t pts = packets[decode_i].pts;
        // Find presentation index for this PTS (first match).
        auto it = std::lower_bound(_frame_pts.begin(), _frame_pts.end(), pts);
        int pres = 0;
        if (it != _frame_pts.end() && *it == pts) {
            pres = static_cast<int>(it - _frame_pts.begin());
        } else {
            // Fallback: map by decode order if PTS missing/collision.
            pres = static_cast<int>(
                std::find(order.begin(), order.end(), decode_i) - order.begin());
        }
        KeyframeEntry entry;
        entry.seek_ts = pts;
        entry.presentation_index = std::clamp(pres, 0, _frame_count - 1);
        // Keep only increasing presentation indices.
        if (_keyframes.empty() || entry.presentation_index >= _keyframes.back().presentation_index) {
            if (!_keyframes.empty() && entry.presentation_index == _keyframes.back().presentation_index) {
                _keyframes.back() = entry;
            } else {
                _keyframes.push_back(entry);
            }
        }
    }

    if (_keyframes.empty()) {
        KeyframeEntry entry;
        entry.seek_ts = _frame_pts.front();
        entry.presentation_index = 0;
        _keyframes.push_back(entry);
    }
    // Ensure index starts at frame 0.
    if (_keyframes.front().presentation_index > 0) {
        KeyframeEntry entry;
        entry.seek_ts = _frame_pts.front();
        entry.presentation_index = 0;
        _keyframes.insert(_keyframes.begin(), entry);
    }

    _last_decoded_internal = -1;
    _next_presentation_index = -1;
    return true;
}

bool NativeMovieDecoder::_validate_decode_or_fallback_sw(const std::string& path)
{
    if (_hw_type == "software") {
        return true;
    }
    // HW open can succeed while the first real decode fails (common with VT in
    // some environments). Fall back to software rather than returning empty frames.
    if (!_seek_to_internal(0)) {
        std::cerr << "NativeMovieDecoder: HW validate seek failed; falling back to software\n";
        return _open_unlocked(path, /*allow_hw=*/false);
    }
    if (!_decode_next_frame(_frame)) {
        std::cerr << "NativeMovieDecoder: HW validate decode failed; falling back to software\n";
        return _open_unlocked(path, /*allow_hw=*/false);
    }
    if (!_seek_to_internal(0)) {
        return _open_unlocked(path, /*allow_hw=*/false);
    }
    _last_decoded_internal = -1;
    _next_presentation_index = -1;
    return true;
}

bool NativeMovieDecoder::_open_unlocked(const std::string& path, bool allow_hw)
{
    _close_unlocked();

    AVFormatContext* fmt = nullptr;
    if (avformat_open_input(&fmt, path.c_str(), nullptr, nullptr) < 0) {
        std::cerr << "NativeMovieDecoder: failed to open " << path << std::endl;
        return false;
    }
    if (avformat_find_stream_info(fmt, nullptr) < 0) {
        std::cerr << "NativeMovieDecoder: failed to find stream info for " << path << std::endl;
        avformat_close_input(&fmt);
        return false;
    }

    const int stream_index = av_find_best_stream(fmt, AVMEDIA_TYPE_VIDEO, -1, -1, nullptr, 0);
    if (stream_index < 0) {
        std::cerr << "NativeMovieDecoder: no video stream in " << path << std::endl;
        avformat_close_input(&fmt);
        return false;
    }

    AVStream* stream = fmt->streams[stream_index];
    const AVCodec* codec = avcodec_find_decoder(stream->codecpar->codec_id);
    if (!codec) {
        std::cerr << "NativeMovieDecoder: unsupported codec in " << path << std::endl;
        avformat_close_input(&fmt);
        return false;
    }

    // Demuxer-only setup first so the packet index does not disturb a HW codec.
    _fmt = fmt;
    _video_stream = stream_index;
    _path = path;
    _width = stream->codecpar->width;
    _height = stream->codecpar->height;

    const AVPixelFormat par_fmt = static_cast<AVPixelFormat>(stream->codecpar->format);
    _has_alpha = pix_fmt_has_alpha(par_fmt);
    _pix_fmt_name = av_get_pix_fmt_name(par_fmt) ? av_get_pix_fmt_name(par_fmt) : "unknown";
    _bits_per_raw_sample = stream->codecpar->bits_per_raw_sample;
    if (_bits_per_raw_sample <= 0) {
        const AVPixFmtDescriptor* desc = av_pix_fmt_desc_get(par_fmt);
        _bits_per_raw_sample = desc ? desc->comp[0].depth : 8;
    }
    if (_bits_per_raw_sample <= 0) {
        _bits_per_raw_sample = 8;
    }

    AVRational rate = stream->avg_frame_rate.num > 0 ? stream->avg_frame_rate : stream->r_frame_rate;
    if (rate.num > 0 && rate.den > 0) {
        _fps = av_q2d(rate);
    } else {
        _fps = 24.0;
    }
    if (!std::isfinite(_fps) || _fps <= 0.0) {
        _fps = 24.0;
    }

    _frame_count = static_cast<int>(stream->nb_frames);
    if (_frame_count <= 0) {
        if (fmt->duration > 0 && _fps > 0.0) {
            const double duration_sec = static_cast<double>(fmt->duration) / static_cast<double>(AV_TIME_BASE);
            _frame_count = std::max(1, static_cast<int>(std::lround(duration_sec * _fps)));
        } else if (stream->duration > 0 && stream->time_base.den > 0) {
            const double duration_sec = stream->duration * av_q2d(stream->time_base);
            _frame_count = std::max(1, static_cast<int>(std::lround(duration_sec * _fps)));
        } else {
            _frame_count = 1;
        }
    }

    _sar = 1.0;
    if (stream->sample_aspect_ratio.num > 0 && stream->sample_aspect_ratio.den > 0) {
        _sar = av_q2d(stream->sample_aspect_ratio);
    } else if (stream->codecpar->sample_aspect_ratio.num > 0
               && stream->codecpar->sample_aspect_ratio.den > 0) {
        _sar = av_q2d(stream->codecpar->sample_aspect_ratio);
    }
    if (!std::isfinite(_sar) || _sar <= 0.0) {
        _sar = 1.0;
    }

    _file_metadata.clear();
    merge_dict(_file_metadata, fmt->metadata);
    merge_dict(_file_metadata, stream->metadata);

    _timecode_start = dict_get(stream->metadata, "timecode");
    if (_timecode_start.empty()) {
        _timecode_start = dict_get(fmt->metadata, "timecode");
    }
    if (_timecode_start.empty()) {
        _timecode_start = "01:00:00:00";
    }
    _start_frame = timecode_to_frame(_timecode_start, _fps);

    if (!_build_frame_index()) {
        std::cerr << "NativeMovieDecoder: frame index build failed for " << path
                  << "; falling back to duration estimate (" << _frame_count << " frames)"
                  << std::endl;
        _frame_pts.clear();
        _keyframes.clear();
        KeyframeEntry entry;
        entry.seek_ts = 0;
        entry.presentation_index = 0;
        _keyframes.push_back(entry);
    }

    auto open_codec = [&](bool try_hw) -> AVCodecContext* {
        AVCodecContext* codec_ctx = avcodec_alloc_context3(codec);
        if (!codec_ctx) {
            return nullptr;
        }
        if (avcodec_parameters_to_context(codec_ctx, stream->codecpar) < 0) {
            avcodec_free_context(&codec_ctx);
            return nullptr;
        }
        codec_ctx->thread_count = 0; // auto
        if (try_hw) {
            if (!_try_open_hw(codec_ctx, codec)) {
                _release_hw();
                codec_ctx->hw_device_ctx = nullptr;
                codec_ctx->get_format = nullptr;
                codec_ctx->opaque = nullptr;
            }
        }
        if (avcodec_open2(codec_ctx, codec, nullptr) < 0) {
            if (codec_ctx->hw_device_ctx) {
                av_buffer_unref(&codec_ctx->hw_device_ctx);
            }
            avcodec_free_context(&codec_ctx);
            _release_hw();
            return nullptr;
        }
        return codec_ctx;
    };

    AVCodecContext* codec_ctx = open_codec(allow_hw);
    if (!codec_ctx && allow_hw) {
        _release_hw();
        codec_ctx = open_codec(false);
    }
    if (!codec_ctx) {
        std::cerr << "NativeMovieDecoder: failed to open codec for " << path << std::endl;
        _fmt = nullptr;
        avformat_close_input(&fmt);
        return false;
    }

    AVFrame* frame = av_frame_alloc();
    AVFrame* sw_frame = av_frame_alloc();
    AVPacket* packet = av_packet_alloc();
    if (!frame || !sw_frame || !packet) {
        av_frame_free(&frame);
        av_frame_free(&sw_frame);
        av_packet_free(&packet);
        if (codec_ctx->hw_device_ctx) {
            av_buffer_unref(&codec_ctx->hw_device_ctx);
        }
        avcodec_free_context(&codec_ctx);
        _release_hw();
        _fmt = nullptr;
        avformat_close_input(&fmt);
        return false;
    }

    _codec = codec_ctx;
    _frame = frame;
    _sw_frame = sw_frame;
    _packet = packet;
    if (codec_ctx->width > 0) {
        _width = codec_ctx->width;
    }
    if (codec_ctx->height > 0) {
        _height = codec_ctx->height;
    }
    if (codec_ctx->pix_fmt != AV_PIX_FMT_NONE) {
        _has_alpha = pix_fmt_has_alpha(codec_ctx->pix_fmt);
        if (const char* name = av_get_pix_fmt_name(codec_ctx->pix_fmt)) {
            _pix_fmt_name = name;
        }
    }

    std::cerr << "NativeMovieDecoder: opened " << path << " hw=" << _hw_type
              << " pix_fmt=" << _pix_fmt_name << " bits=" << _bits_per_raw_sample
              << " frames=" << _frame_count << std::endl;

    _last_decoded_internal = -1;
    _next_presentation_index = -1;

    if (allow_hw && _hw_type != "software") {
        return _validate_decode_or_fallback_sw(path);
    }
    return true;
}

void NativeMovieDecoder::_close_unlocked()
{
    if (_sws) {
        sws_freeContext(_sws);
        _sws = nullptr;
    }
    _sws_src_w = _sws_src_h = _sws_dst_w = _sws_dst_h = 0;
    _sws_src_fmt = _sws_dst_fmt = -1;
    _convert_scratch.clear();
    _frame_pts.clear();
    _keyframes.clear();

    if (_packet) {
        av_packet_free(&_packet);
        _packet = nullptr;
    }
    if (_sw_frame) {
        av_frame_free(&_sw_frame);
        _sw_frame = nullptr;
    }
    if (_frame) {
        av_frame_free(&_frame);
        _frame = nullptr;
    }
    if (_codec) {
        if (_codec->hw_device_ctx) {
            av_buffer_unref(&_codec->hw_device_ctx);
        }
        avcodec_free_context(&_codec);
        _codec = nullptr;
    }
    _release_hw();
    if (_fmt) {
        avformat_close_input(&_fmt);
        _fmt = nullptr;
    }
    _video_stream = -1;
    _last_decoded_internal = -1;
    _next_presentation_index = -1;
    _path.clear();
    _pix_fmt_name = "unknown";
    _bits_per_raw_sample = 8;
}

bool NativeMovieDecoder::_ensure_sws(
    int src_w, int src_h, int src_fmt, int dst_w, int dst_h, int dst_fmt)
{
    if (_sws
        && _sws_src_w == src_w
        && _sws_src_h == src_h
        && _sws_src_fmt == src_fmt
        && _sws_dst_w == dst_w
        && _sws_dst_h == dst_h
        && _sws_dst_fmt == dst_fmt) {
        return true;
    }
    if (_sws) {
        sws_freeContext(_sws);
        _sws = nullptr;
    }
    _sws = sws_getContext(
        src_w,
        src_h,
        static_cast<AVPixelFormat>(src_fmt),
        dst_w,
        dst_h,
        static_cast<AVPixelFormat>(dst_fmt),
        SWS_BILINEAR,
        nullptr,
        nullptr,
        nullptr);
    if (!_sws) {
        return false;
    }
    _sws_src_w = src_w;
    _sws_src_h = src_h;
    _sws_src_fmt = src_fmt;
    _sws_dst_w = dst_w;
    _sws_dst_h = dst_h;
    _sws_dst_fmt = dst_fmt;
    return true;
}

bool NativeMovieDecoder::_transfer_hw_frame(AVFrame* hw_frame, AVFrame* sw_frame)
{
    if (!hw_frame || !sw_frame) {
        return false;
    }
    av_frame_unref(sw_frame);
    if (av_hwframe_transfer_data(sw_frame, hw_frame, 0) < 0) {
        return false;
    }
    sw_frame->best_effort_timestamp = hw_frame->best_effort_timestamp;
    sw_frame->pts = hw_frame->pts;
    return true;
}

bool NativeMovieDecoder::_decode_next_frame(AVFrame* out_frame)
{
    while (true) {
        const int receive = avcodec_receive_frame(_codec, out_frame);
        if (receive == 0) {
            if (_hw_pix_fmt >= 0 && out_frame->format == _hw_pix_fmt) {
                if (!_transfer_hw_frame(out_frame, _sw_frame)) {
                    return false;
                }
                av_frame_unref(out_frame);
                if (av_frame_ref(out_frame, _sw_frame) < 0) {
                    return false;
                }
            }
            return true;
        }
        if (receive != AVERROR(EAGAIN)) {
            return false;
        }

        while (true) {
            av_packet_unref(_packet);
            const int read = av_read_frame(_fmt, _packet);
            if (read < 0) {
                avcodec_send_packet(_codec, nullptr);
                const int flushed = avcodec_receive_frame(_codec, out_frame);
                if (flushed != 0) {
                    return false;
                }
                if (_hw_pix_fmt >= 0 && out_frame->format == _hw_pix_fmt) {
                    if (!_transfer_hw_frame(out_frame, _sw_frame)) {
                        return false;
                    }
                    av_frame_unref(out_frame);
                    if (av_frame_ref(out_frame, _sw_frame) < 0) {
                        return false;
                    }
                }
                return true;
            }
            if (_packet->stream_index != _video_stream) {
                continue;
            }
            if (avcodec_send_packet(_codec, _packet) < 0) {
                return false;
            }
            break;
        }
    }
}

bool NativeMovieDecoder::_seek_to_internal(int internal_index)
{
    if (!_fmt || _video_stream < 0 || !_codec || _keyframes.empty()) {
        return false;
    }

    // Largest keyframe with presentation_index <= internal_index.
    const KeyframeEntry* chosen = &_keyframes.front();
    for (const auto& kf : _keyframes) {
        if (kf.presentation_index <= internal_index) {
            chosen = &kf;
        } else {
            break;
        }
    }

    if (av_seek_frame(_fmt, _video_stream, chosen->seek_ts, AVSEEK_FLAG_BACKWARD) < 0) {
        if (av_seek_frame(_fmt, -1, chosen->seek_ts, AVSEEK_FLAG_BACKWARD) < 0) {
            return false;
        }
    }
    avcodec_flush_buffers(_codec);
    _last_decoded_internal = -1;
    _next_presentation_index = chosen->presentation_index;
    return true;
}

int NativeMovieDecoder::_frame_index_from_pts(int64_t pts) const
{
    if (_frame_pts.empty() || pts == AV_NOPTS_VALUE) {
        return -1;
    }
    auto it = std::lower_bound(_frame_pts.begin(), _frame_pts.end(), pts);
    if (it == _frame_pts.end()) {
        return static_cast<int>(_frame_pts.size()) - 1;
    }
    if (it != _frame_pts.begin()) {
        const auto prev = it - 1;
        if (std::llabs(*it - pts) >= std::llabs(*prev - pts)) {
            it = prev;
        }
    }
    return static_cast<int>(it - _frame_pts.begin());
}

NativeDecoder::DecodeResult NativeMovieDecoder::_frame_to_half(AVFrame* frame, float resolution_scale)
{
    NativeDecoder::DecodeResult result;
    if (!frame || frame->width <= 0 || frame->height <= 0) {
        return result;
    }

    const float scale = std::clamp(resolution_scale, 0.01f, 1.0f);
    const int dst_w = std::max(1, static_cast<int>(std::lround(frame->width * scale)));
    const int dst_h = std::max(1, static_cast<int>(std::lround(frame->height * scale)));

    const AVPixelFormat src_fmt = static_cast<AVPixelFormat>(frame->format);
    int dst_fmt = static_cast<int>(AV_PIX_FMT_RGBA64);
    int bytes_per_pixel = 8; // RGBA64
#ifdef AV_PIX_FMT_RGBAF32
    if (pix_fmt_is_float(src_fmt)) {
        dst_fmt = static_cast<int>(AV_PIX_FMT_RGBAF32);
        bytes_per_pixel = 16;
    }
#endif

    if (!_ensure_sws(frame->width, frame->height, frame->format, dst_w, dst_h, dst_fmt)) {
        // Fall back to 8-bit if high-bit sws fails (exotic formats).
        dst_fmt = static_cast<int>(AV_PIX_FMT_RGBA);
        bytes_per_pixel = 4;
        if (!_ensure_sws(frame->width, frame->height, frame->format, dst_w, dst_h, dst_fmt)) {
            std::cerr << "NativeMovieDecoder: sws_getContext failed" << std::endl;
            return result;
        }
    }

    const size_t scratch_bytes = static_cast<size_t>(dst_w) * static_cast<size_t>(dst_h)
        * static_cast<size_t>(bytes_per_pixel);
    if (_convert_scratch.size() < scratch_bytes) {
        _convert_scratch.resize(scratch_bytes);
    }

    uint8_t* dst_slices[4] = { _convert_scratch.data(), nullptr, nullptr, nullptr };
    int dst_strides[4] = { dst_w * bytes_per_pixel, 0, 0, 0 };

    if (sws_scale(
            _sws,
            frame->data,
            frame->linesize,
            0,
            frame->height,
            dst_slices,
            dst_strides)
        <= 0) {
        std::cerr << "NativeMovieDecoder: sws_scale failed" << std::endl;
        return result;
    }

    result.width = dst_w;
    result.height = dst_h;
    result.channels = 4;
    const size_t n = static_cast<size_t>(dst_w) * static_cast<size_t>(dst_h) * 4u;
    result.pixel_data.resize(n);

    if (dst_fmt == static_cast<int>(AV_PIX_FMT_RGBA64)) {
        convert_rgba64_to_half(
            reinterpret_cast<const uint16_t*>(_convert_scratch.data()),
            result.pixel_data.data(),
            n);
#ifdef AV_PIX_FMT_RGBAF32
    } else if (dst_fmt == static_cast<int>(AV_PIX_FMT_RGBAF32)) {
        convert_rgbaf32_to_half(
            reinterpret_cast<const float*>(_convert_scratch.data()),
            result.pixel_data.data(),
            n);
#endif
    } else {
        convert_rgba8_to_half(_convert_scratch.data(), result.pixel_data.data(), n);
    }

    if (!_has_alpha) {
        constexpr uint16_t kOne = 0x3C00;
        for (size_t p = 0; p < static_cast<size_t>(dst_w) * static_cast<size_t>(dst_h); ++p) {
            result.pixel_data[p * 4 + 3] = kOne;
        }
    }
    result.success = true;
    return result;
}

NativeDecoder::DecodeResult NativeMovieDecoder::decode_frame(int absolute_frame_index, float resolution_scale)
{
    std::lock_guard<std::mutex> lock(_mutex);
    NativeDecoder::DecodeResult result;
    if (!_fmt || !_codec || !_frame) {
        return result;
    }

    const int internal = absolute_frame_index - _start_frame;
    if (internal < 0 || internal >= _frame_count) {
        return result;
    }

    const bool sequential = (_last_decoded_internal >= 0 && internal == _last_decoded_internal + 1);
    if (!sequential) {
        if (!_seek_to_internal(internal)) {
            std::cerr << "NativeMovieDecoder: seek failed for frame " << absolute_frame_index << std::endl;
            return result;
        }
    } else if (_next_presentation_index < 0) {
        _next_presentation_index = internal;
    }

    AVFrame* decoded = _frame;
    AVFrame* last_clone = nullptr;
    auto free_last = [&]() {
        if (last_clone) {
            av_frame_free(&last_clone);
            last_clone = nullptr;
        }
    };

    while (true) {
        if (!_decode_next_frame(decoded)) {
            // Past end: only return last frame when scrubbing to the final index.
            if (last_clone && internal == _frame_count - 1) {
                result = _frame_to_half(last_clone, resolution_scale);
                if (result.success) {
                    _last_decoded_internal = internal;
                    _next_presentation_index = internal + 1;
                }
            }
            free_last();
            return result;
        }

        int presentation = _next_presentation_index;
        if (presentation < 0) {
            // Recover index from PTS when seek state is unknown.
            int64_t pts = decoded->best_effort_timestamp;
            if (pts == AV_NOPTS_VALUE) {
                pts = decoded->pts;
            }
            presentation = _frame_index_from_pts(pts);
            if (presentation < 0) {
                presentation = sequential ? internal : 0;
            }
            _next_presentation_index = presentation;
        }

        if (presentation == internal) {
            result = _frame_to_half(decoded, resolution_scale);
            if (result.success) {
                _last_decoded_internal = internal;
                _next_presentation_index = internal + 1;
            }
            free_last();
            return result;
        }

        if (presentation > internal) {
            // Exact frame missed (corrupt index / missing packets) — do not return overshoot.
            free_last();
            return result;
        }

        free_last();
        last_clone = av_frame_clone(decoded);
        _next_presentation_index = presentation + 1;
    }
}
