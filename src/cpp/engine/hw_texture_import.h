#pragma once

#include <cstdint>
#include <string>

struct HwMetalImportContext {
    void* mtl_device = nullptr;             // id<MTLDevice>
    void* mtl_command_queue = nullptr;      // id<MTLCommandQueue> (optional)
    void* cv_metal_texture_cache = nullptr; // CVMetalTextureCacheRef (owned by caller)
};

struct HwD3D11ImportContext {
    void* device = nullptr;  // ID3D11Device*
    void* context = nullptr; // ID3D11DeviceContext*
    void* impl = nullptr;    // opaque pipeline state (owned via create/release)
};

struct HwVulkanImportContext {
    void* instance = nullptr;   // VkInstance
    void* phys_dev = nullptr;   // VkPhysicalDevice
    void* device = nullptr;     // VkDevice
    void* queue = nullptr;      // VkQueue
    uint32_t queue_family = 0;
    void* impl = nullptr;       // opaque pipeline state
};

#if defined(__APPLE__)

/// Create a CVMetalTextureCache bound to ``mtl_device``. Caller releases via
/// ``fc_metal_release_cv_texture_cache``.
void* fc_metal_create_cv_texture_cache(void* mtl_device, std::string* error);
void fc_metal_release_cv_texture_cache(void* cv_metal_texture_cache);

/// Blit/convert CVPixelBuffer into an existing RGBA16Float MTLTexture (same device).
bool fc_metal_import_cvpixelbuffer_to_rgba16f(
    HwMetalImportContext* ctx,
    void* cv_pixel_buffer,
    void* dst_mtl_texture_rgba16f,
    int width,
    int height,
    std::string* error);

/// Wrap CVPixelBuffer planes as MTLTextures without RGBA16F convert.
/// On success: *out_plane0 / *out_plane1 are id<MTLTexture> (bridge cast),
/// *out_sample_mode is 1 (NV12) or 2 (BGRA). Caller must CFRetain the pixel
/// buffer for the lifetime of the wrapped textures. plane1 is null for BGRA.
bool fc_metal_wrap_cvpixelbuffer_planes(
    HwMetalImportContext* ctx,
    void* cv_pixel_buffer,
    int width,
    int height,
    void** out_plane0_mtl_texture,
    void** out_plane1_mtl_texture,
    int* out_sample_mode,
    std::string* error);

#else

inline void* fc_metal_create_cv_texture_cache(void*, std::string* error)
{
    if (error) {
        *error = "Metal HW import not available on this platform";
    }
    return nullptr;
}

inline void fc_metal_release_cv_texture_cache(void*) {}

inline bool fc_metal_import_cvpixelbuffer_to_rgba16f(
    HwMetalImportContext*,
    void*,
    void*,
    int,
    int,
    std::string* error)
{
    if (error) {
        *error = "Metal HW import not available on this platform";
    }
    return false;
}

inline bool fc_metal_wrap_cvpixelbuffer_planes(
    HwMetalImportContext*,
    void*,
    int,
    int,
    void**,
    void**,
    int*,
    std::string* error)
{
    if (error) {
        *error = "Metal HW import not available on this platform";
    }
    return false;
}

#endif

#if defined(_WIN32)

/// Publish QRhi's D3D11 device for FFmpeg D3D11VA to share (AddRef'd).
void fc_d3d11_set_shared_device(void* device, void* context);
void fc_d3d11_clear_shared_device();
bool fc_d3d11_get_shared_device(void** device_out, void** context_out);

/// Create import pipelines (NV12/P010 compute). Caller releases via release.
bool fc_d3d11_create_import_context(HwD3D11ImportContext* ctx, std::string* error);
void fc_d3d11_release_import_context(HwD3D11ImportContext* ctx);

/// Copy decoder array-slice → SRV texture, convert to RGBA16F UAV (same device).
bool fc_d3d11_import_texture_to_rgba16f(
    HwD3D11ImportContext* ctx,
    void* src_texture_2d,
    int array_index,
    void* dst_texture_rgba16f,
    int width,
    int height,
    std::string* error);

void fc_d3d11_get_import_pool_stats(
    HwD3D11ImportContext* ctx,
    uint64_t* creates_out,
    uint64_t* reuses_out);
void fc_d3d11_set_import_pool_enabled(HwD3D11ImportContext* ctx, bool enabled);

#else

inline void fc_d3d11_set_shared_device(void*, void*) {}
inline void fc_d3d11_clear_shared_device() {}
inline bool fc_d3d11_get_shared_device(void**, void**) { return false; }

inline bool fc_d3d11_create_import_context(HwD3D11ImportContext*, std::string* error)
{
    if (error) {
        *error = "D3D11 HW import not available on this platform";
    }
    return false;
}

inline void fc_d3d11_release_import_context(HwD3D11ImportContext*) {}

inline bool fc_d3d11_import_texture_to_rgba16f(
    HwD3D11ImportContext*,
    void*,
    int,
    void*,
    int,
    int,
    std::string* error)
{
    if (error) {
        *error = "D3D11 HW import not available on this platform";
    }
    return false;
}

inline void fc_d3d11_get_import_pool_stats(HwD3D11ImportContext*, uint64_t* c, uint64_t* r)
{
    if (c) {
        *c = 0;
    }
    if (r) {
        *r = 0;
    }
}
inline void fc_d3d11_set_import_pool_enabled(HwD3D11ImportContext*, bool) {}

#endif

#if defined(__linux__)

/// Create Vulkan import pipelines (DMA-BUF NV12/P010 → RGBA16F compute).
bool fc_vulkan_create_import_context(HwVulkanImportContext* ctx, std::string* error);
void fc_vulkan_release_import_context(HwVulkanImportContext* ctx);

/// Import AVFrame* (AV_PIX_FMT_DRM_PRIME) into an existing RGBA16F VkImage.
bool fc_vulkan_import_drm_prime_to_rgba16f(
    HwVulkanImportContext* ctx,
    void* drm_prime_avframe,
    void* dst_vk_image_rgba16f,
    int width,
    int height,
    std::string* error);

#else

inline bool fc_vulkan_create_import_context(HwVulkanImportContext*, std::string* error)
{
    if (error) {
        *error = "Vulkan HW import not available on this platform";
    }
    return false;
}

inline void fc_vulkan_release_import_context(HwVulkanImportContext*) {}

inline bool fc_vulkan_import_drm_prime_to_rgba16f(
    HwVulkanImportContext*,
    void*,
    void*,
    int,
    int,
    std::string* error)
{
    if (error) {
        *error = "Vulkan HW import not available on this platform";
    }
    return false;
}

#endif
