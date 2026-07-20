#include "half_convert.h"

#include <algorithm>
#include <cmath>
#include <cstring>
#include <random>
#include <vector>

#if defined(__ARM_NEON) || defined(__ARM_NEON__)
#include <arm_neon.h>
#define FC_HALF_NEON 1
#if defined(__ARM_FEATURE_FP16_VECTOR_ARITHMETIC)
#define FC_HALF_NEON_F16 1
#endif
#elif defined(__SSE2__) || defined(_M_X64) || (defined(_M_IX86_FP) && _M_IX86_FP >= 2)
#include <emmintrin.h>
#define FC_HALF_SSE2 1
#if defined(_MSC_VER)
#include <intrin.h>
#else
#include <cpuid.h>
#endif
#if defined(__GNUC__) || defined(__clang__)
#include <immintrin.h>
#define FC_HALF_F16C_DISPATCH 1
#elif defined(_MSC_VER) && defined(__AVX__)
#include <immintrin.h>
#define FC_HALF_F16C_DISPATCH 1
#endif
#endif

namespace fc {
namespace {

void convert_rgba64_to_half_scalar(const uint16_t* src, uint16_t* dst, size_t count)
{
    for (size_t i = 0; i < count; ++i) {
        dst[i] = u16_to_half_scalar(src[i]);
    }
}

void convert_rgba8_to_half_scalar(const uint8_t* src, uint16_t* dst, size_t count)
{
    for (size_t i = 0; i < count; ++i) {
        dst[i] = u8_to_half_scalar(src[i]);
    }
}

void convert_rgbaf32_to_half_scalar(const float* src, uint16_t* dst, size_t count)
{
    for (size_t i = 0; i < count; ++i) {
        dst[i] = float32_to_half_scalar(src[i]);
    }
}

bool halves_close(uint16_t a, uint16_t b, int max_ulp)
{
    return std::abs(static_cast<int>(a) - static_cast<int>(b)) <= max_ulp;
}

#if defined(FC_HALF_NEON)

void store_f32_as_half_neon(float32x4_t v, uint16_t* dst)
{
#if defined(FC_HALF_NEON_F16)
    const float16x4_t h = vcvt_f16_f32(v);
    vst1_u16(dst, vreinterpret_u16_f16(h));
#else
    alignas(16) float tmp[4];
    vst1q_f32(tmp, v);
    dst[0] = float32_to_half_scalar(tmp[0]);
    dst[1] = float32_to_half_scalar(tmp[1]);
    dst[2] = float32_to_half_scalar(tmp[2]);
    dst[3] = float32_to_half_scalar(tmp[3]);
#endif
}

void convert_rgba64_to_half_neon(const uint16_t* src, uint16_t* dst, size_t count)
{
    size_t i = 0;
    constexpr float kScale = 1.0f / 65535.0f;
    for (; i + 8 <= count; i += 8) {
        const uint16x8_t u = vld1q_u16(src + i);
        const uint32x4_t lo = vmovl_u16(vget_low_u16(u));
        const uint32x4_t hi = vmovl_u16(vget_high_u16(u));
        store_f32_as_half_neon(vmulq_n_f32(vcvtq_f32_u32(lo), kScale), dst + i);
        store_f32_as_half_neon(vmulq_n_f32(vcvtq_f32_u32(hi), kScale), dst + i + 4);
    }
    for (; i < count; ++i) {
        dst[i] = u16_to_half_scalar(src[i]);
    }
}

void convert_rgba8_to_half_neon(const uint8_t* src, uint16_t* dst, size_t count)
{
    size_t i = 0;
    constexpr float kScale = 1.0f / 255.0f;
    for (; i + 16 <= count; i += 16) {
        const uint8x16_t u8 = vld1q_u8(src + i);
        const uint16x8_t u16_lo = vmovl_u8(vget_low_u8(u8));
        const uint16x8_t u16_hi = vmovl_u8(vget_high_u8(u8));
        store_f32_as_half_neon(
            vmulq_n_f32(vcvtq_f32_u32(vmovl_u16(vget_low_u16(u16_lo))), kScale), dst + i);
        store_f32_as_half_neon(
            vmulq_n_f32(vcvtq_f32_u32(vmovl_u16(vget_high_u16(u16_lo))), kScale), dst + i + 4);
        store_f32_as_half_neon(
            vmulq_n_f32(vcvtq_f32_u32(vmovl_u16(vget_low_u16(u16_hi))), kScale), dst + i + 8);
        store_f32_as_half_neon(
            vmulq_n_f32(vcvtq_f32_u32(vmovl_u16(vget_high_u16(u16_hi))), kScale), dst + i + 12);
    }
    for (; i < count; ++i) {
        dst[i] = u8_to_half_scalar(src[i]);
    }
}

void convert_rgbaf32_to_half_neon(const float* src, uint16_t* dst, size_t count)
{
    size_t i = 0;
    for (; i + 4 <= count; i += 4) {
        store_f32_as_half_neon(vld1q_f32(src + i), dst + i);
    }
    for (; i < count; ++i) {
        dst[i] = float32_to_half_scalar(src[i]);
    }
}

#endif // FC_HALF_NEON

#if defined(FC_HALF_SSE2)

bool cpu_has_f16c()
{
    static const bool cached = []() {
#if defined(_MSC_VER)
        int info[4] = {};
        __cpuid(info, 1);
        return (info[2] & (1 << 29)) != 0;
#else
        unsigned int eax = 0, ebx = 0, ecx = 0, edx = 0;
        if (__get_cpuid(1, &eax, &ebx, &ecx, &edx)) {
            return (ecx & (1u << 29)) != 0;
        }
        return false;
#endif
    }();
    return cached;
}

void store_f32_as_half_sse2(const __m128 v, uint16_t* dst)
{
    alignas(16) float tmp[4];
    _mm_store_ps(tmp, v);
    dst[0] = float32_to_half_scalar(tmp[0]);
    dst[1] = float32_to_half_scalar(tmp[1]);
    dst[2] = float32_to_half_scalar(tmp[2]);
    dst[3] = float32_to_half_scalar(tmp[3]);
}

void convert_rgba64_to_half_sse2(const uint16_t* src, uint16_t* dst, size_t count)
{
    size_t i = 0;
    const __m128 scale = _mm_set1_ps(1.0f / 65535.0f);
    for (; i + 8 <= count; i += 8) {
        const __m128i u = _mm_loadu_si128(reinterpret_cast<const __m128i*>(src + i));
        const __m128i z = _mm_setzero_si128();
        store_f32_as_half_sse2(_mm_mul_ps(_mm_cvtepi32_ps(_mm_unpacklo_epi16(u, z)), scale), dst + i);
        store_f32_as_half_sse2(
            _mm_mul_ps(_mm_cvtepi32_ps(_mm_unpackhi_epi16(u, z)), scale), dst + i + 4);
    }
    for (; i < count; ++i) {
        dst[i] = u16_to_half_scalar(src[i]);
    }
}

void convert_rgba8_to_half_sse2(const uint8_t* src, uint16_t* dst, size_t count)
{
    size_t i = 0;
    const __m128 scale = _mm_set1_ps(1.0f / 255.0f);
    for (; i + 8 <= count; i += 8) {
        const __m128i bytes = _mm_loadl_epi64(reinterpret_cast<const __m128i*>(src + i));
        const __m128i z = _mm_setzero_si128();
        const __m128i u16 = _mm_unpacklo_epi8(bytes, z);
        store_f32_as_half_sse2(
            _mm_mul_ps(_mm_cvtepi32_ps(_mm_unpacklo_epi16(u16, z)), scale), dst + i);
        store_f32_as_half_sse2(
            _mm_mul_ps(_mm_cvtepi32_ps(_mm_unpackhi_epi16(u16, z)), scale), dst + i + 4);
    }
    for (; i < count; ++i) {
        dst[i] = u8_to_half_scalar(src[i]);
    }
}

void convert_rgbaf32_to_half_sse2(const float* src, uint16_t* dst, size_t count)
{
    size_t i = 0;
    for (; i + 4 <= count; i += 4) {
        store_f32_as_half_sse2(_mm_loadu_ps(src + i), dst + i);
    }
    for (; i < count; ++i) {
        dst[i] = float32_to_half_scalar(src[i]);
    }
}

#if defined(FC_HALF_F16C_DISPATCH)

#if defined(__GNUC__) || defined(__clang__)
__attribute__((target("f16c,sse2")))
#endif
void store_f32_as_half_f16c(const __m128 v, uint16_t* dst)
{
    const __m128i h = _mm_cvtps_ph(v, _MM_FROUND_TO_NEAREST_INT | _MM_FROUND_NO_EXC);
    _mm_storel_epi64(reinterpret_cast<__m128i*>(dst), h);
}

#if defined(__GNUC__) || defined(__clang__)
__attribute__((target("f16c,sse2")))
#endif
void convert_rgba64_to_half_f16c(const uint16_t* src, uint16_t* dst, size_t count)
{
    size_t i = 0;
    const __m128 scale = _mm_set1_ps(1.0f / 65535.0f);
    for (; i + 8 <= count; i += 8) {
        const __m128i u = _mm_loadu_si128(reinterpret_cast<const __m128i*>(src + i));
        const __m128i z = _mm_setzero_si128();
        store_f32_as_half_f16c(_mm_mul_ps(_mm_cvtepi32_ps(_mm_unpacklo_epi16(u, z)), scale), dst + i);
        store_f32_as_half_f16c(
            _mm_mul_ps(_mm_cvtepi32_ps(_mm_unpackhi_epi16(u, z)), scale), dst + i + 4);
    }
    for (; i < count; ++i) {
        dst[i] = u16_to_half_scalar(src[i]);
    }
}

#if defined(__GNUC__) || defined(__clang__)
__attribute__((target("f16c,sse2")))
#endif
void convert_rgba8_to_half_f16c(const uint8_t* src, uint16_t* dst, size_t count)
{
    size_t i = 0;
    const __m128 scale = _mm_set1_ps(1.0f / 255.0f);
    for (; i + 8 <= count; i += 8) {
        const __m128i bytes = _mm_loadl_epi64(reinterpret_cast<const __m128i*>(src + i));
        const __m128i z = _mm_setzero_si128();
        const __m128i u16 = _mm_unpacklo_epi8(bytes, z);
        store_f32_as_half_f16c(
            _mm_mul_ps(_mm_cvtepi32_ps(_mm_unpacklo_epi16(u16, z)), scale), dst + i);
        store_f32_as_half_f16c(
            _mm_mul_ps(_mm_cvtepi32_ps(_mm_unpackhi_epi16(u16, z)), scale), dst + i + 4);
    }
    for (; i < count; ++i) {
        dst[i] = u8_to_half_scalar(src[i]);
    }
}

#if defined(__GNUC__) || defined(__clang__)
__attribute__((target("f16c,sse2")))
#endif
void convert_rgbaf32_to_half_f16c(const float* src, uint16_t* dst, size_t count)
{
    size_t i = 0;
    for (; i + 4 <= count; i += 4) {
        store_f32_as_half_f16c(_mm_loadu_ps(src + i), dst + i);
    }
    for (; i < count; ++i) {
        dst[i] = float32_to_half_scalar(src[i]);
    }
}

#endif // FC_HALF_F16C_DISPATCH
#endif // FC_HALF_SSE2

} // namespace

uint16_t float32_to_half_scalar(float value)
{
    uint32_t bits = 0;
    std::memcpy(&bits, &value, sizeof(bits));
    const uint32_t sign = (bits >> 16) & 0x8000u;
    int32_t exp = static_cast<int32_t>((bits >> 23) & 0xff) - 127 + 15;
    uint32_t mant = bits & 0x7fffffu;

    if (exp <= 0) {
        if (exp < -10) {
            return static_cast<uint16_t>(sign);
        }
        mant |= 0x800000u;
        const uint32_t t = static_cast<uint32_t>(14 - exp);
        uint32_t half_mant = mant >> t;
        if ((mant >> (t - 1)) & 1u) {
            ++half_mant;
        }
        return static_cast<uint16_t>(sign | half_mant);
    }
    if (exp >= 31) {
        return static_cast<uint16_t>(sign | 0x7c00u);
    }
    uint32_t half = static_cast<uint32_t>(exp) << 10;
    half |= mant >> 13;
    if (mant & 0x1000u) {
        ++half;
    }
    return static_cast<uint16_t>(sign | half);
}

void convert_rgba64_to_half(const uint16_t* src, uint16_t* dst, size_t count)
{
    if (!src || !dst || count == 0) {
        return;
    }
#if defined(FC_HALF_NEON)
    convert_rgba64_to_half_neon(src, dst, count);
#elif defined(FC_HALF_SSE2) && defined(FC_HALF_F16C_DISPATCH)
    if (cpu_has_f16c()) {
        convert_rgba64_to_half_f16c(src, dst, count);
    } else {
        convert_rgba64_to_half_sse2(src, dst, count);
    }
#elif defined(FC_HALF_SSE2)
    convert_rgba64_to_half_sse2(src, dst, count);
#else
    convert_rgba64_to_half_scalar(src, dst, count);
#endif
}

void convert_rgba8_to_half(const uint8_t* src, uint16_t* dst, size_t count)
{
    if (!src || !dst || count == 0) {
        return;
    }
#if defined(FC_HALF_NEON)
    convert_rgba8_to_half_neon(src, dst, count);
#elif defined(FC_HALF_SSE2) && defined(FC_HALF_F16C_DISPATCH)
    if (cpu_has_f16c()) {
        convert_rgba8_to_half_f16c(src, dst, count);
    } else {
        convert_rgba8_to_half_sse2(src, dst, count);
    }
#elif defined(FC_HALF_SSE2)
    convert_rgba8_to_half_sse2(src, dst, count);
#else
    convert_rgba8_to_half_scalar(src, dst, count);
#endif
}

void convert_rgbaf32_to_half(const float* src, uint16_t* dst, size_t count)
{
    if (!src || !dst || count == 0) {
        return;
    }
#if defined(FC_HALF_NEON)
    convert_rgbaf32_to_half_neon(src, dst, count);
#elif defined(FC_HALF_SSE2) && defined(FC_HALF_F16C_DISPATCH)
    if (cpu_has_f16c()) {
        convert_rgbaf32_to_half_f16c(src, dst, count);
    } else {
        convert_rgbaf32_to_half_sse2(src, dst, count);
    }
#elif defined(FC_HALF_SSE2)
    convert_rgbaf32_to_half_sse2(src, dst, count);
#else
    convert_rgbaf32_to_half_scalar(src, dst, count);
#endif
}

void fill_opaque_alpha_half(uint16_t* rgba, size_t pixel_count)
{
    if (!rgba || pixel_count == 0) {
        return;
    }
    constexpr uint16_t kOne = 0x3C00;
    size_t p = 0;
    for (; p + 4 <= pixel_count; p += 4) {
        rgba[(p + 0) * 4 + 3] = kOne;
        rgba[(p + 1) * 4 + 3] = kOne;
        rgba[(p + 2) * 4 + 3] = kOne;
        rgba[(p + 3) * 4 + 3] = kOne;
    }
    for (; p < pixel_count; ++p) {
        rgba[p * 4 + 3] = kOne;
    }
}

const char* half_convert_backend()
{
#if defined(FC_HALF_NEON)
#if defined(FC_HALF_NEON_F16)
    return "neon_f16";
#else
    return "neon";
#endif
#elif defined(FC_HALF_SSE2)
#if defined(FC_HALF_F16C_DISPATCH)
    if (cpu_has_f16c()) {
        return "sse_f16c";
    }
#endif
    return "sse2";
#else
    return "scalar";
#endif
}

bool half_convert_self_test()
{
    constexpr size_t kN = 257;
    std::mt19937 rng(0xC0FFEEu);
    std::uniform_int_distribution<int> u8dist(0, 255);
    std::uniform_int_distribution<int> u16dist(0, 65535);
    std::uniform_real_distribution<float> fdist(-2.0f, 2.0f);

    std::vector<uint8_t> u8(kN);
    std::vector<uint16_t> u16(kN);
    std::vector<float> f32(kN);
    for (size_t i = 0; i < kN; ++i) {
        u8[i] = static_cast<uint8_t>(u8dist(rng));
        u16[i] = static_cast<uint16_t>(u16dist(rng));
        f32[i] = fdist(rng);
    }
    u8[0] = 0;
    u8[1] = 255;
    u16[0] = 0;
    u16[1] = 65535;
    f32[0] = 0.0f;
    f32[1] = 1.0f;
    f32[2] = -0.0f;

    std::vector<uint16_t> got(kN), ref(kN);

    convert_rgba8_to_half_scalar(u8.data(), ref.data(), kN);
    convert_rgba8_to_half(u8.data(), got.data(), kN);
    for (size_t i = 0; i < kN; ++i) {
        if (!halves_close(got[i], ref[i], 1)) {
            return false;
        }
    }

    convert_rgba64_to_half_scalar(u16.data(), ref.data(), kN);
    convert_rgba64_to_half(u16.data(), got.data(), kN);
    for (size_t i = 0; i < kN; ++i) {
        if (!halves_close(got[i], ref[i], 1)) {
            return false;
        }
    }

    convert_rgbaf32_to_half_scalar(f32.data(), ref.data(), kN);
    convert_rgbaf32_to_half(f32.data(), got.data(), kN);
    for (size_t i = 0; i < kN; ++i) {
        if (!halves_close(got[i], ref[i], 1)) {
            return false;
        }
    }
    return true;
}

} // namespace fc
