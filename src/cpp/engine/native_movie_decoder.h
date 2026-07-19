#pragma once

#include "native_decoder.h"

#include <cstdint>
#include <mutex>
#include <string>
#include <unordered_map>
#include <vector>

struct AVBufferRef;
struct AVCodec;
struct AVCodecContext;
struct AVFormatContext;
struct AVFrame;
struct AVPacket;
struct SwsContext;

class NativeMovieDecoder {
public:
    NativeMovieDecoder() = default;
    ~NativeMovieDecoder();

    NativeMovieDecoder(const NativeMovieDecoder&) = delete;
    NativeMovieDecoder& operator=(const NativeMovieDecoder&) = delete;

    bool open(const std::string& path);
    void close();
    bool is_open() const;

    int width() const;
    int height() const;
    double fps() const;
    int frame_count() const;
    bool has_alpha() const;
    double sample_aspect_ratio() const;
    std::string timecode_start() const;
    int start_frame() const;
    int end_frame() const;
    std::string path() const;
    std::unordered_map<std::string, std::string> file_metadata() const;

    /// "videotoolbox" | "d3d11va" | "vaapi" | "software"
    std::string hw_type() const;
    int bits_per_raw_sample() const;
    std::string pix_fmt_name() const;

    NativeDecoder::DecodeResult decode_frame(int absolute_frame_index, float resolution_scale);

    /// Used by FFmpeg get_format callback (must be callable from C).
    int _get_hw_format(const int* pix_fmts) const;

private:
    struct KeyframeEntry {
        int64_t seek_ts = 0;          // stream time_base
        int presentation_index = 0;   // first presentation index at/after this keyframe
    };

    bool _open_unlocked(const std::string& path, bool allow_hw);
    void _close_unlocked();
    bool _build_frame_index();
    bool _try_open_hw(AVCodecContext* codec_ctx, const struct AVCodec* codec);
    void _release_hw();
    bool _validate_decode_or_fallback_sw(const std::string& path);
    bool _ensure_sws(int src_w, int src_h, int src_fmt, int dst_w, int dst_h, int dst_fmt);
    bool _decode_next_frame(AVFrame* out_frame);
    bool _transfer_hw_frame(AVFrame* hw_frame, AVFrame* sw_frame);
    bool _seek_to_internal(int internal_index);
    int _frame_index_from_pts(int64_t pts) const;
    NativeDecoder::DecodeResult _frame_to_half(AVFrame* frame, float resolution_scale);

    mutable std::mutex _mutex;
    std::string _path;
    AVFormatContext* _fmt = nullptr;
    AVCodecContext* _codec = nullptr;
    SwsContext* _sws = nullptr;
    AVFrame* _frame = nullptr;
    AVFrame* _sw_frame = nullptr; // HW download target
    AVPacket* _packet = nullptr;
    AVBufferRef* _hw_device_ctx = nullptr;

    int _video_stream = -1;
    int _width = 0;
    int _height = 0;
    double _fps = 24.0;
    int _frame_count = 1;
    bool _has_alpha = false;
    double _sar = 1.0;
    std::string _timecode_start = "01:00:00:00";
    int _start_frame = 0;
    std::unordered_map<std::string, std::string> _file_metadata;

    int _bits_per_raw_sample = 8;
    std::string _pix_fmt_name = "unknown";
    std::string _hw_type = "software";
    int _hw_pix_fmt = -1;

    // Presentation-order PTS list and keyframe seek table (built on open).
    std::vector<int64_t> _frame_pts;
    std::vector<KeyframeEntry> _keyframes;

    int _last_decoded_internal = -1;
    int _next_presentation_index = -1; // expected index of next decoded frame after seek

    int _sws_src_w = 0;
    int _sws_src_h = 0;
    int _sws_src_fmt = -1;
    int _sws_dst_w = 0;
    int _sws_dst_h = 0;
    int _sws_dst_fmt = -1;

    // Reusable convert scratch (RGBA64 bytes or RGBAF32 floats as uint8 view).
    std::vector<uint8_t> _convert_scratch;
};
