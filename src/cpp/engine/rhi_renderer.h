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
#include <condition_variable>
#include <vector>
#include <string>
#include <unordered_map>
#include <array>
#include <memory>
#include <optional>
#include <functional>
#include <cstdint>
#include <chrono>

#include "gpu_texture_cache.h"
#include "display_upload_queue.h"
#include "hw_texture_import.h"
#include "transport_clock.h"
#include "audio_engine.h"
#include "cache_manager.h"

#if !defined(Q_OS_MACOS) && !defined(Q_OS_WIN)
class QVulkanInstance;
#endif

// Structure to pass frame render slot specifications from Python/Cache
struct FrameSlotSpec {
    int source_index = 0;
    int frame_index = -1;
    int width = 0;
    int height = 0;
    int channels = 4;
    int upload_token = 0;
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
    int false_color_mode = 0;  // 0=off, 1=heatmap, 2=zebra
    float zebra_lo = 0.02f;
    float zebra_hi = 0.98f;
    /// Fit-relative zoom multiplier (1.0 = fit). Aspect-fit is computed on the
    /// render thread from live swapchain size + texture dims each present.
    float zoom = 1.0f;
    float pixel_aspect_ratio = 1.0f;
    float pan_x = 0.0f;
    float pan_y = 0.0f;

    std::vector<FrameSlotSpec> slots;
    std::vector<TileSpec> tiles;
};

struct GradingParams {
    std::unordered_map<std::string, float> floats;
    std::unordered_map<std::string, std::array<float, 3>> vec3s;
};

struct PerFrameUboData {
    float scale_x = 1.0f;
    float scale_y = 1.0f;
    float pan_x = 0.0f;
    float pan_y = 0.0f;
    int compare_mode = 0;
    float wipe_pos = 0.5f;
    int channel_mask = 0;
    int false_color_mode = 0;
    float zebra_lo = 0.02f;
    float zebra_hi = 0.98f;
    /// 0 = RGBA texA, 1 = NV12 (texA=Y, texB=UV), 2 = BGRA swizzle on texA.
    int sample_mode = 0;
    float pad1 = 0.0f;
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
    /// OCIO 1D LUTs are exposed as sampler2D (width x height, 1 or 4 channels).
    void upload_ocio_lut_2d(
        int index,
        int width,
        int height,
        int channels,
        const std::vector<float>& data);
    /// Ordered slot dimensions matching sampler_bindings ("2D" or "3D"), starting at binding 3.
    void set_ocio_lut_slot_dims(const std::vector<std::string>& dims);
    void clear_ocio_luts();
    std::string cached_pipeline_key() const;

    // Window event listeners (called from RhiViewportWindow on GUI thread)
    void set_exposed(bool exposed);
    /// Wake the render thread so it can sync swapchain to live surfacePixelSize().
    void notify_surface_changed();

    void request_redraw();
    void sync_and_render();

    void set_display_cache_limit_gb(double limit_gb);
    void clear_display_cache();
    void set_source_playhead(int source_index, int playhead, int direction, int in_point, int out_point);
    void invalidate_display_cache_source(int source_index);
    GpuTextureCache::Stats get_display_cache_stats() const;
    std::vector<int> get_display_cached_frames(int source_index);
    void set_upload_queue_policy(UploadQueuePolicy policy);
    UploadQueuePolicy upload_queue_policy() const;
    UploadQueueStats get_upload_queue_stats() const;
    bool is_fallback_null_backend() const { return _is_fallback_null_backend.load(); }

    /// Prefer Null RHI (set before initialize). Also auto-enabled for offscreen /
    /// FRAMECYCLER_FORCE_NULL_RHI when initialize() runs on the GUI thread.
    void set_force_null_backend(bool enabled);

    /// Viewer present format (SDR vs EDR / HDR10). Applied on the render thread.
    enum class ViewerOutputMode {
        Sdr = 0,
        EdrExtendedSrgbLinear = 1,
        Hdr10 = 2,
    };
    void set_viewer_output_mode(ViewerOutputMode mode);
    ViewerOutputMode viewer_output_mode() const; ///< Requested mode
    ViewerOutputMode actual_viewer_output_mode() const; ///< Last applied mode (may fall back to SDR)
    bool is_viewer_output_mode_supported(ViewerOutputMode mode) const;

    // Transport clock (C++-owned playback). Python pushes a program and reacts
    // to coalesced frame / segment-boundary callbacks.
    void set_transport_program(const TransportProgram& program);
    void transport_play();
    void transport_pause();
    void transport_seek(int global_frame, bool scrub_preview = false);
    int get_transport_frame() const;
    int get_transport_direction() const;
    bool is_transport_playing() const;

    /// Presentation-slave audio (bound to active movie path from Python).
    AudioEngine& audio_engine() { return _audio; }
    /// Bind movie audio. ``media_origin_frame`` is the file's decoder start frame
    /// (usually 0 for QT); media time = (decoder_frame - origin) / fps.
    void set_audio_media_path(const std::string& path, int media_origin_frame = 0);
    void set_audio_volume(float volume);
    void set_audio_muted(bool muted);
    void set_audio_scrub(bool enabled);
    void begin_audio_scrub();
    void end_audio_scrub();
    void set_audio_output_device(const std::string& device_id);
    std::string audio_output_device() const;
    std::string audio_last_error() const;
    bool audio_has_audio() const;
    std::vector<float> audio_peaks(int peaks_per_second = 300);
    static std::vector<AudioDeviceInfo> list_audio_output_devices();
    void set_frame_changed_callback(std::function<void(int frame, int direction)> cb);
    void set_segment_boundary_callback(std::function<void(int frame, int direction)> cb);
    /// Python ack after draining a coalesced frame notification.
    void ack_transport_frame_notify();
    /// Non-blocking drain of coalesced playhead notifies (GUI-thread poll).
    /// Returns true when a new frame is available since the last successful poll.
    bool poll_transport_frame_notify(int& frame_out, int& direction_out);
    bool poll_transport_boundary_notify(int& frame_out, int& direction_out);

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
        /// Jobs submitted into this present's resource batch (lookahead + present sync).
        int last_upload_jobs = 0;
        /// Cumulative upload-section milliseconds across presents.
        double upload_ms_total = 0.0;
        /// Last / max `endFrame` duration (captures GPU transfer stalls).
        double last_end_frame_ms = 0.0;
        double end_frame_ms_max = 0.0;
        int gpu_cache_hits = 0;
        int gpu_cache_misses = 0;
        int pipeline_rebuilds = 0;
        int srb_updates = 0;
        int staging_waits = 0;
        int textures_created = 0;
        int textures_pooled_reuses = 0;
        /// OCIO LUT free-list reuses (size/format match); separate from frame GpuTextureCache.
        int lut_textures_pooled_reuses = 0;
        int pipeline_lut_count = 0;
        /// Count of size-only presents (skipped uploads/lookahead).
        int size_only_presents = 0;
        /// D3D/Vulkan HW import object pool counters (durable).
        int hw_import_creates = 0;
        int hw_import_reuses = 0;
        /// Zero-copy CPU map uploads vs memcpy fallback this process.
        int zc_map_uploads = 0;
        int zc_copy_uploads = 0;
    };
    DebugStats get_debug_stats() const;
    int pipeline_lut_count() const;

private:
    struct StagingBuffer {
        std::vector<uint16_t> data;
        int width = 0;
        int height = 0;
        int channels = 0;
        int upload_token = 0;
        int frame_index = -1;
        /// When set, upload reads pin.data directly (no memcpy into data).
        CacheManager* pin_cache = nullptr;
        FramePin pin;
    };

    struct OcioLut {
        QRhiTexture* texture = nullptr;
        bool is_3d = true;
        int size = 0; // edge length for 3D
        int width = 0;
        int height = 0;
        int channels = 4;
        std::vector<float> rgba_data;
        bool dirty = false;
    };

    struct LutPoolKey {
        bool is_3d = false;
        int width = 0;
        int height = 0;
        int depth = 0; // 0 for 2D; edge for 3D

        bool operator==(const LutPoolKey& o) const
        {
            return is_3d == o.is_3d && width == o.width && height == o.height && depth == o.depth;
        }
    };

    struct LutPoolEntry {
        LutPoolKey key;
        QRhiTexture* texture = nullptr;
    };

    static LutPoolKey _lut_pool_key_3d(int edge);
    static LutPoolKey _lut_pool_key_2d(int width, int height);
    static bool _lut_texture_matches(const OcioLut& lut, const LutPoolKey& key);
    QRhiTexture* _acquire_lut_texture(const LutPoolKey& key);
    void _release_lut_texture(QRhiTexture* texture, const LutPoolKey& key);
    void _drain_lut_texture_pool();

    struct TextureState {
        QRhiTexture* texture = nullptr;
        QRhiTexture* texture_uv = nullptr;
        int sample_mode = 0;
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

    // Thread Loop
    /// ``size_only``: skip uploads/lookahead; re-present last textures with new fit.
    void render_frame(bool size_only = false);
    void update_rhi_resources(QRhiResourceUpdateBatch* batch);
    void build_pipeline(QRhiRenderPassDescriptor* rpDesc, bool force_rebuild = false);
    void update_srb_resources();
    void clear_tile_srb_cache();
    QRhiShaderResourceBindings* get_or_create_tile_srb(QRhiTexture* tex_a);
    void ensure_per_frame_ubo(int slot_count);
    quint32 per_frame_ubo_stride() const;
    bool bake_shaders(
        const std::string& pipeline_key,
        const std::string& vert_src,
        const std::string& frag_src);
    void parse_ocio_ubo_layout(const std::string& fragment_source);
    std::vector<char> pack_ocio_ubo();

    // Texture helpers
    void upload_texture(TextureState& state, const FrameSlotSpec& spec, CacheManager* cpu_cache, QRhiResourceUpdateBatch* batch);
    void upload_texture_3d(OcioLut& lut, QRhiResourceUpdateBatch* batch);
    void upload_texture_2d_lut(OcioLut& lut, QRhiResourceUpdateBatch* batch);
    bool resolve_display_texture(
        int source_index,
        CacheManager* cpu_cache,
        const FrameSlotSpec& spec,
        TextureState& bind_state,
        QRhiResourceUpdateBatch* batch,
        bool& bindings_dirty);
    void apply_pending_display_cache_ops();
    void publish_display_cache_snapshot();
    void destroy_upload_texture(void* texture);
    void drain_upload_queue(QRhiResourceUpdateBatch* batch);
    void put_ready_upload_jobs();
    QRhiTexture* upload_present_to_display_cache(
        int source_index,
        CacheManager* cpu_cache,
        const FrameSlotSpec& spec,
        QRhiResourceUpdateBatch* batch);
    void enqueue_gpu_lookahead();
    bool gpu_warmup_work_remaining() const;
    void _release_gpu_resources();

    /// While transport is playing, bound uploads per present (~1–2 frames).
    void _reset_present_upload_budget();
    size_t _present_upload_job_cap() const;
    bool _try_consume_upload_job();
    bool _try_consume_upload_bytes(size_t byte_size);
    void _update_adaptive_upload_budget();
    void _release_staging_pin(size_t slot);
    bool _fill_staging_from_cache(
        CacheManager* cpu_cache,
        int frame_index,
        size_t req_elements,
        StagingBuffer& ringBuf);

    // Staging ring: returns slot index, or nullopt if all slots still in flight.
    std::optional<size_t> acquire_staging_slot();
    QRhiTexture* acquire_frame_texture(int width, int height, int channels);
    QRhiTexture* acquire_hw_import_texture(int width, int height);
    void _init_metal_hw_import();
    void _shutdown_metal_hw_import();
    void _init_d3d11_hw_import();
    void _shutdown_d3d11_hw_import();
    void _init_vulkan_hw_import();
    void _shutdown_vulkan_hw_import();
    bool _hw_import_ready() const;
    const char* _hw_import_mode_name() const;
    bool _import_hw_job(UploadJob* job);
    void _log_movie_present_mode(const char* mode);
    bool _resolve_slot_dimensions(
        int source_index,
        int decoder_frame,
        CacheManager* cpu_cache,
        int& width,
        int& height,
        int& channels);

    // Threading / state sync
    void start_render_thread();
    void stop_render_thread();
    void render_thread_loop();
    bool initialize_rhi_on_thread();
    void shutdown_rhi_on_thread();
    void sync_and_render_on_thread();
    void _wake_render_thread();
    void _apply_viewer_output_format_unlocked();
    QRhiSwapChain::Format _qrhi_format_for_mode(ViewerOutputMode mode) const;
    ViewerOutputMode _resolve_supported_mode(ViewerOutputMode requested) const;
    /// Aspect-fit NDC scales for texDims into outputSize (PAR-aware).
    static void _fit_scales_for_size(
        int tex_w,
        int tex_h,
        float pixel_aspect_ratio,
        QSize output_size,
        float& out_fit_x,
        float& out_fit_y);

    bool _transport_can_advance(int global_frame);
    bool _transport_can_advance_unlocked(int global_frame);
    void _apply_transport_frame_to_params(RenderParams& params, int global_frame);
    void _update_transport_playheads(int global_frame, int direction);
    void _emit_transport_frame_changed(int frame, int direction);
    void _emit_transport_segment_boundary(int frame, int direction);
    /// Apply dirty transport program + map current playhead into params (no advance).
    void _prepare_transport_program_and_slots();
    /// Advance TransportClock after a successful present (vsync-paced path).
    void _advance_transport_after_present(TransportClock::TimePoint now);
    /// Null/offscreen: wall-clock tick then prepare (legacy cadence).
    void _tick_transport_and_prepare();
    /// Shot-local media seconds for the audio decoder (requires `_mutex`).
    double _audio_media_time_unlocked(int global_frame) const;

    std::vector<QRhiShaderResourceBinding> build_srb_bindings(
        QRhiTexture* tex_a,
        QRhiTexture* tex_b) const;

    std::thread _render_thread;
    mutable std::mutex _mutex;
    std::condition_variable _render_cond;
    std::atomic<bool> _run_thread{false};
    std::atomic<bool> _redraw_needed{false};
    std::atomic<bool> _clear_cache_pending{false};
    std::atomic<bool> _exposed{false};
    QWindow* _window = nullptr;
    /// Render-thread: presentsWithTransaction applied once after first createOrResize.
    bool _metal_pwt_applied = false;
    DebugStats _debug_stats;
    DebugStats _debug_stats_published;
    mutable std::mutex _debug_stats_mutex;
    void _publish_debug_stats();

    // Double-buffered inputs
    struct PendingOcioLut {
        int index = 0;
        bool is_3d = true;
        int size = 0;
        int width = 0;
        int height = 0;
        int channels = 4;
        std::vector<float> rgba_data;
    };
    RenderParams _pending_render_params;
    GradingParams _pending_grading_params;
    std::vector<PendingOcioLut> _pending_ocio_luts;
    std::vector<std::string> _pending_ocio_lut_dims;
    bool _ocio_lut_dims_dirty = false;
    bool _render_params_dirty = false;
    bool _grading_params_dirty = false;
    bool _ocio_luts_dirty = false;
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
    int _perFrameUboSlots = 0; // capacity in dynamic-offset slots
    QRhiBuffer* _ocioUbo = nullptr;
    QRhiSampler* _sampler = nullptr;
    QRhiSampler* _lutSampler = nullptr;
    QRhiShaderResourceBindings* _srb = nullptr;
    QRhiGraphicsPipeline* _pipeline = nullptr;
    QRhiTexture* _placeholderTex2D = nullptr;
    QRhiTexture* _placeholderLut3D = nullptr;
    QRhiTexture* _placeholderLut2D = nullptr;

    QShader _vertexShader;
    QShader _fragmentShader;
    std::string _cached_pipeline_key;
    
    // Pipelines for comparison modes
    TextureState _texAState;
    TextureState _texBState;
    std::vector<TextureState> _texturePool;
    std::vector<OcioLut> _active_ocio_luts;
    std::vector<std::string> _ocio_lut_slot_dims; // "2D" or "3D" per slot index
    int _pipeline_lut_count = 0; // LUT slots baked into current SRB layout
    static constexpr size_t kLutTexturePoolCapacity = 8;
    std::vector<LutPoolEntry> _lut_texture_pool;

    // Per-source (per texA) SRBs for tile compare — layout-compatible with _pipeline
    std::unordered_map<QRhiTexture*, QRhiShaderResourceBindings*> _tileSrbCache;

    // Shader binding layouts
    OcioUboLayout _ocioUboLayout;

    // Staging ring (depth >= max concurrent upload jobs)
    std::vector<StagingBuffer> _stagingRing;
    size_t _stagingRingIndex = 0;
    std::vector<uint64_t> _stagingGeneration; // generation that last used each slot
    uint64_t _upload_generation = 1;
    uint64_t _completed_upload_generation = 0;
    /// Per-present upload job budget (reset each render_frame).
    size_t _present_upload_job_limit = 0;
    size_t _present_upload_jobs_done = 0;
    size_t _present_upload_bytes_done = 0;
    size_t _present_upload_byte_limit = 0;
    /// Adaptive playing budget (job count), adjusted from end-frame / upload latency.
    size_t _adaptive_upload_jobs = 2;
    static constexpr size_t kPlayingUploadJobsPerPresent = 2;
    static constexpr size_t kPlayingUploadJobsMax = 4;
    /// Soft byte ceiling while playing (~one 8K RGBA16F frame).
    static constexpr size_t kPlayingUploadBytesPerPresent = 8ull * 1024ull * 1024ull * 8ull;

    std::unordered_map<int, CacheManager*> _caches;
    GpuTextureCache _displayCache;
    DisplayUploadQueue _uploadQueue;
    HwMetalImportContext _metal_hw_import;
    bool _metal_hw_import_ready = false;
    HwD3D11ImportContext _d3d11_hw_import;
    bool _d3d11_hw_import_ready = false;
    HwVulkanImportContext _vulkan_hw_import;
    bool _vulkan_hw_import_ready = false;
    bool _movie_present_mode_logged = false;

    // Display-cache ops deferred to the render thread
    bool _pending_limit_dirty = false;
    double _pending_limit_gb = 0.0;
    std::unordered_map<int, SourcePlayhead> _pending_playheads;
    std::vector<int> _pending_invalidate_sources;
    std::unordered_map<int, std::vector<int>> _display_frames_snapshot;
    GpuTextureCache::Stats _display_stats_snapshot;

    QRhiTexture* _last_bound_tex_a = nullptr;
    QRhiTexture* _last_bound_tex_b = nullptr;
    std::atomic<bool> _is_fallback_null_backend{false};
    bool _force_null_backend = false;
#if !defined(Q_OS_MACOS) && !defined(Q_OS_WIN)
    /// Owns the Vulkan instance for QRhi present; must outlive `_rhi`.
    QVulkanInstance* _vulkan_instance = nullptr;
#endif
    std::atomic<ViewerOutputMode> _requested_viewer_output_mode{ViewerOutputMode::Sdr};
    std::atomic<ViewerOutputMode> _actual_viewer_output_mode{ViewerOutputMode::Sdr};
    std::atomic<bool> _swap_format_dirty{false};

    TransportClock _transport;
    AudioEngine _audio;
    /// File decoder-frame origin for audio media time (under `_mutex`).
    int _audio_media_origin_frame = 0;
    std::function<void(int, int)> _frame_changed_callback;
    std::function<void(int, int)> _segment_boundary_callback;
    std::atomic<int> _pending_notify_frame{-1};
    std::atomic<int> _pending_notify_direction{1};
    std::atomic<bool> _frame_notify_pending{false};
    std::atomic<int> _pending_boundary_frame{-1};
    std::atomic<int> _pending_boundary_direction{1};
    std::atomic<bool> _boundary_notify_pending{false};
    std::atomic<bool> _transport_program_dirty{false};
    TransportProgram _pending_transport_program;
};
