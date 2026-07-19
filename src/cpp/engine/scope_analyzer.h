#pragma once

#include <cstdint>
#include <string>
#include <vector>

#include "cache_manager.h"

namespace ScopeAnalyzer {

enum class ScopeType : int {
    Waveform = 0,
    Parade = 1,
    Vectorscope = 2,
    Histogram = 3,
    Cie = 4,
};

constexpr int kWaveformBins = 1024;
constexpr int kVectorSize = 256;
constexpr int kHistBins = 256;
constexpr int kCieGrid = 256;

/// Stride-downsample a cached float16 frame to float32 RGB (HxWx3),
/// without returning the full-resolution buffer to Python.
/// Returns empty vector on miss / failure.
std::vector<float> downsample_frame(CacheManager& cache,
                                    int frame_index,
                                    int max_width,
                                    int& out_height,
                                    int& out_width);

/// Downsample an already-resident float16 HxWxC buffer (non-owning).
std::vector<float> downsample_half_rgb(const uint16_t* src,
                                       int height,
                                       int width,
                                       int channels,
                                       int max_width,
                                       int& out_height,
                                       int& out_width);

/// Accumulate one scope type from float32 RGB (HxWx3 contiguous).
/// Returns flat float32 buffer; shape depends on type:
///   Waveform:     [bins * width]
///   Parade:       [bins * width * 3]
///   Vectorscope:  [size * size]
///   Histogram:    [3 * bins]
///   Cie:          [grid * grid]
std::vector<float> accumulate(const float* rgb,
                              int height,
                              int width,
                              ScopeType type);

ScopeType scope_type_from_name(const std::string& name);

} // namespace ScopeAnalyzer
