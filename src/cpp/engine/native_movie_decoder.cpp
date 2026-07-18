#include "native_movie_decoder.h"

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <iostream>
#include <vector>

extern "C" {
#include <libavcodec/avcodec.h>
#include <libavformat/avformat.h>
#include <libavutil/avutil.h>
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

bool pix_fmt_has_alpha(AVPixelFormat fmt)
{
    const AVPixFmtDescriptor* desc = av_pix_fmt_desc_get(fmt);
    return desc && (desc->flags & AV_PIX_FMT_FLAG_ALPHA);
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

} // namespace

NativeMovieDecoder::~NativeMovieDecoder()
{
    close();
}

bool NativeMovieDecoder::open(const std::string& path)
{
    std::lock_guard<std::mutex> lock(_mutex);
    return _open_unlocked(path);
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

bool NativeMovieDecoder::_open_unlocked(const std::string& path)
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

    AVCodecContext* codec_ctx = avcodec_alloc_context3(codec);
    if (!codec_ctx) {
        avformat_close_input(&fmt);
        return false;
    }
    if (avcodec_parameters_to_context(codec_ctx, stream->codecpar) < 0) {
        avcodec_free_context(&codec_ctx);
        avformat_close_input(&fmt);
        return false;
    }
    codec_ctx->thread_count = 0; // auto
    if (avcodec_open2(codec_ctx, codec, nullptr) < 0) {
        std::cerr << "NativeMovieDecoder: failed to open codec for " << path << std::endl;
        avcodec_free_context(&codec_ctx);
        avformat_close_input(&fmt);
        return false;
    }

    AVFrame* frame = av_frame_alloc();
    AVPacket* packet = av_packet_alloc();
    if (!frame || !packet) {
        av_frame_free(&frame);
        av_packet_free(&packet);
        avcodec_free_context(&codec_ctx);
        avformat_close_input(&fmt);
        return false;
    }

    _fmt = fmt;
    _codec = codec_ctx;
    _frame = frame;
    _packet = packet;
    _video_stream = stream_index;
    _path = path;
    _width = codec_ctx->width;
    _height = codec_ctx->height;
    _has_alpha = pix_fmt_has_alpha(codec_ctx->pix_fmt);

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
    } else if (codec_ctx->sample_aspect_ratio.num > 0 && codec_ctx->sample_aspect_ratio.den > 0) {
        _sar = av_q2d(codec_ctx->sample_aspect_ratio);
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
    _last_decoded_internal = -1;
    return true;
}

void NativeMovieDecoder::_close_unlocked()
{
    if (_sws) {
        sws_freeContext(_sws);
        _sws = nullptr;
    }
    _sws_src_w = _sws_src_h = _sws_dst_w = _sws_dst_h = 0;
    _sws_src_fmt = -1;

    if (_packet) {
        av_packet_free(&_packet);
        _packet = nullptr;
    }
    if (_frame) {
        av_frame_free(&_frame);
        _frame = nullptr;
    }
    if (_codec) {
        avcodec_free_context(&_codec);
        _codec = nullptr;
    }
    if (_fmt) {
        avformat_close_input(&_fmt);
        _fmt = nullptr;
    }
    _video_stream = -1;
    _last_decoded_internal = -1;
    _path.clear();
}

bool NativeMovieDecoder::_ensure_sws(int src_w, int src_h, int src_fmt, int dst_w, int dst_h)
{
    if (_sws
        && _sws_src_w == src_w
        && _sws_src_h == src_h
        && _sws_src_fmt == src_fmt
        && _sws_dst_w == dst_w
        && _sws_dst_h == dst_h) {
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
        AV_PIX_FMT_RGBA,
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
    return true;
}

bool NativeMovieDecoder::_decode_next_frame(AVFrame* out_frame)
{
    while (true) {
        const int receive = avcodec_receive_frame(_codec, out_frame);
        if (receive == 0) {
            return true;
        }
        if (receive != AVERROR(EAGAIN)) {
            return false;
        }

        while (true) {
            av_packet_unref(_packet);
            const int read = av_read_frame(_fmt, _packet);
            if (read < 0) {
                // Flush decoder on EOF.
                avcodec_send_packet(_codec, nullptr);
                const int flushed = avcodec_receive_frame(_codec, out_frame);
                return flushed == 0;
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
    if (!_fmt || _video_stream < 0 || !_codec) {
        return false;
    }
    AVStream* stream = _fmt->streams[_video_stream];
    const double target_sec = static_cast<double>(internal_index) / _fps;
    const int64_t target_ts = static_cast<int64_t>(std::llround(target_sec / av_q2d(stream->time_base)));

    if (av_seek_frame(_fmt, _video_stream, target_ts, AVSEEK_FLAG_BACKWARD) < 0) {
        // Fall back to container-level seek in AV_TIME_BASE.
        const int64_t ts = static_cast<int64_t>(std::llround(target_sec * AV_TIME_BASE));
        if (av_seek_frame(_fmt, -1, ts, AVSEEK_FLAG_BACKWARD) < 0) {
            return false;
        }
    }
    avcodec_flush_buffers(_codec);
    _last_decoded_internal = -1;
    return true;
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

    if (!_ensure_sws(frame->width, frame->height, frame->format, dst_w, dst_h)) {
        std::cerr << "NativeMovieDecoder: sws_getContext failed" << std::endl;
        return result;
    }

    std::vector<uint8_t> rgba(static_cast<size_t>(dst_w) * dst_h * 4);
    uint8_t* dst_slices[4] = { rgba.data(), nullptr, nullptr, nullptr };
    int dst_strides[4] = { dst_w * 4, 0, 0, 0 };

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
    result.pixel_data.resize(static_cast<size_t>(dst_w) * dst_h * 4);
    for (size_t i = 0; i < rgba.size(); ++i) {
        result.pixel_data[i] = u8_to_half(rgba[i]);
    }
    // Force opaque alpha when source has no alpha plane.
    if (!_has_alpha) {
        constexpr uint16_t kOne = 0x3C00;
        for (size_t p = 0; p < static_cast<size_t>(dst_w) * dst_h; ++p) {
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

    // Sequential fast path.
    const bool sequential = (_last_decoded_internal >= 0 && internal == _last_decoded_internal + 1);
    if (!sequential) {
        if (!_seek_to_internal(internal)) {
            std::cerr << "NativeMovieDecoder: seek failed for frame " << absolute_frame_index << std::endl;
            return result;
        }
    }

    AVFrame* decoded = _frame;
    int approx_internal = -1;
    AVFrame* last_ok = nullptr;
    // Keep a shallow clone of the last good frame for EOF fallback.
    AVFrame* last_clone = nullptr;

    auto free_last = [&]() {
        if (last_clone) {
            av_frame_free(&last_clone);
            last_clone = nullptr;
        }
    };

    while (true) {
        if (!_decode_next_frame(decoded)) {
            if (last_clone) {
                result = _frame_to_half(last_clone, resolution_scale);
                _last_decoded_internal = approx_internal >= 0 ? approx_internal : internal;
                free_last();
                return result;
            }
            free_last();
            return result;
        }

        AVStream* stream = _fmt->streams[_video_stream];
        if (decoded->best_effort_timestamp != AV_NOPTS_VALUE) {
            const double pts_sec = decoded->best_effort_timestamp * av_q2d(stream->time_base);
            approx_internal = static_cast<int>(std::lround(pts_sec * _fps));
        } else if (decoded->pts != AV_NOPTS_VALUE) {
            const double pts_sec = decoded->pts * av_q2d(stream->time_base);
            approx_internal = static_cast<int>(std::lround(pts_sec * _fps));
        } else if (sequential) {
            approx_internal = internal;
        } else {
            approx_internal = (_last_decoded_internal < 0) ? 0 : (_last_decoded_internal + 1);
        }

        if (approx_internal == internal || sequential) {
            result = _frame_to_half(decoded, resolution_scale);
            if (result.success) {
                _last_decoded_internal = internal;
            }
            free_last();
            return result;
        }
        if (approx_internal > internal) {
            result = _frame_to_half(decoded, resolution_scale);
            if (result.success) {
                _last_decoded_internal = approx_internal;
            }
            free_last();
            return result;
        }

        free_last();
        last_clone = av_frame_clone(decoded);
        last_ok = last_clone;
        (void)last_ok;
    }
}
