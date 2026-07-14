#include "native_decoder.h"
#include <OpenImageIO/imageio.h>
#include <OpenImageIO/imagebuf.h>
#include <OpenImageIO/imagebufalgo.h>
#include <iostream>
#include <cmath>
#include <algorithm>
#include <unordered_map>

namespace NativeDecoder {

std::vector<int> get_layer_channel_indices(const std::vector<std::string>& channel_names, const std::string& layer) {
    if (!layer.empty()) {
        std::unordered_map<std::string, int> component_map;
        for (size_t i = 0; i < channel_names.size(); ++i) {
            const auto& name = channel_names[i];
            if (name == layer) {
                if (component_map.find("R") == component_map.end()) {
                    component_map["R"] = static_cast<int>(i);
                }
            } else if (name.rfind(layer + ".", 0) == 0) { // starts with layer + "."
                std::string component = name.substr(layer.length() + 1);
                component_map[component] = static_cast<int>(i);
            }
        }
        // Check for RGB
        if (component_map.find("R") != component_map.end() &&
            component_map.find("G") != component_map.end() &&
            component_map.find("B") != component_map.end()) {
            std::vector<int> indices = { component_map["R"], component_map["G"], component_map["B"] };
            if (component_map.find("A") != component_map.end()) {
                indices.push_back(component_map["A"]);
            }
            return indices;
        }
        // Check for Y
        if (component_map.find("Y") != component_map.end()) {
            std::vector<int> indices = { component_map["Y"] };
            return indices;
        }
    }

    // Default fallback: check for R, G, B (and A)
    std::unordered_map<std::string, int> flat;
    for (size_t i = 0; i < channel_names.size(); ++i) {
        flat[channel_names[i]] = static_cast<int>(i);
    }
    if (flat.find("R") != flat.end() && flat.find("G") != flat.end() && flat.find("B") != flat.end()) {
        std::vector<int> indices = { flat["R"], flat["G"], flat["B"] };
        if (flat.find("A") != flat.end()) {
            indices.push_back(flat["A"]);
        }
        return indices;
    }

    // Next fallback: if we have beauty.R, beauty.G, beauty.B
    std::unordered_map<std::string, int> beauty;
    for (size_t i = 0; i < channel_names.size(); ++i) {
        const auto& name = channel_names[i];
        if (name.rfind("beauty.", 0) == 0) {
            std::string component = name.substr(7);
            beauty[component] = static_cast<int>(i);
        }
    }
    if (beauty.find("R") != beauty.end() && beauty.find("G") != beauty.end() && beauty.find("B") != beauty.end()) {
        std::vector<int> indices = { beauty["R"], beauty["G"], beauty["B"] };
        if (beauty.find("A") != beauty.end()) {
            indices.push_back(beauty["A"]);
        }
        return indices;
    }

    if (channel_names.size() >= 3) {
        return { 0, 1, 2 };
    }
    return { 0 };
}

DecodeResult decode_frame(const std::string& file_path, float resolution_scale, const std::string& layer, const std::string& fallback_mode, int placeholder_width, int placeholder_height)
{
    DecodeResult result;
    result.success = false;

    OIIO::ImageSpec spec;
    auto in = OIIO::ImageInput::open(file_path);
    if (!in) {
        std::cerr << "NativeDecoder Error: Failed to open image: " << file_path 
                  << " (" << OIIO::geterror() << ")" << std::endl;
        
        int raw_w = (placeholder_width > 0) ? placeholder_width : 1920;
        int raw_h = (placeholder_height > 0) ? placeholder_height : 1080;
        result.width = std::max(1, static_cast<int>(std::round(raw_w * resolution_scale)));
        result.height = std::max(1, static_cast<int>(std::round(raw_h * resolution_scale)));
        result.channels = 4;
        size_t total_size = static_cast<size_t>(result.width) * result.height * result.channels;
        result.pixel_data.resize(total_size);
        
        if (fallback_mode == "Red X") {
            uint16_t bgVal = 0x2E66;    // 0.1f dark gray
            uint16_t redVal = 0x3C00;   // 1.0f red
            uint16_t zeroVal = 0x0000;  // 0.0f
            uint16_t alphaVal = 0x3C00; // 1.0f alpha
            
            for (int y = 0; y < result.height; ++y) {
                for (int x = 0; x < result.width; ++x) {
                    bool is_main_diag = std::abs((double)y / result.height - (double)x / result.width) < 0.015;
                    bool is_anti_diag = std::abs((double)y / result.height - (1.0 - (double)x / result.width)) < 0.015;
                    size_t idx = (static_cast<size_t>(y) * result.width + x) * 4;
                    if (is_main_diag || is_anti_diag) {
                        result.pixel_data[idx + 0] = redVal;
                        result.pixel_data[idx + 1] = zeroVal;
                        result.pixel_data[idx + 2] = zeroVal;
                        result.pixel_data[idx + 3] = alphaVal;
                    } else {
                        result.pixel_data[idx + 0] = bgVal;
                        result.pixel_data[idx + 1] = bgVal;
                        result.pixel_data[idx + 2] = bgVal;
                        result.pixel_data[idx + 3] = alphaVal;
                    }
                }
            }
        } else {
            // Default "Flat Gray"
            uint16_t grayVal = 0x2A66; // 0.05f in half-float
            uint16_t alphaVal = 0x3C00; // 1.0f in half-float
            for (size_t i = 0; i < total_size; i += 4) {
                result.pixel_data[i + 0] = grayVal;
                result.pixel_data[i + 1] = grayVal;
                result.pixel_data[i + 2] = grayVal;
                result.pixel_data[i + 3] = alphaVal;
            }
        }
        return result;
    }

    spec = in->spec();
    result.width = spec.width;
    result.height = spec.height;
    result.channels = spec.nchannels;
    
    std::vector<std::string> channel_names = spec.channelnames;
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
    std::vector<uint16_t> temp_data(reqElements);

    // Read pixel data converted directly to HALF (float16) inside the OIIO buffer algos
    OIIO::ROI roi(0, result.width, 0, result.height, 0, 1, 0, result.channels);
    if (!workingBuf.get_pixels(roi, OIIO::TypeDesc::HALF, temp_data.data())) {
        std::cerr << "NativeDecoder Error: get_pixels failed: " 
                  << file_path << " (" << workingBuf.geterror() << ")" << std::endl;
        return result;
    }

    // Filter target channels by active layer
    std::vector<int> target_channels = get_layer_channel_indices(channel_names, layer);
    
    size_t pixel_count = static_cast<size_t>(result.width) * result.height;
    int src_channels = result.channels;
    int dst_channels = static_cast<int>(target_channels.size());
    
    result.pixel_data.resize(pixel_count * dst_channels);
    for (size_t p = 0; p < pixel_count; ++p) {
        for (int c = 0; c < dst_channels; ++c) {
            result.pixel_data[p * dst_channels + c] = temp_data[p * src_channels + target_channels[c]];
        }
    }
    result.channels = dst_channels;

    if (result.channels == 3) {
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
