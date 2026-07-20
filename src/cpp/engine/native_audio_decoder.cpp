#include "native_audio_decoder.h"

extern "C" {
#include <libavcodec/avcodec.h>
#include <libavformat/avformat.h>
#include <libavutil/channel_layout.h>
#include <libavutil/opt.h>
#include <libavutil/samplefmt.h>
#include <libswresample/swresample.h>
}

#include <algorithm>
#include <cmath>
#include <cstring>

NativeAudioDecoder::~NativeAudioDecoder()
{
    close();
}

bool NativeAudioDecoder::open(const std::string& path)
{
    std::lock_guard<std::mutex> lock(_mutex);
    _close_unlocked();
    return _open_unlocked(path);
}

void NativeAudioDecoder::close()
{
    std::lock_guard<std::mutex> lock(_mutex);
    _close_unlocked();
}

bool NativeAudioDecoder::is_open() const
{
    std::lock_guard<std::mutex> lock(_mutex);
    return _fmt != nullptr && _has_audio;
}

std::string NativeAudioDecoder::path() const
{
    std::lock_guard<std::mutex> lock(_mutex);
    return _path;
}

bool NativeAudioDecoder::_open_unlocked(const std::string& path)
{
    _path = path;
    if (avformat_open_input(&_fmt, path.c_str(), nullptr, nullptr) < 0) {
        _close_unlocked();
        return false;
    }
    if (avformat_find_stream_info(_fmt, nullptr) < 0) {
        _close_unlocked();
        return false;
    }

    _audio_stream = av_find_best_stream(_fmt, AVMEDIA_TYPE_AUDIO, -1, -1, nullptr, 0);
    if (_audio_stream < 0) {
        _has_audio = false;
        _duration_sec = 0.0;
        return true; // open succeeded; simply no audio
    }

    AVStream* stream = _fmt->streams[_audio_stream];
    const AVCodec* codec = avcodec_find_decoder(stream->codecpar->codec_id);
    if (!codec) {
        _close_unlocked();
        return false;
    }
    _codec = avcodec_alloc_context3(codec);
    if (!_codec || avcodec_parameters_to_context(_codec, stream->codecpar) < 0) {
        _close_unlocked();
        return false;
    }
    if (avcodec_open2(_codec, codec, nullptr) < 0) {
        _close_unlocked();
        return false;
    }

    _frame = av_frame_alloc();
    _packet = av_packet_alloc();
    if (!_frame || !_packet) {
        _close_unlocked();
        return false;
    }

    _time_base = av_q2d(stream->time_base);
    if (_fmt->duration > 0) {
        _duration_sec = static_cast<double>(_fmt->duration) / AV_TIME_BASE;
    } else if (stream->duration > 0) {
        _duration_sec = stream->duration * _time_base;
    } else {
        _duration_sec = 0.0;
    }

    _has_audio = true;
    return true;
}

void NativeAudioDecoder::_close_unlocked()
{
    _pending.clear();
    if (_swr) {
        swr_free(&_swr);
        _swr = nullptr;
    }
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
    _audio_stream = -1;
    _has_audio = false;
    _duration_sec = 0.0;
    _time_base = 0.0;
    _swr_in_rate = 0;
    _swr_in_fmt = -1;
    _swr_in_layout_channels = 0;
    _path.clear();
}

bool NativeAudioDecoder::_ensure_swr(AVFrame* frame)
{
    const int in_rate = frame->sample_rate > 0 ? frame->sample_rate : _codec->sample_rate;
    const int in_fmt = frame->format;
#if LIBAVUTIL_VERSION_MAJOR >= 57
    const int in_ch = frame->ch_layout.nb_channels;
#else
    const int in_ch = frame->channels;
#endif
    if (_swr && _swr_in_rate == in_rate && _swr_in_fmt == in_fmt && _swr_in_layout_channels == in_ch) {
        return true;
    }
    if (_swr) {
        swr_free(&_swr);
        _swr = nullptr;
    }

    AVChannelLayout out_layout;
    av_channel_layout_default(&out_layout, kOutputChannels);

#if LIBAVUTIL_VERSION_MAJOR >= 57
    AVChannelLayout in_layout = frame->ch_layout;
    if (in_layout.nb_channels <= 0) {
        av_channel_layout_default(&in_layout, std::max(1, in_ch));
    }
    int ret = swr_alloc_set_opts2(
        &_swr,
        &out_layout,
        AV_SAMPLE_FMT_FLT,
        kOutputSampleRate,
        &in_layout,
        static_cast<AVSampleFormat>(in_fmt),
        in_rate,
        0,
        nullptr);
#else
    int64_t in_layout = frame->channel_layout;
    if (!in_layout) {
        in_layout = av_get_default_channel_layout(std::max(1, in_ch));
    }
    _swr = swr_alloc_set_opts(
        nullptr,
        AV_CH_LAYOUT_STEREO,
        AV_SAMPLE_FMT_FLT,
        kOutputSampleRate,
        in_layout,
        static_cast<AVSampleFormat>(in_fmt),
        in_rate,
        0,
        nullptr);
    int ret = _swr ? 0 : -1;
#endif
    if (ret < 0 || !_swr || swr_init(_swr) < 0) {
        if (_swr) {
            swr_free(&_swr);
        }
        return false;
    }
    _swr_in_rate = in_rate;
    _swr_in_fmt = in_fmt;
    _swr_in_layout_channels = in_ch;
    return true;
}

int NativeAudioDecoder::_resample_frame(AVFrame* frame, float* out, int max_frames)
{
    if (!_ensure_swr(frame) || max_frames <= 0) {
        return 0;
    }
    uint8_t* out_planes[1] = {reinterpret_cast<uint8_t*>(out)};
    const int converted = swr_convert(
        _swr,
        out_planes,
        max_frames,
        const_cast<const uint8_t**>(frame->extended_data),
        frame->nb_samples);
    return converted > 0 ? converted : 0;
}

bool NativeAudioDecoder::seek(double time_sec)
{
    std::lock_guard<std::mutex> lock(_mutex);
    if (!_fmt || !_has_audio || _audio_stream < 0) {
        return false;
    }
    time_sec = std::max(0.0, time_sec);
    if (_duration_sec > 0.0) {
        time_sec = std::min(time_sec, _duration_sec);
    }
    const int64_t ts = static_cast<int64_t>(time_sec / std::max(_time_base, 1e-12));
    if (av_seek_frame(_fmt, _audio_stream, ts, AVSEEK_FLAG_BACKWARD) < 0) {
        const int64_t ts_global = static_cast<int64_t>(time_sec * AV_TIME_BASE);
        if (av_seek_frame(_fmt, -1, ts_global, AVSEEK_FLAG_BACKWARD) < 0) {
            return false;
        }
    }
    avcodec_flush_buffers(_codec);
    _pending.clear();
    return true;
}

int NativeAudioDecoder::decode_frames(float* out, int max_frames)
{
    std::lock_guard<std::mutex> lock(_mutex);
    if (!_fmt || !_has_audio || !out || max_frames <= 0) {
        return 0;
    }

    int written = 0;
    if (!_pending.empty()) {
        const int pending_frames = static_cast<int>(_pending.size() / kOutputChannels);
        const int take = std::min(pending_frames, max_frames);
        std::memcpy(out, _pending.data(), static_cast<size_t>(take * kOutputChannels) * sizeof(float));
        written += take;
        _pending.erase(_pending.begin(), _pending.begin() + take * kOutputChannels);
        if (written >= max_frames) {
            return written;
        }
    }

    while (written < max_frames) {
        int ret = av_read_frame(_fmt, _packet);
        if (ret < 0) {
            break;
        }
        if (_packet->stream_index != _audio_stream) {
            av_packet_unref(_packet);
            continue;
        }
        ret = avcodec_send_packet(_codec, _packet);
        av_packet_unref(_packet);
        if (ret < 0) {
            continue;
        }
        while (ret >= 0 && written < max_frames) {
            ret = avcodec_receive_frame(_codec, _frame);
            if (ret == AVERROR(EAGAIN) || ret == AVERROR_EOF) {
                break;
            }
            if (ret < 0) {
                break;
            }
            const int remain = max_frames - written;
            // Temp buffer large enough for one converted frame (with headroom).
            const int need = std::max(remain, _frame->nb_samples * 2 + 64);
            std::vector<float> tmp(static_cast<size_t>(need * kOutputChannels));
            const int got = _resample_frame(_frame, tmp.data(), need);
            if (got <= 0) {
                continue;
            }
            const int use = std::min(got, remain);
            std::memcpy(
                out + written * kOutputChannels,
                tmp.data(),
                static_cast<size_t>(use * kOutputChannels) * sizeof(float));
            written += use;
            if (use < got) {
                _pending.insert(
                    _pending.end(),
                    tmp.begin() + use * kOutputChannels,
                    tmp.begin() + got * kOutputChannels);
            }
        }
    }
    return written;
}

std::vector<float> NativeAudioDecoder::build_peaks(int peaks_per_second)
{
    std::vector<float> peaks;
    if (peaks_per_second <= 0) {
        return peaks;
    }

    std::string path_copy;
    {
        std::lock_guard<std::mutex> lock(_mutex);
        path_copy = _path;
        if (!_has_audio || path_copy.empty()) {
            return peaks;
        }
    }
    // Private open so we don't disturb playback position.
    NativeAudioDecoder scan;
    if (!scan.open(path_copy) || !scan.has_audio()) {
        return peaks;
    }

    const double duration = std::max(0.05, scan.duration_seconds());
    const int bin_count = std::max(1, static_cast<int>(std::ceil(duration * peaks_per_second)));
    peaks.assign(static_cast<size_t>(bin_count), 0.0f);

    scan.seek(0.0);
    constexpr int kChunk = 4096;
    std::vector<float> chunk(static_cast<size_t>(kChunk * kOutputChannels));
    int64_t sample_index = 0;
    for (;;) {
        const int got = scan.decode_frames(chunk.data(), kChunk);
        if (got <= 0) {
            break;
        }
        for (int i = 0; i < got; ++i) {
            const float l = chunk[static_cast<size_t>(i * 2)];
            const float r = chunk[static_cast<size_t>(i * 2 + 1)];
            const float peak = std::max(std::fabs(l), std::fabs(r));
            const int bin = static_cast<int>(
                (static_cast<double>(sample_index) / kOutputSampleRate) * peaks_per_second);
            if (bin >= 0 && bin < bin_count) {
                peaks[static_cast<size_t>(bin)] = std::max(peaks[static_cast<size_t>(bin)], peak);
            }
            ++sample_index;
        }
    }
    return peaks;
}
