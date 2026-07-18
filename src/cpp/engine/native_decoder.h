#pragma once

#include <cstdint>
#include <functional>
#include <string>
#include <vector>

namespace NativeDecoder {

struct DecodeResult {
    bool success = false;
    int width = 0;
    int height = 0;
    int channels = 0;
    std::vector<uint16_t> pixel_data;
};

/// Decode a still (EXR/DPX/etc via OIIO) into an owned buffer.
DecodeResult decode_frame(const std::string& file_path,
                          float resolution_scale,
                          const std::string& layer = "",
                          const std::string& fallback_mode = "Flat Gray",
                          int placeholder_width = 0,
                          int placeholder_height = 0);

/// Decode into a caller-owned buffer. ``dest`` must hold at least
/// ``dest_capacity`` half-float elements. On success, writes ``out_width``,
/// ``out_height``, ``out_channels`` and returns true. On open failure, may
/// still write a placeholder into ``dest`` (and return false with
/// ``wrote_placeholder=true``) when capacity is sufficient.
bool decode_frame_into(const std::string& file_path,
                       float resolution_scale,
                       const std::string& layer,
                       uint16_t* dest,
                       size_t dest_capacity,
                       int& out_width,
                       int& out_height,
                       int& out_channels,
                       bool& wrote_placeholder,
                       const std::string& fallback_mode = "Flat Gray",
                       int placeholder_width = 0,
                       int placeholder_height = 0);

/// Single-open decode: after resolving output size, calls ``allocate(w, h, ch)``
/// to obtain a writable buffer, then reads pixels into it. ``allocate`` must
/// return a pointer with capacity for ``w * h * ch`` half-floats (or nullptr
/// to abort). Sets ``wrote_placeholder`` when a fallback image was written.
bool decode_with_allocator(const std::string& file_path,
                           float resolution_scale,
                           const std::string& layer,
                           const std::function<uint16_t*(int, int, int)>& allocate,
                           int& out_width,
                           int& out_height,
                           int& out_channels,
                           bool& wrote_placeholder,
                           const std::string& fallback_mode = "Flat Gray",
                           int placeholder_width = 0,
                           int placeholder_height = 0);

/// Probe output dimensions/channels without decoding pixels. Returns false
/// if the file cannot be opened (does not write a placeholder).
bool probe_output_size(const std::string& file_path,
                       float resolution_scale,
                       const std::string& layer,
                       int& out_width,
                       int& out_height,
                       int& out_channels);

/// Configure the global OIIO / OpenEXR thread pools used during decode.
void set_decode_threads(int n);

/// Exposed for tests: resolve layer/channel indices from channel names.
std::vector<int> get_layer_channel_indices(const std::vector<std::string>& channel_names,
                                           const std::string& layer);

} // namespace NativeDecoder
