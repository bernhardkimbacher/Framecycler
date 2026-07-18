#include "native_decoder.h"

#include <OpenImageIO/imageio.h>

#include <algorithm>
#include <cmath>
#include <functional>
#include <iostream>
#include <memory>
#include <unordered_map>

namespace NativeDecoder {

namespace {

constexpr uint16_t kHalfOne = 0x3C00;   // 1.0f
constexpr uint16_t kHalfGray = 0x2A66;  // ~0.05f
constexpr uint16_t kHalfBg = 0x2E66;    // ~0.1f

bool channels_are_contiguous(const std::vector<int>& indices)
{
    if (indices.empty()) {
        return false;
    }
    for (size_t i = 1; i < indices.size(); ++i) {
        if (indices[i] != indices[0] + static_cast<int>(i)) {
            return false;
        }
    }
    return true;
}

void fill_placeholder_pixels(uint16_t* dest,
                             int width,
                             int height,
                             const std::string& fallback_mode)
{
    const size_t pixel_count = static_cast<size_t>(width) * static_cast<size_t>(height);
    if (fallback_mode == "Red X") {
        for (int y = 0; y < height; ++y) {
            for (int x = 0; x < width; ++x) {
                const bool is_main_diag =
                    std::abs((double)y / height - (double)x / width) < 0.015;
                const bool is_anti_diag =
                    std::abs((double)y / height - (1.0 - (double)x / width)) < 0.015;
                const size_t idx = (static_cast<size_t>(y) * width + x) * 4;
                if (is_main_diag || is_anti_diag) {
                    dest[idx + 0] = kHalfOne;
                    dest[idx + 1] = 0;
                    dest[idx + 2] = 0;
                    dest[idx + 3] = kHalfOne;
                } else {
                    dest[idx + 0] = kHalfBg;
                    dest[idx + 1] = kHalfBg;
                    dest[idx + 2] = kHalfBg;
                    dest[idx + 3] = kHalfOne;
                }
            }
        }
    } else {
        for (size_t i = 0; i < pixel_count; ++i) {
            dest[i * 4 + 0] = kHalfGray;
            dest[i * 4 + 1] = kHalfGray;
            dest[i * 4 + 2] = kHalfGray;
            dest[i * 4 + 3] = kHalfOne;
        }
    }
}

int choose_miplevel(OIIO::ImageInput& in, int full_w, int full_h, int target_w, int target_h)
{
    // Prefer the smallest mip whose dimensions are still >= target.
    int best = 0;
    int level = 1;
    while (true) {
        OIIO::ImageSpec mip_spec;
        if (!in.seek_subimage(0, level, mip_spec)) {
            break;
        }
        if (mip_spec.width < target_w || mip_spec.height < target_h) {
            break;
        }
        best = level;
        ++level;
    }
    // Restore to chosen level (or base).
    OIIO::ImageSpec unused;
    in.seek_subimage(0, best, unused);
    (void)full_w;
    (void)full_h;
    return best;
}

void box_decimate_rgba(const float* src,
                       int src_w,
                       int src_h,
                       int src_channels,
                       uint16_t* dst,
                       int dst_w,
                       int dst_h,
                       int dst_channels)
{
    const int copy_ch = std::min(src_channels, dst_channels);
    for (int y = 0; y < dst_h; ++y) {
        const int y0 = (y * src_h) / dst_h;
        const int y1 = std::max(y0 + 1, ((y + 1) * src_h) / dst_h);
        for (int x = 0; x < dst_w; ++x) {
            const int x0 = (x * src_w) / dst_w;
            const int x1 = std::max(x0 + 1, ((x + 1) * src_w) / dst_w);
            float acc[4] = {0, 0, 0, 0};
            int count = 0;
            for (int sy = y0; sy < y1; ++sy) {
                for (int sx = x0; sx < x1; ++sx) {
                    const size_t sidx =
                        (static_cast<size_t>(sy) * src_w + sx) * src_channels;
                    for (int c = 0; c < copy_ch; ++c) {
                        acc[c] += src[sidx + c];
                    }
                    ++count;
                }
            }
            const float inv = count > 0 ? 1.0f / static_cast<float>(count) : 0.0f;
            const size_t didx =
                (static_cast<size_t>(y) * dst_w + x) * dst_channels;
            float out_f[4] = {0, 0, 0, 1.0f};
            for (int c = 0; c < copy_ch; ++c) {
                out_f[c] = acc[c] * inv;
            }
            if (dst_channels == 4 && src_channels < 4) {
                out_f[3] = 1.0f;
            }
            OIIO::convert_pixel_values(OIIO::TypeDesc::FLOAT, out_f,
                                       OIIO::TypeDesc::HALF, dst + didx,
                                       dst_channels);
        }
    }
}

bool read_channels_fullres(OIIO::ImageInput& in,
                           int miplevel,
                           int width,
                           int height,
                           const std::vector<int>& target_channels,
                           int out_channels,
                           uint16_t* dest)
{
    const size_t pixel_count = static_cast<size_t>(width) * static_cast<size_t>(height);
    const int n_target = static_cast<int>(target_channels.size());

    if (n_target == 1 && out_channels == 1) {
        const int ch = target_channels[0];
        return in.read_image(0, miplevel, ch, ch + 1, OIIO::TypeDesc::HALF, dest);
    }

    // Contiguous RGB[A] → write with RGBA stride when expanding RGB→RGBA.
    if (channels_are_contiguous(target_channels) && n_target >= 3) {
        const int chbegin = target_channels[0];
        const int chend = target_channels.back() + 1;
        const OIIO::stride_t xstride =
            static_cast<OIIO::stride_t>(out_channels * sizeof(uint16_t));
        if (!in.read_image(0, miplevel, chbegin, chend, OIIO::TypeDesc::HALF, dest,
                           xstride)) {
            return false;
        }
        if (out_channels == 4 && n_target == 3) {
            for (size_t p = 0; p < pixel_count; ++p) {
                dest[p * 4 + 3] = kHalfOne;
            }
        }
        return true;
    }

    // Scattered channels: read covering range, then gather.
    int ch_min = target_channels[0];
    int ch_max = target_channels[0];
    for (int ch : target_channels) {
        ch_min = std::min(ch_min, ch);
        ch_max = std::max(ch_max, ch);
    }
    const int range = ch_max - ch_min + 1;
    std::vector<uint16_t> scratch(pixel_count * static_cast<size_t>(range));
    if (!in.read_image(0, miplevel, ch_min, ch_max + 1, OIIO::TypeDesc::HALF,
                       scratch.data())) {
        return false;
    }
    for (size_t p = 0; p < pixel_count; ++p) {
        for (int c = 0; c < n_target; ++c) {
            const int src_c = target_channels[c] - ch_min;
            dest[p * out_channels + c] =
                scratch[p * static_cast<size_t>(range) + src_c];
        }
        if (out_channels == 4 && n_target == 3) {
            dest[p * 4 + 3] = kHalfOne;
        }
    }
    return true;
}

bool read_channels_float(OIIO::ImageInput& in,
                         int miplevel,
                         int width,
                         int height,
                         const std::vector<int>& target_channels,
                         int out_channels,
                         float* dest)
{
    const size_t pixel_count = static_cast<size_t>(width) * static_cast<size_t>(height);
    const int n_target = static_cast<int>(target_channels.size());

    if (n_target == 1 && out_channels == 1) {
        const int ch = target_channels[0];
        return in.read_image(0, miplevel, ch, ch + 1, OIIO::TypeDesc::FLOAT, dest);
    }

    if (channels_are_contiguous(target_channels) && n_target >= 3) {
        const int chbegin = target_channels[0];
        const int chend = target_channels.back() + 1;
        const OIIO::stride_t xstride =
            static_cast<OIIO::stride_t>(out_channels * sizeof(float));
        if (!in.read_image(0, miplevel, chbegin, chend, OIIO::TypeDesc::FLOAT, dest,
                           xstride)) {
            return false;
        }
        if (out_channels == 4 && n_target == 3) {
            for (size_t p = 0; p < pixel_count; ++p) {
                dest[p * 4 + 3] = 1.0f;
            }
        }
        return true;
    }

    int ch_min = target_channels[0];
    int ch_max = target_channels[0];
    for (int ch : target_channels) {
        ch_min = std::min(ch_min, ch);
        ch_max = std::max(ch_max, ch);
    }
    const int range = ch_max - ch_min + 1;
    std::vector<float> scratch(pixel_count * static_cast<size_t>(range));
    if (!in.read_image(0, miplevel, ch_min, ch_max + 1, OIIO::TypeDesc::FLOAT,
                       scratch.data())) {
        return false;
    }
    for (size_t p = 0; p < pixel_count; ++p) {
        for (int c = 0; c < n_target; ++c) {
            const int src_c = target_channels[c] - ch_min;
            dest[p * out_channels + c] =
                scratch[p * static_cast<size_t>(range) + src_c];
        }
        if (out_channels == 4 && n_target == 3) {
            dest[p * 4 + 3] = 1.0f;
        }
    }
    return true;
}

bool decode_opened(OIIO::ImageInput& in,
                   const OIIO::ImageSpec& base_spec,
                   const std::vector<int>& target_channels,
                   float resolution_scale,
                   uint16_t* dest,
                   size_t dest_capacity,
                   int& out_width,
                   int& out_height,
                   int& out_channels)
{
    const int n_target = static_cast<int>(target_channels.size());
    out_channels = (n_target == 3) ? 4 : n_target;
    if (out_channels != 1 && out_channels != 4) {
        out_channels = 4;
    }

    const int full_w = base_spec.width;
    const int full_h = base_spec.height;
    out_width = full_w;
    out_height = full_h;
    if (resolution_scale < 0.99f) {
        out_width = std::max(1, static_cast<int>(std::round(full_w * resolution_scale)));
        out_height = std::max(1, static_cast<int>(std::round(full_h * resolution_scale)));
    }

    const size_t needed =
        static_cast<size_t>(out_width) * static_cast<size_t>(out_height) * out_channels;
    if (dest_capacity < needed) {
        std::cerr << "NativeDecoder Error: destination buffer too small ("
                  << dest_capacity << " < " << needed << ")" << std::endl;
        return false;
    }

    const bool needs_scale = (out_width != full_w || out_height != full_h);
    if (!needs_scale) {
        return read_channels_fullres(in, 0, full_w, full_h, target_channels, out_channels, dest);
    }

    const int miplevel = choose_miplevel(in, full_w, full_h, out_width, out_height);
    OIIO::ImageSpec mip_spec = in.spec();
    {
        OIIO::ImageSpec tmp;
        if (in.seek_subimage(0, miplevel, tmp)) {
            mip_spec = tmp;
        }
    }

    if (mip_spec.width == out_width && mip_spec.height == out_height) {
        return read_channels_fullres(in, miplevel, out_width, out_height, target_channels,
                                     out_channels, dest);
    }

    const int src_w = mip_spec.width;
    const int src_h = mip_spec.height;
    const int src_out_ch = out_channels;
    std::vector<float> full(
        static_cast<size_t>(src_w) * static_cast<size_t>(src_h) * src_out_ch);
    if (!read_channels_float(in, miplevel, src_w, src_h, target_channels, src_out_ch,
                             full.data())) {
        return false;
    }
    box_decimate_rgba(full.data(), src_w, src_h, src_out_ch, dest, out_width, out_height,
                      out_channels);
    return true;
}

} // namespace

std::vector<int> get_layer_channel_indices(const std::vector<std::string>& channel_names,
                                           const std::string& layer)
{
    if (!layer.empty()) {
        std::unordered_map<std::string, int> component_map;
        for (size_t i = 0; i < channel_names.size(); ++i) {
            const auto& name = channel_names[i];
            if (name == layer) {
                if (component_map.find("R") == component_map.end()) {
                    component_map["R"] = static_cast<int>(i);
                }
            } else if (name.rfind(layer + ".", 0) == 0) {
                std::string component = name.substr(layer.length() + 1);
                component_map[component] = static_cast<int>(i);
            }
        }
        if (component_map.find("R") != component_map.end() &&
            component_map.find("G") != component_map.end() &&
            component_map.find("B") != component_map.end()) {
            std::vector<int> indices = {component_map["R"], component_map["G"],
                                        component_map["B"]};
            if (component_map.find("A") != component_map.end()) {
                indices.push_back(component_map["A"]);
            }
            return indices;
        }
        if (component_map.find("Y") != component_map.end()) {
            return {component_map["Y"]};
        }
    }

    std::unordered_map<std::string, int> flat;
    for (size_t i = 0; i < channel_names.size(); ++i) {
        flat[channel_names[i]] = static_cast<int>(i);
    }
    if (flat.find("R") != flat.end() && flat.find("G") != flat.end() &&
        flat.find("B") != flat.end()) {
        std::vector<int> indices = {flat["R"], flat["G"], flat["B"]};
        if (flat.find("A") != flat.end()) {
            indices.push_back(flat["A"]);
        }
        return indices;
    }

    std::unordered_map<std::string, int> beauty;
    for (size_t i = 0; i < channel_names.size(); ++i) {
        const auto& name = channel_names[i];
        if (name.rfind("beauty.", 0) == 0) {
            beauty[name.substr(7)] = static_cast<int>(i);
        }
    }
    if (beauty.find("R") != beauty.end() && beauty.find("G") != beauty.end() &&
        beauty.find("B") != beauty.end()) {
        std::vector<int> indices = {beauty["R"], beauty["G"], beauty["B"]};
        if (beauty.find("A") != beauty.end()) {
            indices.push_back(beauty["A"]);
        }
        return indices;
    }

    if (channel_names.size() >= 3) {
        return {0, 1, 2};
    }
    return {0};
}

void set_decode_threads(int n)
{
    n = std::max(1, n);
    OIIO::attribute("threads", n);
    OIIO::attribute("exr_threads", n);
}

bool probe_output_size(const std::string& file_path,
                       float resolution_scale,
                       const std::string& layer,
                       int& out_width,
                       int& out_height,
                       int& out_channels)
{
    auto in = OIIO::ImageInput::open(file_path);
    if (!in) {
        return false;
    }
    const OIIO::ImageSpec& spec = in->spec();
    const std::vector<int> targets = get_layer_channel_indices(spec.channelnames, layer);
    const int n_target = static_cast<int>(targets.size());
    out_channels = (n_target == 3) ? 4 : ((n_target == 1) ? 1 : 4);
    out_width = spec.width;
    out_height = spec.height;
    if (resolution_scale < 0.99f) {
        out_width = std::max(1, static_cast<int>(std::round(spec.width * resolution_scale)));
        out_height = std::max(1, static_cast<int>(std::round(spec.height * resolution_scale)));
    }
    return true;
}

bool decode_frame_into(const std::string& file_path,
                       float resolution_scale,
                       const std::string& layer,
                       uint16_t* dest,
                       size_t dest_capacity,
                       int& out_width,
                       int& out_height,
                       int& out_channels,
                       bool& wrote_placeholder,
                       const std::string& fallback_mode,
                       int placeholder_width,
                       int placeholder_height)
{
    return decode_with_allocator(
        file_path,
        resolution_scale,
        layer,
        [&](int w, int h, int ch) -> uint16_t* {
            const size_t needed =
                static_cast<size_t>(w) * static_cast<size_t>(h) * static_cast<size_t>(ch);
            if (!dest || dest_capacity < needed) {
                return nullptr;
            }
            return dest;
        },
        out_width,
        out_height,
        out_channels,
        wrote_placeholder,
        fallback_mode,
        placeholder_width,
        placeholder_height);
}

bool decode_with_allocator(const std::string& file_path,
                           float resolution_scale,
                           const std::string& layer,
                           const std::function<uint16_t*(int, int, int)>& allocate,
                           int& out_width,
                           int& out_height,
                           int& out_channels,
                           bool& wrote_placeholder,
                           const std::string& fallback_mode,
                           int placeholder_width,
                           int placeholder_height)
{
    wrote_placeholder = false;
    out_width = 0;
    out_height = 0;
    out_channels = 0;

    auto fill_ph = [&]() -> bool {
        const int raw_w = (placeholder_width > 0) ? placeholder_width : 1920;
        const int raw_h = (placeholder_height > 0) ? placeholder_height : 1080;
        out_width = std::max(1, static_cast<int>(std::round(raw_w * resolution_scale)));
        out_height = std::max(1, static_cast<int>(std::round(raw_h * resolution_scale)));
        out_channels = 4;
        uint16_t* dest = allocate(out_width, out_height, out_channels);
        if (!dest) {
            return false;
        }
        fill_placeholder_pixels(dest, out_width, out_height, fallback_mode);
        wrote_placeholder = true;
        return false;
    };

    auto in = OIIO::ImageInput::open(file_path);
    if (!in) {
        std::cerr << "NativeDecoder Error: Failed to open image: " << file_path
                  << " (" << OIIO::geterror() << ")" << std::endl;
        return fill_ph();
    }

    const OIIO::ImageSpec base_spec = in->spec();
    const std::vector<int> targets =
        get_layer_channel_indices(base_spec.channelnames, layer);
    const int n_target = static_cast<int>(targets.size());
    out_channels = (n_target == 3) ? 4 : ((n_target == 1) ? 1 : 4);
    out_width = base_spec.width;
    out_height = base_spec.height;
    if (resolution_scale < 0.99f) {
        out_width =
            std::max(1, static_cast<int>(std::round(base_spec.width * resolution_scale)));
        out_height =
            std::max(1, static_cast<int>(std::round(base_spec.height * resolution_scale)));
    }

    uint16_t* dest = allocate(out_width, out_height, out_channels);
    if (!dest) {
        std::cerr << "NativeDecoder Error: allocate() returned null for " << file_path
                  << std::endl;
        return false;
    }

    const size_t capacity =
        static_cast<size_t>(out_width) * static_cast<size_t>(out_height) * out_channels;
    if (!decode_opened(*in, base_spec, targets, resolution_scale, dest, capacity, out_width,
                       out_height, out_channels)) {
        std::cerr << "NativeDecoder Error: read failed: " << file_path << " ("
                  << in->geterror() << ")" << std::endl;
        return false;
    }
    return true;
}

DecodeResult decode_frame(const std::string& file_path,
                          float resolution_scale,
                          const std::string& layer,
                          const std::string& fallback_mode,
                          int placeholder_width,
                          int placeholder_height)
{
    DecodeResult result;
    result.success = false;

    bool wrote_placeholder = false;
    result.success = decode_with_allocator(
        file_path,
        resolution_scale,
        layer,
        [&](int w, int h, int ch) -> uint16_t* {
            result.pixel_data.resize(static_cast<size_t>(w) * h * ch);
            return result.pixel_data.data();
        },
        result.width,
        result.height,
        result.channels,
        wrote_placeholder,
        fallback_mode,
        placeholder_width,
        placeholder_height);

    if (!result.success && wrote_placeholder) {
        // Placeholder path: buffer is filled; success stays false (matches prior API).
        return result;
    }
    if (!result.success) {
        result.pixel_data.clear();
        result.width = 0;
        result.height = 0;
        result.channels = 0;
    }
    return result;
}

} // namespace NativeDecoder
