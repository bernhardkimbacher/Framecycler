#pragma once

#include <cstddef>
#include <cstdint>

namespace fc {

/// Scalar IEEE754 binary16 conversion (reference / tail / non-SIMD builds).
uint16_t float32_to_half_scalar(float value);

inline uint16_t u8_to_half_scalar(uint8_t v)
{
    return float32_to_half_scalar(static_cast<float>(v) / 255.0f);
}

inline uint16_t u16_to_half_scalar(uint16_t v)
{
    return float32_to_half_scalar(static_cast<float>(v) / 65535.0f);
}

/// Convert interleaved channel samples to float16 bit patterns.
/// ``count`` is the number of channel values (pixels * channels).
void convert_rgba64_to_half(const uint16_t* src, uint16_t* dst, size_t count);
void convert_rgba8_to_half(const uint8_t* src, uint16_t* dst, size_t count);
void convert_rgbaf32_to_half(const float* src, uint16_t* dst, size_t count);

/// Set alpha of packed RGBA16F pixels to 1.0 (0x3C00).
void fill_opaque_alpha_half(uint16_t* rgba, size_t pixel_count);

/// Active backend name: neon_f16 | sse_f16c | sse2 | scalar
const char* half_convert_backend();

/// Compare SIMD path against scalar on random buffers. Returns true on match.
bool half_convert_self_test();

} // namespace fc
