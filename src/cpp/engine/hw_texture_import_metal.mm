#include "hw_texture_import.h"

#import <CoreVideo/CoreVideo.h>
#import <Metal/Metal.h>
#import <Foundation/Foundation.h>

#include <mutex>
#include <string>

namespace {

id<MTLLibrary> g_library = nil;
id<MTLComputePipelineState> g_bgra_pso = nil;
id<MTLComputePipelineState> g_nv12_pso = nil;
std::mutex g_pso_mutex;

static const char* kShaders = R"METAL(
#include <metal_stdlib>
using namespace metal;

kernel void bgra8_to_rgba16f(
    texture2d<float, access::read> src [[texture(0)]],
    texture2d<float, access::write> dst [[texture(1)]],
    uint2 gid [[thread_position_in_grid]])
{
    if (gid.x >= dst.get_width() || gid.y >= dst.get_height()) return;
    float4 s = src.read(gid);
    // BGRA → RGBA
    dst.write(float4(s.b, s.g, s.r, s.a), gid);
}

kernel void nv12_to_rgba16f(
    texture2d<float, access::sample> luma [[texture(0)]],
    texture2d<float, access::sample> chroma [[texture(1)]],
    texture2d<float, access::write> dst [[texture(2)]],
    uint2 gid [[thread_position_in_grid]])
{
    if (gid.x >= dst.get_width() || gid.y >= dst.get_height()) return;
    constexpr sampler s(address::clamp_to_edge, filter::linear);
    float2 uv = (float2(gid) + 0.5f) / float2(dst.get_width(), dst.get_height());
    float y = luma.sample(s, uv).r;
    float2 cbcr = chroma.sample(s, uv).rg;
    float cb = cbcr.r - 0.5f;
    float cr = cbcr.g - 0.5f;
    // BT.709 full-range-ish (matches typical VT → sws path closely enough for review)
    float r = y + 1.5748f * cr;
    float g = y - 0.1873f * cb - 0.4681f * cr;
    float b = y + 1.8556f * cb;
    dst.write(float4(clamp(r, 0.0f, 1.0f), clamp(g, 0.0f, 1.0f), clamp(b, 0.0f, 1.0f), 1.0f), gid);
}
)METAL";

bool ensure_pipelines(id<MTLDevice> device, std::string* error)
{
    std::lock_guard<std::mutex> lock(g_pso_mutex);
    if (g_bgra_pso && g_nv12_pso) {
        return true;
    }
    NSError* nserr = nil;
    id<MTLLibrary> lib = [device newLibraryWithSource:@(kShaders) options:nil error:&nserr];
    if (!lib) {
        if (error) {
            *error = nserr ? std::string(nserr.localizedDescription.UTF8String)
                           : "Failed to compile Metal import shaders";
        }
        return false;
    }
    id<MTLFunction> bgraFn = [lib newFunctionWithName:@"bgra8_to_rgba16f"];
    id<MTLFunction> nv12Fn = [lib newFunctionWithName:@"nv12_to_rgba16f"];
    if (!bgraFn || !nv12Fn) {
        if (error) {
            *error = "Missing Metal import kernel functions";
        }
        return false;
    }
    g_bgra_pso = [device newComputePipelineStateWithFunction:bgraFn error:&nserr];
    if (!g_bgra_pso) {
        if (error) {
            *error = nserr ? std::string(nserr.localizedDescription.UTF8String)
                           : "Failed to create BGRA pipeline";
        }
        return false;
    }
    g_nv12_pso = [device newComputePipelineStateWithFunction:nv12Fn error:&nserr];
    if (!g_nv12_pso) {
        if (error) {
            *error = nserr ? std::string(nserr.localizedDescription.UTF8String)
                           : "Failed to create NV12 pipeline";
        }
        return false;
    }
    g_library = lib;
    return true;
}

id<MTLTexture> texture_from_cv(
    CVMetalTextureCacheRef cache,
    CVPixelBufferRef pix,
    MTLPixelFormat format,
    size_t plane,
    int width,
    int height,
    std::string* error)
{
    CVMetalTextureRef cvTex = nullptr;
    const CVReturn rc = CVMetalTextureCacheCreateTextureFromImage(
        kCFAllocatorDefault,
        cache,
        pix,
        nullptr,
        format,
        static_cast<size_t>(width),
        static_cast<size_t>(height),
        plane,
        &cvTex);
    if (rc != kCVReturnSuccess || !cvTex) {
        if (error) {
            *error = "CVMetalTextureCacheCreateTextureFromImage failed";
        }
        return nil;
    }
    id<MTLTexture> tex = CVMetalTextureGetTexture(cvTex);
    CFRelease(cvTex);
    return tex;
}

} // namespace

void* fc_metal_create_cv_texture_cache(void* mtl_device, std::string* error)
{
    id<MTLDevice> device = (__bridge id<MTLDevice>)mtl_device;
    if (!device) {
        if (error) {
            *error = "null MTLDevice";
        }
        return nullptr;
    }
    CVMetalTextureCacheRef cache = nullptr;
    const CVReturn rc = CVMetalTextureCacheCreate(
        kCFAllocatorDefault, nullptr, device, nullptr, &cache);
    if (rc != kCVReturnSuccess || !cache) {
        if (error) {
            *error = "CVMetalTextureCacheCreate failed";
        }
        return nullptr;
    }
    return cache;
}

void fc_metal_release_cv_texture_cache(void* cv_metal_texture_cache)
{
    if (cv_metal_texture_cache) {
        CFRelease(static_cast<CVMetalTextureCacheRef>(cv_metal_texture_cache));
    }
}

bool fc_metal_import_cvpixelbuffer_to_rgba16f(
    HwMetalImportContext* ctx,
    void* cv_pixel_buffer,
    void* dst_mtl_texture_rgba16f,
    int width,
    int height,
    std::string* error)
{
    if (!ctx || !ctx->mtl_device || !ctx->cv_metal_texture_cache || !cv_pixel_buffer
        || !dst_mtl_texture_rgba16f || width <= 0 || height <= 0) {
        if (error) {
            *error = "invalid Metal import arguments";
        }
        return false;
    }

    id<MTLDevice> device = (__bridge id<MTLDevice>)ctx->mtl_device;
    id<MTLCommandQueue> queue = ctx->mtl_command_queue
        ? (__bridge id<MTLCommandQueue>)ctx->mtl_command_queue
        : [device newCommandQueue];
    if (!queue) {
        if (error) {
            *error = "Failed to create MTLCommandQueue";
        }
        return false;
    }
    if (!ensure_pipelines(device, error)) {
        return false;
    }

    CVPixelBufferRef pix = static_cast<CVPixelBufferRef>(cv_pixel_buffer);
    CVMetalTextureCacheRef cache =
        static_cast<CVMetalTextureCacheRef>(ctx->cv_metal_texture_cache);
    id<MTLTexture> dst = (__bridge id<MTLTexture>)dst_mtl_texture_rgba16f;

    const OSType pix_fmt = CVPixelBufferGetPixelFormatType(pix);
    id<MTLCommandBuffer> cmd = [queue commandBuffer];
    id<MTLComputeCommandEncoder> enc = [cmd computeCommandEncoder];
    if (!cmd || !enc) {
        if (error) {
            *error = "Failed to create Metal command encoder";
        }
        return false;
    }

    const MTLSize grid = MTLSizeMake(static_cast<NSUInteger>(width), static_cast<NSUInteger>(height), 1);
    const MTLSize tg = MTLSizeMake(16, 16, 1);

    if (pix_fmt == kCVPixelFormatType_32BGRA) {
        id<MTLTexture> src = texture_from_cv(
            cache, pix, MTLPixelFormatBGRA8Unorm, 0, width, height, error);
        if (!src) {
            [enc endEncoding];
            return false;
        }
        [enc setComputePipelineState:g_bgra_pso];
        [enc setTexture:src atIndex:0];
        [enc setTexture:dst atIndex:1];
        [enc dispatchThreads:grid threadsPerThreadgroup:tg];
    } else if (
        pix_fmt == kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange
        || pix_fmt == kCVPixelFormatType_420YpCbCr8BiPlanarFullRange) {
        id<MTLTexture> yTex = texture_from_cv(
            cache, pix, MTLPixelFormatR8Unorm, 0, width, height, error);
        id<MTLTexture> uvTex = texture_from_cv(
            cache,
            pix,
            MTLPixelFormatRG8Unorm,
            1,
            width / 2,
            height / 2,
            error);
        if (!yTex || !uvTex) {
            [enc endEncoding];
            return false;
        }
        [enc setComputePipelineState:g_nv12_pso];
        [enc setTexture:yTex atIndex:0];
        [enc setTexture:uvTex atIndex:1];
        [enc setTexture:dst atIndex:2];
        [enc dispatchThreads:grid threadsPerThreadgroup:tg];
    } else {
        [enc endEncoding];
        if (error) {
            *error = "Unsupported CVPixelBuffer format for Metal import";
        }
        return false;
    }

    [enc endEncoding];
    [cmd commit];
    [cmd waitUntilCompleted];
    if (cmd.error) {
        if (error) {
            *error = std::string(cmd.error.localizedDescription.UTF8String);
        }
        return false;
    }
    CVMetalTextureCacheFlush(cache, 0);
    return true;
}

bool fc_metal_wrap_cvpixelbuffer_planes(
    HwMetalImportContext* ctx,
    void* cv_pixel_buffer,
    int width,
    int height,
    void** out_plane0_mtl_texture,
    void** out_plane1_mtl_texture,
    int* out_sample_mode,
    std::string* error)
{
    if (out_plane0_mtl_texture) {
        *out_plane0_mtl_texture = nullptr;
    }
    if (out_plane1_mtl_texture) {
        *out_plane1_mtl_texture = nullptr;
    }
    if (out_sample_mode) {
        *out_sample_mode = 0;
    }
    if (!ctx || !ctx->cv_metal_texture_cache || !cv_pixel_buffer || width <= 0 || height <= 0
        || !out_plane0_mtl_texture || !out_sample_mode) {
        if (error) {
            *error = "invalid Metal wrap arguments";
        }
        return false;
    }

    CVPixelBufferRef pix = static_cast<CVPixelBufferRef>(cv_pixel_buffer);
    CVMetalTextureCacheRef cache =
        static_cast<CVMetalTextureCacheRef>(ctx->cv_metal_texture_cache);
    const OSType pix_fmt = CVPixelBufferGetPixelFormatType(pix);

    if (pix_fmt == kCVPixelFormatType_32BGRA) {
        id<MTLTexture> src = texture_from_cv(
            cache, pix, MTLPixelFormatBGRA8Unorm, 0, width, height, error);
        if (!src) {
            return false;
        }
        *out_plane0_mtl_texture = (__bridge void*)src;
        *out_sample_mode = 2;
        return true;
    }
    if (pix_fmt == kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange
        || pix_fmt == kCVPixelFormatType_420YpCbCr8BiPlanarFullRange) {
        id<MTLTexture> yTex = texture_from_cv(
            cache, pix, MTLPixelFormatR8Unorm, 0, width, height, error);
        id<MTLTexture> uvTex = texture_from_cv(
            cache,
            pix,
            MTLPixelFormatRG8Unorm,
            1,
            width / 2,
            height / 2,
            error);
        if (!yTex || !uvTex) {
            return false;
        }
        *out_plane0_mtl_texture = (__bridge void*)yTex;
        if (out_plane1_mtl_texture) {
            *out_plane1_mtl_texture = (__bridge void*)uvTex;
        }
        *out_sample_mode = 1;
        return true;
    }
    if (error) {
        *error = "Unsupported CVPixelBuffer format for Metal direct wrap";
    }
    return false;
}
