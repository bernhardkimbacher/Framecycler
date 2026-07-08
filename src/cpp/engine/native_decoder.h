#pragma once

#include <string>
#include <vector>
#include <cstdint>

namespace NativeDecoder {

struct DecodeResult {
    bool success = false;
    int width = 0;
    int height = 0;
    int channels = 0;
    std::vector<uint16_t> pixel_data;
};

DecodeResult decode_frame(const std::string& file_path, float resolution_scale);

} // namespace NativeDecoder
