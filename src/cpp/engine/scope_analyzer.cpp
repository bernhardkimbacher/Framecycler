#include "scope_analyzer.h"

#include <algorithm>
#include <cmath>
#include <cstring>

namespace ScopeAnalyzer {
namespace {

inline float half_to_float(uint16_t h)
{
    const uint32_t sign = (static_cast<uint32_t>(h) & 0x8000u) << 16;
    uint32_t exp = (h >> 10) & 0x1fu;
    uint32_t mant = h & 0x3ffu;
    uint32_t bits;
    if (exp == 0) {
        if (mant == 0) {
            bits = sign;
        } else {
            // Subnormal
            exp = 127 - 15 + 1;
            while ((mant & 0x400u) == 0) {
                mant <<= 1;
                --exp;
            }
            mant &= 0x3ffu;
            bits = sign | (exp << 23) | (mant << 13);
        }
    } else if (exp == 31) {
        bits = sign | 0x7f800000u | (mant << 13);
    } else {
        bits = sign | ((exp + (127 - 15)) << 23) | (mant << 13);
    }
    float out;
    std::memcpy(&out, &bits, sizeof(out));
    return out;
}

inline int clampi(int v, int lo, int hi)
{
    return std::max(lo, std::min(hi, v));
}

void accumulate_waveform(const float* rgb, int h, int w, int channel, float* out)
{
    // out: [bins * w], row-major bins x width
    std::memset(out, 0, sizeof(float) * static_cast<size_t>(kWaveformBins) * w);
    const float scale = static_cast<float>(kWaveformBins - 1);
    for (int y = 0; y < h; ++y) {
        const float* row = rgb + static_cast<size_t>(y) * w * 3;
        for (int x = 0; x < w; ++x) {
            const float r = row[x * 3 + 0];
            const float g = row[x * 3 + 1];
            const float b = row[x * 3 + 2];
            float sig;
            if (channel == 0) {
                sig = r;
            } else if (channel == 1) {
                sig = g;
            } else if (channel == 2) {
                sig = b;
            } else {
                sig = 0.2126f * r + 0.7152f * g + 0.0722f * b;
            }
            int bin = static_cast<int>(sig * scale);
            bin = clampi(bin, 0, kWaveformBins - 1);
            out[static_cast<size_t>(bin) * w + x] += 1.0f;
        }
    }
}

void accumulate_parade(const float* rgb, int h, int w, float* out)
{
    // out: [bins * 3w]
    std::memset(out, 0, sizeof(float) * static_cast<size_t>(kWaveformBins) * w * 3);
    const float scale = static_cast<float>(kWaveformBins - 1);
    const int total_w = w * 3;
    for (int y = 0; y < h; ++y) {
        const float* row = rgb + static_cast<size_t>(y) * w * 3;
        for (int x = 0; x < w; ++x) {
            for (int c = 0; c < 3; ++c) {
                float sig = row[x * 3 + c];
                int bin = clampi(static_cast<int>(sig * scale), 0, kWaveformBins - 1);
                out[static_cast<size_t>(bin) * total_w + c * w + x] += 1.0f;
            }
        }
    }
}

void accumulate_histogram(const float* rgb, int h, int w, float* out)
{
    std::memset(out, 0, sizeof(float) * 3 * kHistBins);
    const float scale = static_cast<float>(kHistBins - 1);
    const size_t n = static_cast<size_t>(h) * w;
    for (size_t i = 0; i < n; ++i) {
        for (int c = 0; c < 3; ++c) {
            int bin = clampi(static_cast<int>(rgb[i * 3 + c] * scale), 0, kHistBins - 1);
            out[c * kHistBins + bin] += 1.0f;
        }
    }
}

void accumulate_vectorscope(const float* rgb, int h, int w, float* out)
{
    std::memset(out, 0, sizeof(float) * kVectorSize * kVectorSize);
    const float half = (kVectorSize - 1) * 0.5f;
    const size_t n = static_cast<size_t>(h) * w;
    for (size_t i = 0; i < n; ++i) {
        const float r = rgb[i * 3 + 0];
        const float g = rgb[i * 3 + 1];
        const float b = rgb[i * 3 + 2];
        const float cb = -0.114572f * r - 0.385428f * g + 0.5f * b;
        const float cr = 0.5f * r - 0.454153f * g - 0.045847f * b;
        int xi = clampi(static_cast<int>(std::lround(cb * kVectorSize + half)), 0, kVectorSize - 1);
        int yi = clampi(static_cast<int>(std::lround(-cr * kVectorSize + half)), 0, kVectorSize - 1);
        out[yi * kVectorSize + xi] += 1.0f;
    }
}

void accumulate_cie(const float* rgb, int h, int w, float* out)
{
    std::memset(out, 0, sizeof(float) * kCieGrid * kCieGrid);
    // Rec.709 RGB → XYZ
    constexpr float m00 = 0.4124564f, m01 = 0.3575761f, m02 = 0.1804375f;
    constexpr float m10 = 0.2126729f, m11 = 0.7151522f, m12 = 0.0721750f;
    constexpr float m20 = 0.0193339f, m21 = 0.1191920f, m22 = 0.9503041f;
    const size_t n = static_cast<size_t>(h) * w;
    for (size_t i = 0; i < n; ++i) {
        float r = std::max(0.0f, rgb[i * 3 + 0]);
        float g = std::max(0.0f, rgb[i * 3 + 1]);
        float b = std::max(0.0f, rgb[i * 3 + 2]);
        float X = m00 * r + m01 * g + m02 * b;
        float Y = m10 * r + m11 * g + m12 * b;
        float Z = m20 * r + m21 * g + m22 * b;
        float s = X + Y + Z;
        if (s <= 1e-8f) {
            continue;
        }
        float xx = X / s;
        float yy = Y / s;
        if (xx + yy <= 1e-6f) {
            continue;
        }
        int xi = clampi(static_cast<int>(std::lround(xx / 0.8f * (kCieGrid - 1))), 0, kCieGrid - 1);
        int yi = clampi(static_cast<int>(std::lround((1.0f - yy / 0.9f) * (kCieGrid - 1))), 0, kCieGrid - 1);
        out[yi * kCieGrid + xi] += 1.0f;
    }
}

} // namespace

ScopeType scope_type_from_name(const std::string& name)
{
    if (name == "waveform") {
        return ScopeType::Waveform;
    }
    if (name == "parade") {
        return ScopeType::Parade;
    }
    if (name == "vectorscope") {
        return ScopeType::Vectorscope;
    }
    if (name == "histogram") {
        return ScopeType::Histogram;
    }
    if (name == "cie") {
        return ScopeType::Cie;
    }
    return ScopeType::Waveform;
}

std::vector<float> downsample_half_rgb(const uint16_t* src,
                                       int height,
                                       int width,
                                       int channels,
                                       int max_width,
                                       int& out_height,
                                       int& out_width)
{
    out_height = 0;
    out_width = 0;
    if (!src || height <= 0 || width <= 0 || channels < 3 || max_width <= 0) {
        return {};
    }
    const int step = (width <= max_width) ? 1 : static_cast<int>(std::ceil(width / static_cast<double>(max_width)));
    out_width = (width + step - 1) / step;
    // Keep full height (column scopes need vertical samples); stride only in X.
    out_height = height;
    std::vector<float> out(static_cast<size_t>(out_height) * out_width * 3);
    for (int y = 0; y < height; ++y) {
        const uint16_t* row = src + static_cast<size_t>(y) * width * channels;
        float* dst = out.data() + static_cast<size_t>(y) * out_width * 3;
        for (int ox = 0, x = 0; ox < out_width; ++ox, x += step) {
            const uint16_t* px = row + static_cast<size_t>(x) * channels;
            dst[ox * 3 + 0] = half_to_float(px[0]);
            dst[ox * 3 + 1] = half_to_float(px[1]);
            dst[ox * 3 + 2] = half_to_float(px[2]);
        }
    }
    return out;
}

std::vector<float> downsample_frame(CacheManager& cache,
                                    int frame_index,
                                    int max_width,
                                    int& out_height,
                                    int& out_width)
{
    out_height = 0;
    out_width = 0;
    std::vector<float> result;
    const bool ok = cache.with_active_frame(
        frame_index,
        [&](const uint16_t* data, int width, int height, int channels) {
            result = downsample_half_rgb(
                data, height, width, channels, max_width, out_height, out_width);
        });
    if (!ok) {
        return {};
    }
    return result;
}

std::vector<float> accumulate(const float* rgb, int height, int width, ScopeType type)
{
    if (!rgb || height <= 0 || width <= 0) {
        return {};
    }
    switch (type) {
    case ScopeType::Waveform: {
        std::vector<float> out(static_cast<size_t>(kWaveformBins) * width);
        accumulate_waveform(rgb, height, width, /*luma*/ -1, out.data());
        return out;
    }
    case ScopeType::Parade: {
        std::vector<float> out(static_cast<size_t>(kWaveformBins) * width * 3);
        accumulate_parade(rgb, height, width, out.data());
        return out;
    }
    case ScopeType::Vectorscope: {
        std::vector<float> out(static_cast<size_t>(kVectorSize) * kVectorSize);
        accumulate_vectorscope(rgb, height, width, out.data());
        return out;
    }
    case ScopeType::Histogram: {
        std::vector<float> out(3 * kHistBins);
        accumulate_histogram(rgb, height, width, out.data());
        return out;
    }
    case ScopeType::Cie: {
        std::vector<float> out(static_cast<size_t>(kCieGrid) * kCieGrid);
        accumulate_cie(rgb, height, width, out.data());
        return out;
    }
    }
    return {};
}

} // namespace ScopeAnalyzer
