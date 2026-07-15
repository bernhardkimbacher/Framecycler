#include "rhi_renderer.h"
#include "cache_manager.h"
#include "gpu_texture_cache.h"
#include <QWindow>
#include <QGuiApplication>
#include <QDebug>
#include <QEvent>
#include <regex>
#include <cmath>
#include <algorithm>
#include <chrono>

#if defined(Q_OS_MACOS)
#include <QtGui/rhi/qrhi_platform.h>
#elif defined(Q_OS_WIN)
#include <QtGui/rhi/qrhi_platform.h>
#endif

// Quad vertex layout
const float QUAD_VERTICES[] = {
    -1.0f,  1.0f,  0.0f, 1.0f,
    -1.0f, -1.0f,  0.0f, 0.0f,
     1.0f,  1.0f,  1.0f, 1.0f,
     1.0f, -1.0f,  1.0f, 0.0f
};
const size_t QUAD_VERTICES_SIZE = sizeof(QUAD_VERTICES);

RhiRenderer::RhiRenderer()
{
}

RhiRenderer::~RhiRenderer()
{
    shutdown();
}

bool RhiRenderer::initialize(uintptr_t window_ptr)
{
    _window = reinterpret_cast<QWindow*>(window_ptr);
    if (!_window) {
        return false;
    }

    _pending_size = _window->size();
    _resize_pending = true;

    start_render_thread();
    return true;
}

void RhiRenderer::shutdown()
{
    stop_render_thread();
}

void RhiRenderer::start_render_thread()
{
    if (_run_thread) {
        return;
    }
    _run_thread = true;
    _render_thread = std::thread(&RhiRenderer::render_thread_loop, this);
}

void RhiRenderer::stop_render_thread()
{
    if (!_run_thread) {
        return;
    }
    _run_thread = false;
    _render_cond.notify_all();
    if (_render_thread.joinable()) {
        _render_thread.join();
    }
}

void RhiRenderer::render_thread_loop()
{
    if (!initialize_rhi_on_thread()) {
        qWarning() << "RhiRenderer: Failed to initialize QRhi on render thread!";
        return;
    }

    while (_run_thread) {
        bool needs_render = false;
        bool resize = false;
        QSize target_size;
        {
            std::unique_lock<std::mutex> lock(_mutex);
            _render_cond.wait(lock, [this]() {
                return !_run_thread || _resize_pending || _redraw_needed || _clear_cache_pending;
            });

            if (!_run_thread) {
                break;
            }

            needs_render = _redraw_needed || _clear_cache_pending;
            _redraw_needed = false;
            resize = _resize_pending.exchange(false);
            target_size = _pending_size;
        }

        if (resize || needs_render) {
            sync_and_render_on_thread(resize, target_size);
        }
    }

    _release_gpu_resources();
    shutdown_rhi_on_thread();
}

bool RhiRenderer::initialize_rhi_on_thread()
{
    if (!_window) {
        return false;
    }

    _is_fallback_null_backend = false;

#if defined(Q_OS_MACOS)
    QRhiMetalInitParams params;
    _rhi = QRhi::create(QRhi::Metal, &params);
#elif defined(Q_OS_WIN)
    QRhiD3D11InitParams params;
    _rhi = QRhi::create(QRhi::D3D11, &params);
#else
    if (QGuiApplication::platformName() == QLatin1String("offscreen")) {
        qWarning() << "RhiRenderer: offscreen platform detected, bypassing Vulkan to use Null backend directly.";
        QRhiNullInitParams nullParams;
        _rhi = QRhi::create(QRhi::Null, &nullParams);
        _is_fallback_null_backend = true;
    } else {
        QRhiVulkanInitParams params;
        _rhi = QRhi::create(QRhi::Vulkan, &params);
    }
#endif

    if (!_rhi) {
        qWarning() << "RhiRenderer: Failed to initialize primary QRhi backend! Falling back to Null backend.";
        QRhiNullInitParams nullParams;
        _rhi = QRhi::create(QRhi::Null, &nullParams);
        _is_fallback_null_backend = true;
    }

    if (!_rhi) {
        qWarning() << "RhiRenderer: Failed to initialize QRhi backend!";
        return false;
    }

    _swapChain = _rhi->newSwapChain();
    _swapChain->setWindow(_window);
    _fallbackRpDesc = _swapChain->newCompatibleRenderPassDescriptor();
    _swapChain->setRenderPassDescriptor(_fallbackRpDesc);

    _stagingRing.resize(3);
    _displayCache.set_rhi(_rhi);
    _debug_stats.rhi_ready = true;
    return true;
}

void RhiRenderer::shutdown_rhi_on_thread()
{
    if (_pipeline) {
        _pipeline->destroy();
        delete _pipeline;
        _pipeline = nullptr;
    }
    if (_srb) {
        _srb->destroy();
        delete _srb;
        _srb = nullptr;
    }
    if (_vertexBuffer) {
        _vertexBuffer->destroy();
        delete _vertexBuffer;
        _vertexBuffer = nullptr;
    }
    if (_perFrameUbo) {
        _perFrameUbo->destroy();
        delete _perFrameUbo;
        _perFrameUbo = nullptr;
    }
    if (_ocioUbo) {
        _ocioUbo->destroy();
        delete _ocioUbo;
        _ocioUbo = nullptr;
    }
    if (_sampler) {
        _sampler->destroy();
        delete _sampler;
        _sampler = nullptr;
    }
    if (_lutSampler) {
        _lutSampler->destroy();
        delete _lutSampler;
        _lutSampler = nullptr;
    }
    if (_swapChain) {
        _swapChain->destroy();
        delete _swapChain;
        _swapChain = nullptr;
    }
    if (_fallbackRpDesc) {
        _fallbackRpDesc->destroy();
        delete _fallbackRpDesc;
        _fallbackRpDesc = nullptr;
    }

    delete _rhi;
    _rhi = nullptr;
    _debug_stats = DebugStats{};
}

void RhiRenderer::_wake_render_thread()
{
    _redraw_needed = true;
    _render_cond.notify_one();
}

void RhiRenderer::set_display_cache_limit_gb(double limit_gb)
{
    std::lock_guard<std::mutex> lock(_mutex);
    _displayCache.set_limit_gb(limit_gb);
}

void RhiRenderer::clear_display_cache()
{
    _clear_cache_pending = true;
    _wake_render_thread();
}

void RhiRenderer::set_source_playhead(int source_index, int playhead, int direction, int in_point, int out_point)
{
    std::lock_guard<std::mutex> lock(_mutex);
    SourcePlayhead ph;
    ph.playhead = playhead;
    ph.direction = direction;
    ph.in_point = in_point;
    ph.out_point = out_point;
    _displayCache.set_source_playhead(source_index, ph);
    _wake_render_thread();
}

void RhiRenderer::invalidate_display_cache_source(int source_index)
{
    std::lock_guard<std::mutex> lock(_mutex);
    _displayCache.invalidate_source(source_index);
    _wake_render_thread();
}

GpuTextureCache::Stats RhiRenderer::get_display_cache_stats() const
{
    return _displayCache.stats();
}

void RhiRenderer::update_render_params(const RenderParams& params)
{
    std::lock_guard<std::mutex> lock(_mutex);
    _pending_render_params = params;
    _render_params_dirty = true;
    _wake_render_thread();
}

void RhiRenderer::request_redraw()
{
    _wake_render_thread();
}

RhiRenderer::DebugStats RhiRenderer::get_debug_stats() const
{
    return _debug_stats;
}

void RhiRenderer::sync_and_render()
{
    _wake_render_thread();
}

void RhiRenderer::sync_and_render_on_thread(bool resize, const QSize& target_size)
{
    if (!_rhi || !_window) {
        return;
    }

    if (_window->isExposed()) {
        _exposed = true;
    }
    if (!_exposed.load()) {
        return;
    }

    if (_clear_cache_pending.exchange(false)) {
        if (!_displayCache.enabled()) {
            if (_texAState.texture) {
                _texAState.texture->destroy();
                delete _texAState.texture;
            }
            if (_texBState.texture) {
                _texBState.texture->destroy();
                delete _texBState.texture;
            }
        }
        {
            std::lock_guard<std::mutex> lock(_mutex);
            _displayCache.clear();
        }
        _texAState.texture = nullptr;
        _texBState.texture = nullptr;
        _last_bound_tex_a = nullptr;
        _last_bound_tex_b = nullptr;
        for (auto& state : _texturePool) {
            state.texture = nullptr;
        }
        if (_pipeline) {
            _pipeline->destroy();
            delete _pipeline;
            _pipeline = nullptr;
        }
        if (_srb) {
            _srb->destroy();
            delete _srb;
            _srb = nullptr;
        }
    }

    bool shaders_dirty = false;
    std::string frag_src_for_layout;

    {
        std::lock_guard<std::mutex> lock(_mutex);
        if (_render_params_dirty) {
            _active_render_params = _pending_render_params;
            _render_params_dirty = false;
        }
        if (_grading_params_dirty) {
            _active_grading_params = _pending_grading_params;
            _grading_params_dirty = false;
        }
        if (_ocio_luts_dirty) {
            // Destroy old active textures first (since we are on the render thread!)
            for (auto& lut : _active_ocio_luts) {
                if (lut.texture) {
                    lut.texture->destroy();
                    delete lut.texture;
                    lut.texture = nullptr;
                }
            }
            _active_ocio_luts.clear();

            // Build new active LUTs list from pending
            for (const auto& pending : _pending_ocio_luts) {
                while (static_cast<int>(_active_ocio_luts.size()) <= pending.index) {
                    _active_ocio_luts.push_back(OcioLut3D());
                }
                auto& lut = _active_ocio_luts[pending.index];
                lut.size = pending.size;
                lut.rgba_data = pending.rgba_data;
                lut.dirty = true;
            }
            _ocio_luts_dirty = false;
        }
        if (_shaders_dirty) {
            shaders_dirty = true;
            frag_src_for_layout = _pending_frag_src_for_layout;
            _shaders_dirty = false;
        }
    }

    if (shaders_dirty && !frag_src_for_layout.empty()) {
        parse_ocio_ubo_layout(frag_src_for_layout);
        if (_pipeline) {
            _pipeline->destroy();
            delete _pipeline;
            _pipeline = nullptr;
        }
        if (_srb) {
            _srb->destroy();
            delete _srb;
            _srb = nullptr;
        }
    }

    if (resize && target_size.width() > 0 && target_size.height() > 0) {
        if (_swapChain->createOrResize()) {
            build_pipeline(_swapChain->renderPassDescriptor(), true);
        }
    } else if (!_swapChain->createOrResize()) {
        qWarning() << "RhiRenderer: Failed to create/resize swap chain!";
        return;
    }

    render_frame();
}

void RhiRenderer::set_grading_uniform(const std::string& name, float value)
{
    std::lock_guard<std::mutex> lock(_mutex);
    _pending_grading_params.floats[name] = value;
    _grading_params_dirty = true;
    _render_params_dirty = true;
    _wake_render_thread();
}

void RhiRenderer::set_grading_uniform_vec3(const std::string& name, float x, float y, float z)
{
    std::lock_guard<std::mutex> lock(_mutex);
    _pending_grading_params.vec3s[name] = {x, y, z};
    _grading_params_dirty = true;
    _render_params_dirty = true;
    _wake_render_thread();
}

void RhiRenderer::clear_grading_uniforms()
{
    std::lock_guard<std::mutex> lock(_mutex);
    _pending_grading_params.floats.clear();
    _pending_grading_params.vec3s.clear();
    _grading_params_dirty = true;
    _render_params_dirty = true;
    _wake_render_thread();
}

void RhiRenderer::register_cache(int source_index, CacheManager* cache)
{
    std::lock_guard<std::mutex> lock(_mutex);
    _caches[source_index] = cache;
}

void RhiRenderer::set_shader_sources(const std::string& pipeline_key, const std::string& vert_src, const std::string& frag_src)
{
    std::lock_guard<std::mutex> lock(_mutex);
    if (_cached_pipeline_key == pipeline_key) {
        return;
    }
    
    // Bake shaders on the calling thread (CPU-only). GPU resource setup
    // (Ocio UBO, pipeline) happens in sync_and_render() on the GUI thread.
    if (bake_shaders(vert_src, frag_src)) {
        _cached_pipeline_key = pipeline_key;
        _pending_frag_src_for_layout = frag_src;
        _shaders_dirty = true;
        _render_params_dirty = true;
        _wake_render_thread();
    }
}

void RhiRenderer::set_exposed(bool exposed)
{
    _exposed = exposed;
    _wake_render_thread();
}

void RhiRenderer::set_pending_size(int width, int height)
{
    std::lock_guard<std::mutex> lock(_mutex);
    _pending_size = QSize(width, height);
    _resize_pending = true;
    _wake_render_thread();
}

void RhiRenderer::upload_ocio_lut_3d(int index, int size, const std::vector<float>& data)
{
    std::lock_guard<std::mutex> lock(_mutex);
    PendingLut3D lut;
    lut.index = index;
    lut.size = size;
    lut.rgba_data.resize(size * size * size * 4);
    
    // Convert RGB to RGBA
    for (int i = 0; i < size * size * size; ++i) {
        lut.rgba_data[i * 4 + 0] = data[i * 3 + 0];
        lut.rgba_data[i * 4 + 1] = data[i * 3 + 1];
        lut.rgba_data[i * 4 + 2] = data[i * 3 + 2];
        lut.rgba_data[i * 4 + 3] = 1.0f;
    }
    _pending_ocio_luts.push_back(lut);
    _ocio_luts_dirty = true;
    _shaders_dirty = true;
    _wake_render_thread();
}

void RhiRenderer::clear_ocio_luts()
{
    std::lock_guard<std::mutex> lock(_mutex);
    _pending_ocio_luts.clear();
    _ocio_luts_dirty = true;
    _shaders_dirty = true;
    _wake_render_thread();
}



void RhiRenderer::render_frame()
{
    using Clock = std::chrono::steady_clock;
    const auto frame_start = Clock::now();

    _debug_stats.shaders_valid = _vertexShader.isValid() && _fragmentShader.isValid();
    _debug_stats.pipeline_ready = _pipeline != nullptr;
    _debug_stats.tex_a_ready = _texAState.texture != nullptr;
    _debug_stats.last_upload_bytes = 0;
    _debug_stats.last_upload_count = 0;

    QRhi::FrameOpResult result = _rhi->beginFrame(_swapChain);
    if (result != QRhi::FrameOpSuccess) {
        _debug_stats.begin_frame_fail++;
        return;
    }
    _debug_stats.begin_frame_ok++;

    QRhiResourceUpdateBatch* batch = _rhi->nextResourceUpdateBatch();
    
    update_rhi_resources(batch);

    const auto upload_start = Clock::now();

    // 1. Upload frame textures
    const int compare_mode = _active_render_params.compare_mode;
    bool pipeline_dirty = false;
    for (auto slot : _active_render_params.slots) {
        CacheManager* cpu_cache = nullptr;
        {
            std::lock_guard<std::mutex> lock(_mutex);
            auto it = _caches.find(slot.source_index);
            if (it != _caches.end()) {
                cpu_cache = it->second;
            }
        }
        if (cpu_cache) {
            int w = 0, h = 0, channels = 0;
            if (cpu_cache->get_frame_dimensions(slot.frame_index, w, h, channels)) {
                _debug_stats.cache_hits++;
                slot.width = w;
                slot.height = h;
                slot.channels = channels;
                slot.data_size = static_cast<size_t>(w * h * channels);

                if (compare_mode == 3) {
                    while (static_cast<int>(_texturePool.size()) <= slot.source_index) {
                        _texturePool.push_back(TextureState());
                    }
                    resolve_display_texture(
                        slot.source_index,
                        cpu_cache,
                        slot,
                        _texturePool[slot.source_index],
                        batch,
                        pipeline_dirty);
                } else if (compare_mode == 0) {
                    if (slot.source_index == _active_render_params.sequence_index) {
                        resolve_display_texture(
                            slot.source_index,
                            cpu_cache,
                            slot,
                            _texAState,
                            batch,
                            pipeline_dirty);
                    }
                } else {
                    if (slot.source_index == 0) {
                        resolve_display_texture(
                            slot.source_index,
                            cpu_cache,
                            slot,
                            _texAState,
                            batch,
                            pipeline_dirty);
                    } else if (slot.source_index == 1) {
                        resolve_display_texture(
                            slot.source_index,
                            cpu_cache,
                            slot,
                            _texBState,
                            batch,
                            pipeline_dirty);
                    }
                }
            } else {
                _debug_stats.cache_misses++;
            }
        }
    }

    auto display_stats = _displayCache.stats();
    _debug_stats.gpu_cache_hits = display_stats.hits;
    _debug_stats.gpu_cache_misses = display_stats.misses;

    if (pipeline_dirty || _texAState.texture != _last_bound_tex_a || _texBState.texture != _last_bound_tex_b) {
        build_pipeline(_swapChain->renderPassDescriptor(), true);
        _last_bound_tex_a = _texAState.texture;
        _last_bound_tex_b = _texBState.texture;
    }

    const auto upload_end = Clock::now();

    _debug_stats.last_tex_w = _texAState.last_w;
    _debug_stats.last_tex_h = _texAState.last_h;
    _debug_stats.tex_a_ready = _texAState.texture != nullptr;
    if (_swapChain && _swapChain->currentFrameRenderTarget()) {
        QSize swapSize = _swapChain->currentFrameRenderTarget()->pixelSize();
        _debug_stats.swap_w = swapSize.width();
        _debug_stats.swap_h = swapSize.height();
    }

    // 2. Upload OCIO 3D LUTs
    for (auto& lut : _active_ocio_luts) {
        if (lut.dirty && lut.size > 0) {
            upload_texture_3d(lut, batch);
            lut.dirty = false;
        }
    }

    // 3. Update dynamic uniform buffers
    struct PerFrameUboData {
        float scale_x;
        float scale_y;
        float pan_x;
        float pan_y;
        int compare_mode;
        float wipe_pos;
        int channel_mask;
        int padding;
    };

    PerFrameUboData perFrame;
    perFrame.scale_x = _active_render_params.scale_x;
    perFrame.scale_y = _active_render_params.scale_y;
    perFrame.pan_x = _active_render_params.pan_x;
    perFrame.pan_y = _active_render_params.pan_y;
    perFrame.compare_mode = _active_render_params.compare_mode;
    perFrame.wipe_pos = _active_render_params.wipe_pos;
    perFrame.channel_mask = _active_render_params.channel_mask;
    perFrame.padding = 0;

    batch->updateDynamicBuffer(_perFrameUbo, 0, sizeof(perFrame), &perFrame);

    if (_ocioUbo && _ocioUboLayout.size > 0) {
        std::vector<char> ocioData = pack_ocio_ubo();
        batch->updateDynamicBuffer(_ocioUbo, 0, ocioData.size(), ocioData.data());
    }

    // 4. Ensure graphics pipeline is valid for this render pass
    build_pipeline(_swapChain->renderPassDescriptor());

    const auto draw_start = Clock::now();

    QRhiCommandBuffer* cb = _swapChain->currentFrameCommandBuffer();
    QRhiRenderTarget* rt = _swapChain->currentFrameRenderTarget();
    QColor clearColor(0, 0, 0, 1);

    if (_active_render_params.compare_mode == 3) {
        // Tile Layout Compare Mode: Multiple Draw passes
        cb->beginPass(rt, clearColor, {1.0f, 0}, batch);
        
        QSize outputSize = rt->pixelSize();
        cb->setViewport(QRhiViewport(0, 0, outputSize.width(), outputSize.height()));

        for (const auto& tile : _active_render_params.tiles) {
            if (tile.source_index < 0 || tile.source_index >= static_cast<int>(_texturePool.size())) {
                continue;
            }
            auto& texState = _texturePool[tile.source_index];
            if (!texState.texture) {
                continue;
            }

            // Temporarily swap TexA to point to this source texture for this draw call
            _texAState.texture = texState.texture;
            build_pipeline(_swapChain->renderPassDescriptor(), true);

            PerFrameUboData tileFrame;
            tileFrame.scale_x = tile.scale_x;
            tileFrame.scale_y = tile.scale_y;
            tileFrame.pan_x = tile.offset_x;
            tileFrame.pan_y = tile.offset_y;
            tileFrame.compare_mode = 0;
            tileFrame.wipe_pos = 0.5f;
            tileFrame.channel_mask = _active_render_params.channel_mask;
            tileFrame.padding = 0;

            QRhiResourceUpdateBatch* tileBatch = _rhi->nextResourceUpdateBatch();
            tileBatch->updateDynamicBuffer(_perFrameUbo, 0, sizeof(tileFrame), &tileFrame);

            cb->setGraphicsPipeline(_pipeline);
            cb->setShaderResources();
            
            QRhiCommandBuffer::VertexInput vInput(_vertexBuffer, 0);
            cb->setVertexInput(0, 1, &vInput);
            
            cb->resourceUpdate(tileBatch);
            cb->draw(4);
        }
        cb->endPass();
    } else {
        // Normal rendering pass
        cb->beginPass(rt, clearColor, {1.0f, 0}, batch);
        if (_pipeline && _texAState.texture) {
            QSize outputSize = rt->pixelSize();
            cb->setViewport(QRhiViewport(0, 0, outputSize.width(), outputSize.height()));
            cb->setGraphicsPipeline(_pipeline);
            cb->setShaderResources();
            
            QRhiCommandBuffer::VertexInput vInput(_vertexBuffer, 0);
            cb->setVertexInput(0, 1, &vInput);
            
            cb->draw(4);
            _debug_stats.frames_drawn++;
        } else {
            _debug_stats.frames_cleared_only++;
        }
        cb->endPass();
    }

    _debug_stats.pipeline_ready = _pipeline != nullptr;
    _rhi->endFrame(_swapChain);
    if (_window) {
        _window->requestUpdate();
    }

    const auto frame_end = Clock::now();
    _debug_stats.last_upload_ms = std::chrono::duration<double, std::milli>(upload_end - upload_start).count();
    _debug_stats.last_draw_ms = std::chrono::duration<double, std::milli>(frame_end - draw_start).count();
    _debug_stats.last_render_ms = std::chrono::duration<double, std::milli>(frame_end - frame_start).count();
}

void RhiRenderer::update_rhi_resources(QRhiResourceUpdateBatch* batch)
{
    if (!_vertexBuffer) {
        _vertexBuffer = _rhi->newBuffer(QRhiBuffer::Immutable, QRhiBuffer::VertexBuffer, QUAD_VERTICES_SIZE);
        _vertexBuffer->create();
        if (batch) {
            batch->uploadStaticBuffer(_vertexBuffer, QUAD_VERTICES);
        }
    }

    if (!_perFrameUbo) {
        _perFrameUbo = _rhi->newBuffer(QRhiBuffer::Dynamic, QRhiBuffer::UniformBuffer, sizeof(float) * 8);
        _perFrameUbo->create();
    }

    if (!_sampler) {
        _sampler = _rhi->newSampler(QRhiSampler::Linear, QRhiSampler::Linear, QRhiSampler::None, QRhiSampler::ClampToEdge, QRhiSampler::ClampToEdge);
        _sampler->create();
    }

    if (!_lutSampler) {
        _lutSampler = _rhi->newSampler(QRhiSampler::Linear, QRhiSampler::Linear, QRhiSampler::None, QRhiSampler::ClampToEdge, QRhiSampler::ClampToEdge);
        _lutSampler->create();
    }

    if (!_texBState.texture) {
        _texBState.texture = _rhi->newTexture(QRhiTexture::RGBA16F, QSize(1, 1));
        _texBState.texture->create();
        
        if (batch) {
            std::vector<uint16_t> black(4, 0);
            QByteArray arr(reinterpret_cast<const char*>(black.data()), static_cast<int>(black.size() * sizeof(uint16_t)));
            batch->uploadTexture(
                _texBState.texture,
                QRhiTextureUploadDescription(
                    QRhiTextureUploadEntry(0, 0, QRhiTextureSubresourceUploadDescription(arr))));
        }
    }
}

void RhiRenderer::build_pipeline(QRhiRenderPassDescriptor* rpDesc, bool force_rebuild)
{
    if (!_rhi || !rpDesc || !_vertexShader.isValid() || !_fragmentShader.isValid()) {
        return;
    }

    if (_pipeline && !force_rebuild) {
        return;
    }

    if (_pipeline) {
        _pipeline->destroy();
        delete _pipeline;
        _pipeline = nullptr;
    }
    if (_srb) {
        _srb->destroy();
        delete _srb;
        _srb = nullptr;
    }

    std::vector<QRhiShaderResourceBinding> bindings;
    bindings.push_back(QRhiShaderResourceBinding::uniformBuffer(0, QRhiShaderResourceBinding::VertexStage | QRhiShaderResourceBinding::FragmentStage, _perFrameUbo));
    
    if (_texAState.texture) {
        bindings.push_back(QRhiShaderResourceBinding::sampledTexture(1, QRhiShaderResourceBinding::FragmentStage, _texAState.texture, _sampler));
    }
    
    if (_texBState.texture) {
        bindings.push_back(QRhiShaderResourceBinding::sampledTexture(2, QRhiShaderResourceBinding::FragmentStage, _texBState.texture, _sampler));
    }

    int lutBinding = 3;
    for (auto& lut : _active_ocio_luts) {
        if (lut.texture) {
            bindings.push_back(QRhiShaderResourceBinding::sampledTexture(lutBinding, QRhiShaderResourceBinding::FragmentStage, lut.texture, _lutSampler));
            lutBinding++;
        }
    }

    if (_ocioUbo && _ocioUboLayout.binding >= 0) {
        bindings.push_back(QRhiShaderResourceBinding::uniformBuffer(_ocioUboLayout.binding, QRhiShaderResourceBinding::FragmentStage, _ocioUbo));
    }

    _srb = _rhi->newShaderResourceBindings();
    _srb->setBindings(bindings.begin(), bindings.end());
    _srb->create();

    _pipeline = _rhi->newGraphicsPipeline();
    _pipeline->setShaderStages({
        {QRhiShaderStage::Vertex, _vertexShader},
        {QRhiShaderStage::Fragment, _fragmentShader}
    });

    QRhiVertexInputLayout layout;
    QRhiVertexInputBinding binding;
    binding.setStride(16);
    layout.setBindings({binding});
    layout.setAttributes({
        {0, 0, QRhiVertexInputAttribute::Float2, 0},
        {0, 1, QRhiVertexInputAttribute::Float2, 8}
    });

    _pipeline->setVertexInputLayout(layout);
    _pipeline->setShaderResourceBindings(_srb);
    _pipeline->setTopology(QRhiGraphicsPipeline::Topology::TriangleStrip);
    _pipeline->setRenderPassDescriptor(rpDesc);
    _pipeline->create();
}

bool RhiRenderer::bake_shaders(const std::string& vert_src, const std::string& frag_src)
{
    // Bake Vertex Shader
    QShaderBaker vertBaker;
    vertBaker.setGeneratedShaderVariants({QShader::StandardShader});
    vertBaker.setGeneratedShaders({
        {QShader::SpirvShader, QShaderVersion(100)},
        {QShader::GlslShader, QShaderVersion(450)},
        {QShader::MslShader, QShaderVersion(20)},
        {QShader::HlslShader, QShaderVersion(50)},
    });
    vertBaker.setSourceString(QByteArray::fromStdString(vert_src), QShader::VertexStage, QByteArray("quad.vert"));
    QShader vertShader = vertBaker.bake();
    if (!vertShader.isValid()) {
        qWarning() << "RhiRenderer: Vertex shader bake failed:" << vertBaker.errorMessage();
        return false;
    }

    // Bake Fragment Shader
    QShaderBaker fragBaker;
    fragBaker.setGeneratedShaderVariants({QShader::StandardShader});
    fragBaker.setGeneratedShaders({
        {QShader::SpirvShader, QShaderVersion(100)},
        {QShader::GlslShader, QShaderVersion(450)},
        {QShader::MslShader, QShaderVersion(20)},
        {QShader::HlslShader, QShaderVersion(50)},
    });
    fragBaker.setSourceString(QByteArray::fromStdString(frag_src), QShader::FragmentStage, QByteArray("quad.frag"));
    QShader fragShader = fragBaker.bake();
    if (!fragShader.isValid()) {
        qWarning() << "RhiRenderer: Fragment shader bake failed:" << fragBaker.errorMessage();
        return false;
    }

    _vertexShader = vertShader;
    _fragmentShader = fragShader;
    return true;
}

void RhiRenderer::parse_ocio_ubo_layout(const std::string& fragment_source)
{
    _ocioUboLayout.members.clear();
    _ocioUboLayout.size = 0;
    _ocioUboLayout.binding = -1;
    if (_ocioUbo) {
        _ocioUbo->destroy();
        delete _ocioUbo;
        _ocioUbo = nullptr;
    }

    // Regex to find: layout (std140, binding = X) uniform OcioDynamicUbo { body }
    std::regex uboBlockRe("layout\\s*\\(\\s*std140\\s*,\\s*binding\\s*=\\s*(\\d+)\\s*\\)\\s*uniform\\s+OcioDynamicUbo\\s*\\{([^}]*)\\}");
    std::smatch uboMatch;
    if (!std::regex_search(fragment_source, uboMatch, uboBlockRe)) {
        return;
    }

    _ocioUboLayout.binding = std::stoi(uboMatch[1].str());
    std::string body = uboMatch[2].str();

    // Regex to parse members: type name;
    std::regex memberRe("([A-Za-z0-9_]+)\\s+([A-Za-z0-9_]+)\\s*;");
    auto words_begin = std::sregex_iterator(body.begin(), body.end(), memberRe);
    auto words_end = std::sregex_iterator();

    std::vector<std::pair<std::string, std::string>> members;
    for (std::sregex_iterator i = words_begin; i != words_end; ++i) {
        std::smatch match = *i;
        members.push_back({match[1].str(), match[2].str()});
    }

    // Compute std140 layouts
    int offset = 0;
    for (const auto& pair : members) {
        std::string type_name = pair.first;
        std::string name = pair.second;
        
        int align = 4, size = 4;
        bool is_vec3 = false;

        if (type_name == "float" || type_name == "int" || type_name == "bool") {
            align = 4; size = 4;
        } else if (type_name == "vec2") {
            align = 8; size = 8;
        } else if (type_name == "vec3") {
            align = 16; size = 16; is_vec3 = true; // vec3 behaves as vec4 for padding/align
        } else if (type_name == "vec4") {
            align = 16; size = 16;
        } else {
            continue;
        }

        if (offset % align) {
            offset += align - (offset % align);
        }

        OcioUniformMember member;
        member.name = name;
        member.offset = offset;
        member.size = size;
        member.is_vec3 = is_vec3;
        _ocioUboLayout.members[name] = member;
        offset += size;
    }

    if (offset > 0) {
        _ocioUboLayout.size = (offset + 15) / 16 * 16;
        _ocioUbo = _rhi->newBuffer(QRhiBuffer::Dynamic, QRhiBuffer::UniformBuffer, _ocioUboLayout.size);
        _ocioUbo->create();
    }
}

std::vector<char> RhiRenderer::pack_ocio_ubo()
{
    std::vector<char> buf(_ocioUboLayout.size, 0);
    for (const auto& pair : _active_grading_params.floats) {
        auto it = _ocioUboLayout.members.find(pair.first);
        if (it == _ocioUboLayout.members.end() || it->second.is_vec3) {
            continue;
        }
        float val = pair.second;
        if (!std::isfinite(val)) {
            val = 0.0f;
        }
        std::memcpy(buf.data() + it->second.offset, &val, sizeof(float));
    }

    for (const auto& pair : _active_grading_params.vec3s) {
        auto it = _ocioUboLayout.members.find(pair.first);
        if (it == _ocioUboLayout.members.end() || !it->second.is_vec3) {
            continue;
        }
        std::array<float, 3> val = pair.second;
        std::memcpy(buf.data() + it->second.offset, val.data(), sizeof(float) * 3);
    }
    return buf;
}

bool RhiRenderer::resolve_display_texture(
    int source_index,
    CacheManager* cpu_cache,
    const FrameSlotSpec& spec,
    TextureState& bind_state,
    QRhiResourceUpdateBatch* batch,
    bool& pipeline_dirty)
{
    if (spec.width <= 0 || spec.height <= 0) {
        return false;
    }

    if (!_displayCache.enabled()) {
        QRhiTexture* previous = bind_state.texture;
        upload_texture(bind_state, spec, cpu_cache, batch);
        if (bind_state.texture != previous) {
            pipeline_dirty = true;
        }
        return bind_state.texture != nullptr;
    }

    QRhiTexture* cached = _displayCache.try_get(
        source_index,
        spec.frame_index,
        spec.width,
        spec.height,
        spec.channels,
        cpu_cache);
    if (cached) {
        if (bind_state.texture != cached) {
            pipeline_dirty = true;
        }
        bind_state.texture = cached;
        bind_state.last_w = spec.width;
        bind_state.last_h = spec.height;
        bind_state.last_channels = spec.channels;
        bind_state.last_upload_token = spec.upload_token;
        return true;
    }

    TextureState upload_state;
    upload_texture(upload_state, spec, cpu_cache, batch);
    if (!upload_state.texture) {
        return false;
    }

    const size_t bytes = spec.data_size * sizeof(uint16_t);
    _displayCache.put(
        source_index,
        spec.frame_index,
        spec.width,
        spec.height,
        spec.channels,
        upload_state.texture,
        bytes);

    if (bind_state.texture != upload_state.texture) {
        pipeline_dirty = true;
    }
    bind_state.texture = upload_state.texture;
    bind_state.last_w = upload_state.last_w;
    bind_state.last_h = upload_state.last_h;
    bind_state.last_channels = upload_state.last_channels;
    bind_state.last_upload_token = upload_state.last_upload_token;
    return true;
}

void RhiRenderer::upload_texture(TextureState& state, const FrameSlotSpec& spec, CacheManager* cpu_cache, QRhiResourceUpdateBatch* batch)
{
    if (spec.width <= 0 || spec.height <= 0) {
        return;
    }

    QRhiTexture::Format texFormat = (spec.channels == 1) ? QRhiTexture::R16F : QRhiTexture::RGBA16F;
    bool sizeChanged = !state.texture || state.last_w != spec.width || state.last_h != spec.height || state.last_channels != spec.channels;
    
    if (sizeChanged) {
        if (state.texture) {
            state.texture->destroy();
            delete state.texture;
        }
        state.texture = _rhi->newTexture(texFormat, QSize(spec.width, spec.height));
        state.texture->create();
        state.last_w = spec.width;
        state.last_h = spec.height;
        state.last_channels = spec.channels;
        build_pipeline(_swapChain->renderPassDescriptor(), true);
    }

    if (state.last_upload_token == spec.upload_token && !sizeChanged) {
        return;
    }

    // Dynamic sizing of the local ring buffer
    StagingBuffer& ringBuf = _stagingRing[_stagingRingIndex];
    _stagingRingIndex = (_stagingRingIndex + 1) % _stagingRing.size();

    size_t reqElements = spec.data_size;
    if (ringBuf.data.size() < reqElements) {
        ringBuf.data.resize(reqElements);
    }
    
    if (cpu_cache) {
        if (!cpu_cache->copy_frame_data(spec.frame_index, ringBuf.data.data(), reqElements)) {
            return;
        }
    } else {
        return;
    }

    QByteArray wrappedData(reinterpret_cast<const char*>(ringBuf.data.data()), static_cast<int>(reqElements * sizeof(uint16_t)));
    QRhiTextureSubresourceUploadDescription subres(wrappedData);
    subres.setDataStride(spec.width * spec.channels * sizeof(uint16_t));

    QRhiTextureUploadDescription desc(QRhiTextureUploadEntry(0, 0, subres));
    batch->uploadTexture(state.texture, desc);

    state.last_upload_token = spec.upload_token;
    _debug_stats.last_upload_bytes += static_cast<int>(reqElements * sizeof(uint16_t));
    _debug_stats.last_upload_count++;
}

void RhiRenderer::upload_texture_3d(OcioLut3D& lut, QRhiResourceUpdateBatch* batch)
{
    if (!_rhi || lut.size <= 0) {
        return;
    }

    if (lut.texture) {
        lut.texture->destroy();
        delete lut.texture;
    }

    lut.texture = _rhi->newTexture(QRhiTexture::RGBA32F, lut.size, lut.size, lut.size, QRhiTexture::ThreeDimensional);
    lut.texture->create();

    int rowStride = lut.size * 4 * sizeof(float);
    std::vector<QRhiTextureUploadEntry> entries;
    entries.reserve(lut.size);

    for (int layer = 0; layer < lut.size; ++layer) {
        size_t sliceOffset = layer * lut.size * lut.size * 4;
        QByteArray sliceData = QByteArray::fromRawData(reinterpret_cast<const char*>(lut.rgba_data.data() + sliceOffset), lut.size * lut.size * 4 * sizeof(float));
        QRhiTextureSubresourceUploadDescription subres(sliceData);
        subres.setDataStride(rowStride);
        entries.push_back(QRhiTextureUploadEntry(layer, 0, subres));
    }

    QRhiTextureUploadDescription desc;
    desc.setEntries(entries.begin(), entries.end());
    batch->uploadTexture(lut.texture, desc);
    
    build_pipeline(_swapChain->renderPassDescriptor(), true);
}

void RhiRenderer::_release_gpu_resources() {
    if (_displayCache.enabled()) {
        _displayCache.clear();
    } else {
        if (_texAState.texture) {
            _texAState.texture->destroy();
            delete _texAState.texture;
        }
        if (_texBState.texture) {
            _texBState.texture->destroy();
            delete _texBState.texture;
        }
    }
    _texAState.texture = nullptr;
    _texBState.texture = nullptr;
    _last_bound_tex_a = nullptr;
    _last_bound_tex_b = nullptr;
    for (auto& state : _texturePool) {
        state.texture = nullptr;
    }
    _texturePool.clear();

    for (auto& lut : _active_ocio_luts) {
        if (lut.texture) {
            lut.texture->destroy();
            delete lut.texture;
        }
    }
    _active_ocio_luts.clear();
}
