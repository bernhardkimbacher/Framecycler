#pragma once

#include <QWindow>
#include <QSize>
#include <QColor>
#include <rhi/qrhi.h>
#include <rhi/qshader.h>
#include <rhi/qshaderbaker.h>

#include <thread>
#include <mutex>
#include <atomic>
#include <vector>
#include <string>
#include <unordered_map>
#include <array>
#include <memory>

#include "gpu_texture_cache.h"

class CacheManager;

// Structure to pass frame render slot specifications from Python/Cache
struct FrameSlotSpec {
    int source_index = 0;
    int frame_index = -1;
    int width = 0;
    int height = 0;
    int channels = 4;
    int upload_token = 0;
    const uint16_t* pixel_data = nullptr; // Cache pointer (read under lock)
    size_t data_size = 0;
};

struct TileSpec {
    int source_index = -1;
    float scale_x = 1.0f;
    float scale_y = 1.0f;
    float offset_x = 0.0f;
    float offset_y = 0.0f;
};

// Thread-safe parameters for rendering a frame
struct RenderParams {
    int compare_mode = 0;
    int sequence_index = 0;
    float wipe_pos = 0.5f;
    int channel_mask = 0;
    float scale_x = 1.0f;
    float scale_y = 1.0f;
    float pan_x = 0.0f;
    float pan_y = 0.0f;
    
    std::vector<FrameSlotSpec> slots;
    std::vector<TileSpec> tiles;
};

struct GradingParams {
    std::unordered_map<std::string, float> floats;
    std::unordered_map<std::string, std::array<float, 3>> vec3s;
};

class RhiRenderer {
public:
    RhiRenderer();
    ~RhiRenderer();

    // Lifecycle (called from GUI/Python)
    bool initialize(uintptr_t window_ptr);
    void shutdown();

    // Setters for rendering parameters (thread-safe double buffering)
    void update_render_params(const RenderParams& params);
    void set_grading_uniform(const std::string& name, float value);
    void set_grading_uniform_vec3(const std::string& name, float x, float y, float z);
    void clear_grading_uniforms();
    
    // Cache registration for direct native lookup
    void register_cache(int source_index, CacheManager* cache);
    
    // Shader/OCIO updates
    void set_shader_sources(const std::string& pipeline_key, const std::string& vert_src, const std::string& frag_src);
    void upload_ocio_lut_3d(int index, int size, const std::vector<float>& data);
    void clear_ocio_luts();

    // Window event listeners (called from RhiViewportWindow on GUI thread)
    void set_exposed(bool exposed);
    void set_pending_size(int width, int height);

    void request_redraw();
    void sync_and_render();

    void set_display_cache_limit_gb(double limit_gb);
    void clear_display_cache();
    void set_source_playhead(int source_index, int playhead, int direction, int in_point, int out_point);
    void invalidate_display_cache_source(int source_index);
    GpuTextureCache::Stats get_display_cache_stats() const;

    struct DebugStats {
        int begin_frame_ok = 0;
        int begin_frame_fail = 0;
        int frames_drawn = 0;
        int frames_cleared_only = 0;
        int cache_hits = 0;
        int cache_misses = 0;
        bool rhi_ready = false;
        bool shaders_valid = false;
        bool pipeline_ready = false;
        bool tex_a_ready = false;
        int last_tex_w = 0;
        int last_tex_h = 0;
        int swap_w = 0;
        int swap_h = 0;
        double last_render_ms = 0.0;
        double last_upload_ms = 0.0;
        double last_draw_ms = 0.0;
        int last_upload_bytes = 0;
        int last_upload_count = 0;
        int gpu_cache_hits = 0;
        int gpu_cache_misses = 0;
    };
    DebugStats get_debug_stats() const;

private:
    struct StagingBuffer {
        std::vector<uint16_t> data;
        int width = 0;
        int height = 0;
        int channels = 0;
        int upload_token = 0;
        int frame_index = -1;
    };

    struct OcioLut3D {
        QRhiTexture* texture = nullptr;
        int size = 0;
        std::vector<float> rgba_data;
        bool dirty = false;
    };

    struct TextureState {
        QRhiTexture* texture = nullptr;
        int last_w = 0;
        int last_h = 0;
        int last_channels = 0;
        int last_upload_token = -1;
    };

    struct OcioUniformMember {
        std::string name;
        int offset = 0;
        int size = 0;
        bool is_vec3 = false;
    };

    struct OcioUboLayout {
        std::unordered_map<std::string, OcioUniformMember> members;
        int size = 0;
        int binding = -1;
    };

    // Thread Loop (GUI-thread synchronous render; no background thread)
    void render_frame();
    void update_rhi_resources(QRhiResourceUpdateBatch* batch);
    void build_pipeline(QRhiRenderPassDescriptor* rpDesc, bool force_rebuild = false);
    bool bake_shaders(const std::string& vert_src, const std::string& frag_src);
    void parse_ocio_ubo_layout(const std::string& fragment_source);
    std::vector<char> pack_ocio_ubo();

    // Texture helpers
    void upload_texture(TextureState& state, const FrameSlotSpec& spec, QRhiResourceUpdateBatch* batch);
    void upload_texture_3d(OcioLut3D& lut, QRhiResourceUpdateBatch* batch);
    bool resolve_display_texture(
        int source_index,
        CacheManager* cpu_cache,
        const FrameSlotSpec& spec,
        TextureState& bind_state,
        QRhiResourceUpdateBatch* batch,
        bool& pipeline_dirty);
    void _release_gpu_resources();

    // Threading / state sync (params arrive from Python on GUI thread)
    std::mutex _mutex;
    std::atomic<bool> _exposed{false};
    std::atomic<bool> _resize_pending{false};
    QSize _pending_size;
    QWindow* _window = nullptr;
    DebugStats _debug_stats;

    // Double-buffered inputs
    RenderParams _pending_render_params;
    GradingParams _pending_grading_params;
    bool _render_params_dirty = false;
    bool _grading_params_dirty = false;
    bool _shaders_dirty = false;
    std::string _pending_frag_src_for_layout;

    // Active rendering state (only accessed by render thread)
    RenderParams _active_render_params;
    GradingParams _active_grading_params;

    // QRhi Resources
    QRhi* _rhi = nullptr;
    QRhiSwapChain* _swapChain = nullptr;
    QRhiRenderPassDescriptor* _fallbackRpDesc = nullptr;
    QRhiBuffer* _vertexBuffer = nullptr;
    QRhiBuffer* _perFrameUbo = nullptr;
    QRhiBuffer* _ocioUbo = nullptr;
    QRhiSampler* _sampler = nullptr;
    QRhiSampler* _lutSampler = nullptr;
    QRhiShaderResourceBindings* _srb = nullptr;
    QRhiGraphicsPipeline* _pipeline = nullptr;

    QShader _vertexShader;
    QShader _fragmentShader;
    std::string _cached_pipeline_key;
    
    // Pipelines for comparison modes
    TextureState _texAState;
    TextureState _texBState;
    std::vector<TextureState> _texturePool;
    std::vector<OcioLut3D> _ocioLuts3d;

    // Shader binding layouts
    OcioUboLayout _ocioUboLayout;

    // Staging ring
    std::vector<StagingBuffer> _stagingRing;
    size_t _stagingRingIndex = 0;

    std::unordered_map<int, CacheManager*> _caches;
    GpuTextureCache _displayCache;
    QRhiTexture* _last_bound_tex_a = nullptr;
    QRhiTexture* _last_bound_tex_b = nullptr;
};
