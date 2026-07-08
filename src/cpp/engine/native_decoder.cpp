#include "native_decoder.h"
#include <OpenImageIO/imageio.h>
#include <OpenImageIO/imagebuf.h>
#include <OpenImageIO/imagebufalgo.h>
#include <iostream>
#include <cmath>
#include <algorithm>

namespace NativeDecoder {

DecodeResult decode_frame(const std::string& file_path, float resolution_scale)
{
    DecodeResult result;
    result.success = false;

    OIIO::ImageSpec spec;
    auto in = OIIO::ImageInput::open(file_path);
    if (!in) {
        std::cerr << "NativeDecoder Error: Failed to open image: " << file_path 
                  << " (" << OIIO::geterror() << ")" << std::endl;
        
        // Fallback: Generate a standard grey placeholder frame (1920x1080, RGBA)
        result.width = 1920;
        result.height = 1080;
        result.channels = 4;
        size_t total_size = static_cast<size_t>(result.width) * result.height * result.channels;
        result.pixel_data.assign(total_size, 0);
        uint16_t grayVal = 0x3266; // 0.2f in half-float
        uint16_t alphaVal = 0x3C00; // 1.0f in half-float
        for (size_t i = 0; i < total_size; i += 4) {
            result.pixel_data[i + 0] = grayVal;
            result.pixel_data[i + 1] = grayVal;
            result.pixel_data[i + 2] = grayVal;
            result.pixel_data[i + 3] = alphaVal;
        }
        return result;
    }

    spec = in->spec();
    result.width = spec.width;
    result.height = spec.height;
    result.channels = spec.nchannels;
    in->close();

    OIIO::ImageBuf srcBuf(file_path);
    if (srcBuf.has_error()) {
        std::cerr << "NativeDecoder Error: Failed to read image spec buffer: " 
                  << file_path << " (" << srcBuf.geterror() << ")" << std::endl;
        return result;
    }

    OIIO::ImageBuf workingBuf;
    if (resolution_scale < 0.99f) {
        int new_w = std::max(1, static_cast<int>(std::round(result.width * resolution_scale)));
        int new_h = std::max(1, static_cast<int>(std::round(result.height * resolution_scale)));
        
        OIIO::ImageSpec dstSpec(new_w, new_h, result.channels, OIIO::TypeDesc::HALF);
        workingBuf = OIIO::ImageBuf(dstSpec);

        if (!OIIO::ImageBufAlgo::resize(workingBuf, srcBuf, "box", 1.0f)) {
            std::cerr << "NativeDecoder Error: Resize failed: " 
                      << file_path << " (" << workingBuf.geterror() << ")" << std::endl;
            return result;
        }
        result.width = new_w;
        result.height = new_h;
    } else {
        workingBuf = srcBuf;
    }

    size_t reqElements = static_cast<size_t>(result.width) * result.height * result.channels;
    result.pixel_data.resize(reqElements);

    // Read pixel data converted directly to HALF (float16) inside the OIIO buffer algos
    OIIO::ROI roi(0, result.width, 0, result.height, 0, 1, 0, result.channels);
    if (!workingBuf.get_pixels(roi, OIIO::TypeDesc::HALF, result.pixel_data.data())) {
        std::cerr << "NativeDecoder Error: get_pixels failed: " 
                  << file_path << " (" << workingBuf.geterror() << ")" << std::endl;
        return result;
    }

    if (result.channels == 3) {
        size_t pixel_count = static_cast<size_t>(result.width) * result.height;
        std::vector<uint16_t> rgba(pixel_count * 4);
        const uint16_t alphaVal = 0x3C00; // 1.0f in half-float
        for (size_t i = 0; i < pixel_count; ++i) {
            rgba[i * 4 + 0] = result.pixel_data[i * 3 + 0];
            rgba[i * 4 + 1] = result.pixel_data[i * 3 + 1];
            rgba[i * 4 + 2] = result.pixel_data[i * 3 + 2];
            rgba[i * 4 + 3] = alphaVal;
        }
        result.pixel_data = std::move(rgba);
        result.channels = 4;
    }

    result.success = true;
    return result;
}

} // namespace NativeDecoder
