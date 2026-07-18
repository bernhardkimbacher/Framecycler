#pragma once

#include "native_decoder.h"

#include <cstdint>
#include <mutex>
#include <string>
#include <unordered_map>

struct AVFormatContext;
struct AVCodecContext;
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

    NativeDecoder::DecodeResult decode_frame(int absolute_frame_index, float resolution_scale);

private:
    bool _open_unlocked(const std::string& path);
    void _close_unlocked();
    bool _ensure_sws(int src_w, int src_h, int src_fmt, int dst_w, int dst_h);
    bool _decode_next_frame(AVFrame* out_frame);
    bool _seek_to_internal(int internal_index);
    NativeDecoder::DecodeResult _frame_to_half(AVFrame* frame, float resolution_scale);

    mutable std::mutex _mutex;
    std::string _path;
    AVFormatContext* _fmt = nullptr;
    AVCodecContext* _codec = nullptr;
    SwsContext* _sws = nullptr;
    AVFrame* _frame = nullptr;
    AVPacket* _packet = nullptr;

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

    int _last_decoded_internal = -1;
    int _sws_src_w = 0;
    int _sws_src_h = 0;
    int _sws_src_fmt = -1;
    int _sws_dst_w = 0;
    int _sws_dst_h = 0;
};
