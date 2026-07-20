#include "hw_texture_import.h"

#if defined(__linux__)

#include "shaders/nv12_to_rgba16f_spv.h"

#include <vulkan/vulkan.h>

extern "C" {
#include <libavutil/frame.h>
#include <libavutil/hwcontext_drm.h>
#include <libavutil/pixfmt.h>
}

#include <unistd.h>

#include <algorithm>
#include <cstring>
#include <string>
#include <vector>

namespace {

constexpr uint32_t fourcc(char a, char b, char c, char d)
{
    return uint32_t(uint8_t(a)) | (uint32_t(uint8_t(b)) << 8)
        | (uint32_t(uint8_t(c)) << 16) | (uint32_t(uint8_t(d)) << 24);
}

constexpr uint32_t kDrmFormatNv12 = fourcc('N', 'V', '1', '2');
constexpr uint32_t kDrmFormatP010 = fourcc('P', '0', '1', '0');
constexpr uint64_t kDrmFormatModLinear = 0;

#ifndef VK_EXT_EXTERNAL_MEMORY_DMA_BUF_EXTENSION_NAME
#define VK_EXT_EXTERNAL_MEMORY_DMA_BUF_EXTENSION_NAME "VK_EXT_external_memory_dma_buf"
#endif

struct ImportedPlane {
    VkImage image = VK_NULL_HANDLE;
    VkDeviceMemory memory = VK_NULL_HANDLE; // owned if owns_memory
    VkImageView view = VK_NULL_HANDLE;
    bool owns_memory = true;
};

struct VulkanImportImpl {
    VkInstance instance = VK_NULL_HANDLE;
    VkPhysicalDevice phys = VK_NULL_HANDLE;
    VkDevice device = VK_NULL_HANDLE;
    VkQueue queue = VK_NULL_HANDLE;
    uint32_t queue_family = 0;

    VkDescriptorSetLayout set_layout = VK_NULL_HANDLE;
    VkPipelineLayout pipeline_layout = VK_NULL_HANDLE;
    VkPipeline pipeline = VK_NULL_HANDLE;
    VkSampler sampler = VK_NULL_HANDLE;
    VkDescriptorPool desc_pool = VK_NULL_HANDLE;
    VkCommandPool cmd_pool = VK_NULL_HANDLE;
    VkFence fence = VK_NULL_HANDLE;

    bool has_dma_buf = false;
    bool has_modifier = false;

    PFN_vkGetMemoryFdPropertiesKHR GetMemoryFdPropertiesKHR = nullptr;
};

bool set_error(std::string* error, const char* msg)
{
    if (error) {
        *error = msg;
    }
    return false;
}

uint32_t find_memory_type(
    VkPhysicalDevice phys,
    uint32_t type_bits,
    VkMemoryPropertyFlags /*props*/)
{
    VkPhysicalDeviceMemoryProperties mem_props{};
    vkGetPhysicalDeviceMemoryProperties(phys, &mem_props);
    for (uint32_t i = 0; i < mem_props.memoryTypeCount; ++i) {
        if ((type_bits & (1u << i)) != 0) {
            return i;
        }
    }
    return UINT32_MAX;
}

bool create_compute_pipeline(VulkanImportImpl* impl, std::string* error)
{
    VkShaderModuleCreateInfo smci{VK_STRUCTURE_TYPE_SHADER_MODULE_CREATE_INFO};
    smci.codeSize = fc_vk_shaders::kNv12ToRgba16fSpvWordCount * sizeof(uint32_t);
    smci.pCode = fc_vk_shaders::kNv12ToRgba16fSpv;
    VkShaderModule module = VK_NULL_HANDLE;
    if (vkCreateShaderModule(impl->device, &smci, nullptr, &module) != VK_SUCCESS) {
        return set_error(error, "vkCreateShaderModule failed");
    }

    VkDescriptorSetLayoutBinding bindings[3]{};
    bindings[0].binding = 0;
    bindings[0].descriptorType = VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER;
    bindings[0].descriptorCount = 1;
    bindings[0].stageFlags = VK_SHADER_STAGE_COMPUTE_BIT;
    bindings[1] = bindings[0];
    bindings[1].binding = 1;
    bindings[2].binding = 2;
    bindings[2].descriptorType = VK_DESCRIPTOR_TYPE_STORAGE_IMAGE;
    bindings[2].descriptorCount = 1;
    bindings[2].stageFlags = VK_SHADER_STAGE_COMPUTE_BIT;

    VkDescriptorSetLayoutCreateInfo dslci{VK_STRUCTURE_TYPE_DESCRIPTOR_SET_LAYOUT_CREATE_INFO};
    dslci.bindingCount = 3;
    dslci.pBindings = bindings;
    if (vkCreateDescriptorSetLayout(impl->device, &dslci, nullptr, &impl->set_layout)
        != VK_SUCCESS) {
        vkDestroyShaderModule(impl->device, module, nullptr);
        return set_error(error, "vkCreateDescriptorSetLayout failed");
    }

    VkPipelineLayoutCreateInfo plci{VK_STRUCTURE_TYPE_PIPELINE_LAYOUT_CREATE_INFO};
    plci.setLayoutCount = 1;
    plci.pSetLayouts = &impl->set_layout;
    if (vkCreatePipelineLayout(impl->device, &plci, nullptr, &impl->pipeline_layout)
        != VK_SUCCESS) {
        vkDestroyShaderModule(impl->device, module, nullptr);
        return set_error(error, "vkCreatePipelineLayout failed");
    }

    VkComputePipelineCreateInfo pci{VK_STRUCTURE_TYPE_COMPUTE_PIPELINE_CREATE_INFO};
    pci.stage.sType = VK_STRUCTURE_TYPE_PIPELINE_SHADER_STAGE_CREATE_INFO;
    pci.stage.stage = VK_SHADER_STAGE_COMPUTE_BIT;
    pci.stage.module = module;
    pci.stage.pName = "main";
    pci.layout = impl->pipeline_layout;
    if (vkCreateComputePipelines(
            impl->device, VK_NULL_HANDLE, 1, &pci, nullptr, &impl->pipeline)
        != VK_SUCCESS) {
        vkDestroyShaderModule(impl->device, module, nullptr);
        return set_error(error, "vkCreateComputePipelines failed");
    }
    vkDestroyShaderModule(impl->device, module, nullptr);

    VkSamplerCreateInfo sci{VK_STRUCTURE_TYPE_SAMPLER_CREATE_INFO};
    sci.magFilter = VK_FILTER_NEAREST;
    sci.minFilter = VK_FILTER_NEAREST;
    sci.addressModeU = VK_SAMPLER_ADDRESS_MODE_CLAMP_TO_EDGE;
    sci.addressModeV = VK_SAMPLER_ADDRESS_MODE_CLAMP_TO_EDGE;
    sci.addressModeW = VK_SAMPLER_ADDRESS_MODE_CLAMP_TO_EDGE;
    if (vkCreateSampler(impl->device, &sci, nullptr, &impl->sampler) != VK_SUCCESS) {
        return set_error(error, "vkCreateSampler failed");
    }

    VkDescriptorPoolSize pool_sizes[2]{};
    pool_sizes[0].type = VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER;
    pool_sizes[0].descriptorCount = 2;
    pool_sizes[1].type = VK_DESCRIPTOR_TYPE_STORAGE_IMAGE;
    pool_sizes[1].descriptorCount = 1;
    VkDescriptorPoolCreateInfo dpci{VK_STRUCTURE_TYPE_DESCRIPTOR_POOL_CREATE_INFO};
    dpci.flags = VK_DESCRIPTOR_POOL_CREATE_FREE_DESCRIPTOR_SET_BIT;
    dpci.maxSets = 1;
    dpci.poolSizeCount = 2;
    dpci.pPoolSizes = pool_sizes;
    if (vkCreateDescriptorPool(impl->device, &dpci, nullptr, &impl->desc_pool) != VK_SUCCESS) {
        return set_error(error, "vkCreateDescriptorPool failed");
    }

    VkCommandPoolCreateInfo cpci{VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO};
    cpci.flags = VK_COMMAND_POOL_CREATE_RESET_COMMAND_BUFFER_BIT;
    cpci.queueFamilyIndex = impl->queue_family;
    if (vkCreateCommandPool(impl->device, &cpci, nullptr, &impl->cmd_pool) != VK_SUCCESS) {
        return set_error(error, "vkCreateCommandPool failed");
    }

    VkFenceCreateInfo fci{VK_STRUCTURE_TYPE_FENCE_CREATE_INFO};
    if (vkCreateFence(impl->device, &fci, nullptr, &impl->fence) != VK_SUCCESS) {
        return set_error(error, "vkCreateFence failed");
    }
    return true;
}

void destroy_plane(VkDevice device, ImportedPlane& plane)
{
    if (plane.view) {
        vkDestroyImageView(device, plane.view, nullptr);
        plane.view = VK_NULL_HANDLE;
    }
    if (plane.image) {
        vkDestroyImage(device, plane.image, nullptr);
        plane.image = VK_NULL_HANDLE;
    }
    if (plane.owns_memory && plane.memory) {
        vkFreeMemory(device, plane.memory, nullptr);
    }
    plane.memory = VK_NULL_HANDLE;
    plane.owns_memory = true;
}

bool import_object_memory(
    VulkanImportImpl* impl,
    const AVDRMObjectDescriptor& obj,
    VkDeviceMemory* out_memory,
    std::string* error)
{
    if (!out_memory || obj.fd < 0) {
        return set_error(error, "invalid DRM object");
    }
    const int dup_fd = ::dup(obj.fd);
    if (dup_fd < 0) {
        return set_error(error, "dup(dma_buf fd) failed");
    }

    VkMemoryFdPropertiesKHR fd_props{VK_STRUCTURE_TYPE_MEMORY_FD_PROPERTIES_KHR};
    uint32_t type_bits = ~0u;
    if (impl->GetMemoryFdPropertiesKHR
        && impl->GetMemoryFdPropertiesKHR(
               impl->device,
               VK_EXTERNAL_MEMORY_HANDLE_TYPE_DMA_BUF_BIT_EXT,
               dup_fd,
               &fd_props)
            == VK_SUCCESS) {
        type_bits = fd_props.memoryTypeBits;
    }
    const uint32_t mem_type = find_memory_type(impl->phys, type_bits, 0);
    if (mem_type == UINT32_MAX) {
        ::close(dup_fd);
        return set_error(error, "No compatible memory type for DMA-BUF");
    }

    VkImportMemoryFdInfoKHR import_info{VK_STRUCTURE_TYPE_IMPORT_MEMORY_FD_INFO_KHR};
    import_info.handleType = VK_EXTERNAL_MEMORY_HANDLE_TYPE_DMA_BUF_BIT_EXT;
    import_info.fd = dup_fd;

    VkMemoryAllocateInfo mai{VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO};
    mai.allocationSize = obj.size > 0 ? obj.size : 1;
    mai.memoryTypeIndex = mem_type;
    mai.pNext = &import_info;

    if (vkAllocateMemory(impl->device, &mai, nullptr, out_memory) != VK_SUCCESS) {
        ::close(dup_fd);
        return set_error(error, "vkAllocateMemory (import fd) failed");
    }
    return true;
}

bool create_plane_image(
    VulkanImportImpl* impl,
    VkDeviceMemory memory,
    uint64_t modifier,
    VkFormat format,
    uint32_t width,
    uint32_t height,
    uint64_t offset,
    uint32_t pitch,
    ImportedPlane* out,
    bool owns_memory,
    std::string* error)
{
    if (!out || !memory) {
        return set_error(error, "invalid plane image args");
    }

    VkExternalMemoryImageCreateInfo ext_img{VK_STRUCTURE_TYPE_EXTERNAL_MEMORY_IMAGE_CREATE_INFO};
    ext_img.handleTypes = VK_EXTERNAL_MEMORY_HANDLE_TYPE_DMA_BUF_BIT_EXT;

    VkImageDrmFormatModifierExplicitCreateInfoEXT mod_info{
        VK_STRUCTURE_TYPE_IMAGE_DRM_FORMAT_MODIFIER_EXPLICIT_CREATE_INFO_EXT};
    VkSubresourceLayout plane_layout{};
    plane_layout.offset = offset;
    plane_layout.rowPitch = pitch;
    mod_info.drmFormatModifier = modifier;
    mod_info.drmFormatModifierPlaneCount = 1;
    mod_info.pPlaneLayouts = &plane_layout;

    VkImageCreateInfo ici{VK_STRUCTURE_TYPE_IMAGE_CREATE_INFO};
    ici.imageType = VK_IMAGE_TYPE_2D;
    ici.format = format;
    ici.extent = {width, height, 1};
    ici.mipLevels = 1;
    ici.arrayLayers = 1;
    ici.samples = VK_SAMPLE_COUNT_1_BIT;
    ici.usage = VK_IMAGE_USAGE_SAMPLED_BIT;
    ici.sharingMode = VK_SHARING_MODE_EXCLUSIVE;
    ici.initialLayout = VK_IMAGE_LAYOUT_UNDEFINED;
    ici.pNext = &ext_img;

    if (impl->has_modifier && modifier != kDrmFormatModLinear) {
        ici.tiling = VK_IMAGE_TILING_DRM_FORMAT_MODIFIER_EXT;
        ext_img.pNext = &mod_info;
    } else if (modifier == kDrmFormatModLinear) {
        ici.tiling = VK_IMAGE_TILING_LINEAR;
    } else {
        return set_error(error, "Non-linear DRM modifier requires VK_EXT_image_drm_format_modifier");
    }

    VkImage image = VK_NULL_HANDLE;
    if (vkCreateImage(impl->device, &ici, nullptr, &image) != VK_SUCCESS) {
        return set_error(error, "vkCreateImage (DMA-BUF plane) failed");
    }

    // For LINEAR plane images, bind at plane offset; modifier-explicit embeds offset.
    const VkDeviceSize bind_offset =
        (ici.tiling == VK_IMAGE_TILING_LINEAR) ? static_cast<VkDeviceSize>(offset) : 0;
    if (vkBindImageMemory(impl->device, image, memory, bind_offset) != VK_SUCCESS) {
        vkDestroyImage(impl->device, image, nullptr);
        return set_error(error, "vkBindImageMemory failed");
    }

    VkImageViewCreateInfo ivci{VK_STRUCTURE_TYPE_IMAGE_VIEW_CREATE_INFO};
    ivci.image = image;
    ivci.viewType = VK_IMAGE_VIEW_TYPE_2D;
    ivci.format = format;
    ivci.subresourceRange.aspectMask = VK_IMAGE_ASPECT_COLOR_BIT;
    ivci.subresourceRange.levelCount = 1;
    ivci.subresourceRange.layerCount = 1;
    VkImageView view = VK_NULL_HANDLE;
    if (vkCreateImageView(impl->device, &ivci, nullptr, &view) != VK_SUCCESS) {
        vkDestroyImage(impl->device, image, nullptr);
        return set_error(error, "vkCreateImageView failed");
    }

    out->image = image;
    out->memory = memory;
    out->view = view;
    out->owns_memory = owns_memory;
    return true;
}

void image_barrier(
    VkCommandBuffer cmd,
    VkImage image,
    VkImageLayout old_layout,
    VkImageLayout new_layout,
    VkAccessFlags src_access,
    VkAccessFlags dst_access,
    VkPipelineStageFlags src_stage,
    VkPipelineStageFlags dst_stage)
{
    VkImageMemoryBarrier barrier{VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER};
    barrier.oldLayout = old_layout;
    barrier.newLayout = new_layout;
    barrier.srcQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED;
    barrier.dstQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED;
    barrier.image = image;
    barrier.subresourceRange.aspectMask = VK_IMAGE_ASPECT_COLOR_BIT;
    barrier.subresourceRange.levelCount = 1;
    barrier.subresourceRange.layerCount = 1;
    barrier.srcAccessMask = src_access;
    barrier.dstAccessMask = dst_access;
    vkCmdPipelineBarrier(cmd, src_stage, dst_stage, 0, 0, nullptr, 0, nullptr, 1, &barrier);
}

} // namespace

bool fc_vulkan_create_import_context(HwVulkanImportContext* ctx, std::string* error)
{
    if (!ctx || !ctx->instance || !ctx->phys_dev || !ctx->device || !ctx->queue) {
        return set_error(error, "null Vulkan device handles");
    }
    fc_vulkan_release_import_context(ctx);

    auto* impl = new VulkanImportImpl();
    impl->instance = static_cast<VkInstance>(ctx->instance);
    impl->phys = static_cast<VkPhysicalDevice>(ctx->phys_dev);
    impl->device = static_cast<VkDevice>(ctx->device);
    impl->queue = static_cast<VkQueue>(ctx->queue);
    impl->queue_family = ctx->queue_family;

    // Probe device extensions for DMA-BUF import.
    uint32_t ext_count = 0;
    vkEnumerateDeviceExtensionProperties(impl->phys, nullptr, &ext_count, nullptr);
    std::vector<VkExtensionProperties> exts(ext_count);
    if (ext_count > 0) {
        vkEnumerateDeviceExtensionProperties(impl->phys, nullptr, &ext_count, exts.data());
    }
    for (const auto& e : exts) {
        if (std::strcmp(e.extensionName, VK_KHR_EXTERNAL_MEMORY_FD_EXTENSION_NAME) == 0
            || std::strcmp(e.extensionName, VK_EXT_EXTERNAL_MEMORY_DMA_BUF_EXTENSION_NAME) == 0) {
            impl->has_dma_buf = true;
        }
        if (std::strcmp(e.extensionName, VK_EXT_IMAGE_DRM_FORMAT_MODIFIER_EXTENSION_NAME) == 0) {
            impl->has_modifier = true;
        }
    }
    // EXTERNAL_MEMORY_FD is required; DMA_BUF is typically present with it on Linux.
    impl->GetMemoryFdPropertiesKHR = reinterpret_cast<PFN_vkGetMemoryFdPropertiesKHR>(
        vkGetDeviceProcAddr(impl->device, "vkGetMemoryFdPropertiesKHR"));
    if (!impl->GetMemoryFdPropertiesKHR) {
        impl->GetMemoryFdPropertiesKHR = reinterpret_cast<PFN_vkGetMemoryFdPropertiesKHR>(
            vkGetInstanceProcAddr(impl->instance, "vkGetMemoryFdPropertiesKHR"));
    }
    if (!impl->has_dma_buf && !impl->GetMemoryFdPropertiesKHR) {
        delete impl;
        return set_error(error, "Vulkan device missing DMA-BUF external memory support");
    }
    impl->has_dma_buf = true;

    if (!create_compute_pipeline(impl, error)) {
        ctx->impl = impl;
        fc_vulkan_release_import_context(ctx);
        return false;
    }

    ctx->impl = impl;
    return true;
}

void fc_vulkan_release_import_context(HwVulkanImportContext* ctx)
{
    if (!ctx || !ctx->impl) {
        return;
    }
    auto* impl = static_cast<VulkanImportImpl*>(ctx->impl);
    if (impl->device) {
        if (impl->fence) {
            vkDestroyFence(impl->device, impl->fence, nullptr);
        }
        if (impl->cmd_pool) {
            vkDestroyCommandPool(impl->device, impl->cmd_pool, nullptr);
        }
        if (impl->desc_pool) {
            vkDestroyDescriptorPool(impl->device, impl->desc_pool, nullptr);
        }
        if (impl->sampler) {
            vkDestroySampler(impl->device, impl->sampler, nullptr);
        }
        if (impl->pipeline) {
            vkDestroyPipeline(impl->device, impl->pipeline, nullptr);
        }
        if (impl->pipeline_layout) {
            vkDestroyPipelineLayout(impl->device, impl->pipeline_layout, nullptr);
        }
        if (impl->set_layout) {
            vkDestroyDescriptorSetLayout(impl->device, impl->set_layout, nullptr);
        }
    }
    delete impl;
    ctx->impl = nullptr;
}

bool fc_vulkan_import_drm_prime_to_rgba16f(
    HwVulkanImportContext* ctx,
    void* drm_prime_avframe,
    void* dst_vk_image_rgba16f,
    int width,
    int height,
    std::string* error)
{
    if (!ctx || !ctx->impl || !drm_prime_avframe || !dst_vk_image_rgba16f
        || width <= 0 || height <= 0) {
        return set_error(error, "invalid Vulkan import arguments");
    }

    auto* impl = static_cast<VulkanImportImpl*>(ctx->impl);
    auto* frame = static_cast<AVFrame*>(drm_prime_avframe);
    if (frame->format != AV_PIX_FMT_DRM_PRIME || !frame->data[0]) {
        return set_error(error, "AVFrame is not DRM_PRIME");
    }

    const auto* desc = reinterpret_cast<const AVDRMFrameDescriptor*>(frame->data[0]);
    if (!desc || desc->nb_layers < 1 || desc->layers[0].nb_planes < 2) {
        return set_error(error, "DRM descriptor missing NV12/P010 planes");
    }

    const AVDRMLayerDescriptor& layer = desc->layers[0];
    const bool is_nv12 = (layer.format == kDrmFormatNv12);
    const bool is_p010 = (layer.format == kDrmFormatP010);
    if (!is_nv12 && !is_p010) {
        return set_error(error, "Unsupported DRM format (need NV12 or P010)");
    }

    const AVDRMPlaneDescriptor& y_plane = layer.planes[0];
    const AVDRMPlaneDescriptor& uv_plane = layer.planes[1];
    if (y_plane.object_index < 0 || y_plane.object_index >= desc->nb_objects
        || uv_plane.object_index < 0 || uv_plane.object_index >= desc->nb_objects) {
        return set_error(error, "DRM plane object_index out of range");
    }

    const AVDRMObjectDescriptor& y_obj = desc->objects[y_plane.object_index];
    const AVDRMObjectDescriptor& uv_obj = desc->objects[uv_plane.object_index];
    const uint64_t modifier = y_obj.format_modifier;
    if (modifier != kDrmFormatModLinear && !impl->has_modifier) {
        return set_error(error, "Non-linear DRM modifier requires VK_EXT_image_drm_format_modifier");
    }

    const VkFormat y_fmt = is_nv12 ? VK_FORMAT_R8_UNORM : VK_FORMAT_R16_UNORM;
    const VkFormat uv_fmt = is_nv12 ? VK_FORMAT_R8G8_UNORM : VK_FORMAT_R16G16_UNORM;

    // Import each unique DRM object once (NV12 usually shares one fd for Y+UV).
    VkDeviceMemory y_mem = VK_NULL_HANDLE;
    VkDeviceMemory uv_mem = VK_NULL_HANDLE;
    if (!import_object_memory(impl, y_obj, &y_mem, error)) {
        return false;
    }
    if (uv_plane.object_index == y_plane.object_index) {
        uv_mem = y_mem;
    } else if (!import_object_memory(impl, uv_obj, &uv_mem, error)) {
        vkFreeMemory(impl->device, y_mem, nullptr);
        return false;
    }

    ImportedPlane y_imp{};
    ImportedPlane uv_imp{};
    if (!create_plane_image(
            impl,
            y_mem,
            modifier,
            y_fmt,
            static_cast<uint32_t>(width),
            static_cast<uint32_t>(height),
            y_plane.offset,
            y_plane.pitch,
            &y_imp,
            /*owns_memory=*/true,
            error)) {
        vkFreeMemory(impl->device, y_mem, nullptr);
        if (uv_mem != y_mem) {
            vkFreeMemory(impl->device, uv_mem, nullptr);
        }
        return false;
    }
    if (!create_plane_image(
            impl,
            uv_mem,
            modifier,
            uv_fmt,
            static_cast<uint32_t>(width / 2),
            static_cast<uint32_t>(height / 2),
            uv_plane.offset,
            uv_plane.pitch,
            &uv_imp,
            /*owns_memory=*/(uv_mem != y_mem),
            error)) {
        destroy_plane(impl->device, y_imp);
        if (uv_mem != y_mem) {
            vkFreeMemory(impl->device, uv_mem, nullptr);
        }
        return false;
    }

    auto* dst_image = static_cast<VkImage>(dst_vk_image_rgba16f);

    VkImageViewCreateInfo dst_view_ci{VK_STRUCTURE_TYPE_IMAGE_VIEW_CREATE_INFO};
    dst_view_ci.image = dst_image;
    dst_view_ci.viewType = VK_IMAGE_VIEW_TYPE_2D;
    dst_view_ci.format = VK_FORMAT_R16G16B16A16_SFLOAT;
    dst_view_ci.subresourceRange.aspectMask = VK_IMAGE_ASPECT_COLOR_BIT;
    dst_view_ci.subresourceRange.levelCount = 1;
    dst_view_ci.subresourceRange.layerCount = 1;
    VkImageView dst_view = VK_NULL_HANDLE;
    if (vkCreateImageView(impl->device, &dst_view_ci, nullptr, &dst_view) != VK_SUCCESS) {
        destroy_plane(impl->device, y_imp);
        destroy_plane(impl->device, uv_imp);
        return set_error(error, "vkCreateImageView (RGBA16F) failed");
    }

    VkDescriptorSetAllocateInfo dsai{VK_STRUCTURE_TYPE_DESCRIPTOR_SET_ALLOCATE_INFO};
    dsai.descriptorPool = impl->desc_pool;
    dsai.descriptorSetCount = 1;
    dsai.pSetLayouts = &impl->set_layout;
    VkDescriptorSet set = VK_NULL_HANDLE;
    if (vkAllocateDescriptorSets(impl->device, &dsai, &set) != VK_SUCCESS) {
        vkDestroyImageView(impl->device, dst_view, nullptr);
        destroy_plane(impl->device, y_imp);
        destroy_plane(impl->device, uv_imp);
        return set_error(error, "vkAllocateDescriptorSets failed");
    }

    VkDescriptorImageInfo y_info{};
    y_info.sampler = impl->sampler;
    y_info.imageView = y_imp.view;
    y_info.imageLayout = VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL;
    VkDescriptorImageInfo uv_info = y_info;
    uv_info.imageView = uv_imp.view;
    VkDescriptorImageInfo dst_info{};
    dst_info.imageView = dst_view;
    dst_info.imageLayout = VK_IMAGE_LAYOUT_GENERAL;

    VkWriteDescriptorSet writes[3]{};
    writes[0].sType = VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET;
    writes[0].dstSet = set;
    writes[0].dstBinding = 0;
    writes[0].descriptorCount = 1;
    writes[0].descriptorType = VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER;
    writes[0].pImageInfo = &y_info;
    writes[1] = writes[0];
    writes[1].dstBinding = 1;
    writes[1].pImageInfo = &uv_info;
    writes[2].sType = VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET;
    writes[2].dstSet = set;
    writes[2].dstBinding = 2;
    writes[2].descriptorCount = 1;
    writes[2].descriptorType = VK_DESCRIPTOR_TYPE_STORAGE_IMAGE;
    writes[2].pImageInfo = &dst_info;
    vkUpdateDescriptorSets(impl->device, 3, writes, 0, nullptr);

    VkCommandBufferAllocateInfo cbai{VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO};
    cbai.commandPool = impl->cmd_pool;
    cbai.level = VK_COMMAND_BUFFER_LEVEL_PRIMARY;
    cbai.commandBufferCount = 1;
    VkCommandBuffer cmd = VK_NULL_HANDLE;
    if (vkAllocateCommandBuffers(impl->device, &cbai, &cmd) != VK_SUCCESS) {
        vkFreeDescriptorSets(impl->device, impl->desc_pool, 1, &set);
        vkDestroyImageView(impl->device, dst_view, nullptr);
        destroy_plane(impl->device, y_imp);
        destroy_plane(impl->device, uv_imp);
        return set_error(error, "vkAllocateCommandBuffers failed");
    }

    VkCommandBufferBeginInfo begin{VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO};
    begin.flags = VK_COMMAND_BUFFER_USAGE_ONE_TIME_SUBMIT_BIT;
    vkBeginCommandBuffer(cmd, &begin);

    image_barrier(
        cmd,
        y_imp.image,
        VK_IMAGE_LAYOUT_UNDEFINED,
        VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL,
        0,
        VK_ACCESS_SHADER_READ_BIT,
        VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT,
        VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT);
    image_barrier(
        cmd,
        uv_imp.image,
        VK_IMAGE_LAYOUT_UNDEFINED,
        VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL,
        0,
        VK_ACCESS_SHADER_READ_BIT,
        VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT,
        VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT);
    image_barrier(
        cmd,
        dst_image,
        VK_IMAGE_LAYOUT_UNDEFINED,
        VK_IMAGE_LAYOUT_GENERAL,
        0,
        VK_ACCESS_SHADER_WRITE_BIT,
        VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT,
        VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT);

    vkCmdBindPipeline(cmd, VK_PIPELINE_BIND_POINT_COMPUTE, impl->pipeline);
    vkCmdBindDescriptorSets(
        cmd, VK_PIPELINE_BIND_POINT_COMPUTE, impl->pipeline_layout, 0, 1, &set, 0, nullptr);
    const uint32_t gx = (static_cast<uint32_t>(width) + 15u) / 16u;
    const uint32_t gy = (static_cast<uint32_t>(height) + 15u) / 16u;
    vkCmdDispatch(cmd, gx, gy, 1);

    image_barrier(
        cmd,
        dst_image,
        VK_IMAGE_LAYOUT_GENERAL,
        VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL,
        VK_ACCESS_SHADER_WRITE_BIT,
        VK_ACCESS_SHADER_READ_BIT,
        VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT,
        VK_PIPELINE_STAGE_FRAGMENT_SHADER_BIT);

    vkEndCommandBuffer(cmd);

    vkResetFences(impl->device, 1, &impl->fence);
    VkSubmitInfo submit{VK_STRUCTURE_TYPE_SUBMIT_INFO};
    submit.commandBufferCount = 1;
    submit.pCommandBuffers = &cmd;
    if (vkQueueSubmit(impl->queue, 1, &submit, impl->fence) != VK_SUCCESS) {
        vkFreeCommandBuffers(impl->device, impl->cmd_pool, 1, &cmd);
        vkFreeDescriptorSets(impl->device, impl->desc_pool, 1, &set);
        vkDestroyImageView(impl->device, dst_view, nullptr);
        destroy_plane(impl->device, y_imp);
        destroy_plane(impl->device, uv_imp);
        return set_error(error, "vkQueueSubmit failed");
    }
    vkWaitForFences(impl->device, 1, &impl->fence, VK_TRUE, UINT64_MAX);

    vkFreeCommandBuffers(impl->device, impl->cmd_pool, 1, &cmd);
    vkFreeDescriptorSets(impl->device, impl->desc_pool, 1, &set);
    vkDestroyImageView(impl->device, dst_view, nullptr);
    destroy_plane(impl->device, y_imp);
    destroy_plane(impl->device, uv_imp);
    return true;
}

#endif // __linux__
