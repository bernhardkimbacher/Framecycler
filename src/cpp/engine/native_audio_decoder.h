#pragma once

#include <cstdint>
#include <mutex>
#include <string>
#include <vector>

struct AVCodecContext;
struct AVFormatContext;
struct AVFrame;
struct AVPacket;
struct SwrContext;

/// FFmpeg audio demux/decode → interleaved stereo float @ 48 kHz.
class NativeAudioDecoder {
public:
    static constexpr int kOutputSampleRate = 48000;
    static constexpr int kOutputChannels = 2;

    NativeAudioDecoder() = default;
    ~NativeAudioDecoder();

    NativeAudioDecoder(const NativeAudioDecoder&) = delete;
    NativeAudioDecoder& operator=(const NativeAudioDecoder&) = delete;

    bool open(const std::string& path);
    void close();
    bool is_open() const;
    bool has_audio() const { return _has_audio; }

    std::string path() const;
    double duration_seconds() const { return _duration_sec; }
    int sample_rate() const { return kOutputSampleRate; }
    int channels() const { return kOutputChannels; }

    /// Seek output timeline to ``time_sec`` (clamped). Returns false on failure.
    bool seek(double time_sec);

    /// Decode up to ``max_frames`` stereo frames into ``out`` (interleaved LRLR…).
    /// Returns frames written (0 on EOF/error).
    int decode_frames(float* out, int max_frames);

    /// Abs-peak envelope at ``peaks_per_second`` bins (mono). Empty if no audio.
    std::vector<float> build_peaks(int peaks_per_second = 300);

private:
    bool _open_unlocked(const std::string& path);
    void _close_unlocked();
    bool _ensure_swr(AVFrame* frame);
    int _resample_frame(AVFrame* frame, float* out, int max_frames);

    mutable std::mutex _mutex;
    std::string _path;
    AVFormatContext* _fmt = nullptr;
    AVCodecContext* _codec = nullptr;
    SwrContext* _swr = nullptr;
    AVFrame* _frame = nullptr;
    AVPacket* _packet = nullptr;

    int _audio_stream = -1;
    bool _has_audio = false;
    double _duration_sec = 0.0;
    double _time_base = 0.0;

    int _swr_in_rate = 0;
    int _swr_in_fmt = -1;
    int _swr_in_layout_channels = 0;

    std::vector<float> _pending; // leftover resampled samples (interleaved)
};
