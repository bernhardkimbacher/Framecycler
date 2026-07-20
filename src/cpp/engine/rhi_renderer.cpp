#include "rhi_renderer.h"
#include "cache_manager.h"
#include "gpu_texture_cache.h"
#include "display_upload_queue.h"
#include "hw_frame_ticket.h"
#include <QWindow>
#include <QGuiApplication>
#include <QDebug>
#include <QEvent>
#include <QStandardPaths>
#include <QDir>
#include <QFile>
#include <QCryptographicHash>
#include <regex>
#include <cmath>
#include <algorithm>
#include <chrono>
#include <cstring>
#include <optional>

#if defined(Q_OS_MACOS) || defined(Q_OS_WIN) || defined(Q_OS_LINUX)
#include <QtGui/rhi/qrhi_platform.h>
#endif
#if defined(Q_OS_LINUX)
#include <QVulkanInstance>
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

    // Resolve Null preference on the GUI thread — QGuiApplication::platformName()
    // is not safe to call from the dedicated render thread.
    const QByteArray forceNull = qgetenv("FRAMECYCLER_FORCE_NULL_RHI");
    if (forceNull == "1" || forceNull.compare("true", Qt::CaseInsensitive) == 0) {
        _force_null_backend = true;
    }
    if (QGuiApplication::platformName() == QLatin1String("offscreen")) {
        _force_null_backend = true;
    }

    _pending_size = _window->size();
    _resize_pending = true;

    start_render_thread();
    return true;
}

void RhiRenderer::set_force_null_backend(bool enabled)
{
    _force_null_backend = enabled;
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
        bool transport_playing = false;
        const bool null_backend = _is_fallback_null_backend.load();
        QSize target_size;
        {
            std::unique_lock<std::mutex> lock(_mutex);
            transport_playing = _transport.is_playing();
            if (transport_playing && null_backend) {
                // Null/offscreen: wall-clock FPS pacing (no real vsync).
                const auto deadline = _transport.next_deadline();
                _render_cond.wait_until(lock, deadline, [this]() {
                    return !_run_thread || _resize_pending || _redraw_needed
                        || _clear_cache_pending || _transport_program_dirty.load();
                });
            } else if (transport_playing) {
                // Present-paced: proceed when exposed so beginFrame/endFrame
                // provide display cadence; wait while hidden to avoid a CPU spin.
                _render_cond.wait(lock, [this]() {
                    return !_run_thread || _resize_pending || _redraw_needed
                        || _clear_cache_pending || _transport_program_dirty.load()
                        || (_transport.is_playing() && _exposed.load());
                });
            } else {
                _render_cond.wait(lock, [this]() {
                    return !_run_thread || _resize_pending || _redraw_needed
                        || _clear_cache_pending || _transport_program_dirty.load();
                });
            }

            if (!_run_thread) {
                break;
            }

            needs_render = _redraw_needed || _clear_cache_pending || _transport.is_playing()
                || _transport_program_dirty.load();
            _redraw_needed = false;
            resize = _resize_pending.exchange(false);
            target_size = _pending_size;
        }

        if (null_backend) {
            // Wall-clock tick before present (CI / offscreen).
            _tick_transport_and_prepare();
        } else {
            // Real swapchain: prepare current playhead; advance after endFrame.
            _prepare_transport_program_and_slots();
        }

        if (resize || needs_render || _transport.is_playing()) {
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

    if (_force_null_backend) {
        qWarning() << "RhiRenderer: using Null backend (forced/offscreen)";
        QRhiNullInitParams nullParams;
        _rhi = QRhi::create(QRhi::Null, &nullParams);
        _is_fallback_null_backend = true;
    } else {
#if defined(Q_OS_MACOS)
        QRhiMetalInitParams params;
        _rhi = QRhi::create(QRhi::Metal, &params);
#elif defined(Q_OS_WIN)
        QRhiD3D11InitParams params;
        _rhi = QRhi::create(QRhi::D3D11, &params);
#else
        QRhiVulkanInitParams params;
        _rhi = QRhi::create(QRhi::Vulkan, &params);
#endif
    }

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

    _stagingRing.resize(6);
    _stagingGeneration.assign(_stagingRing.size(), 0);
    _displayCache.set_rhi(_rhi);
    _init_metal_hw_import();
    _init_d3d11_hw_import();
    _init_vulkan_hw_import();
    _debug_stats.rhi_ready = true;
    return true;
}

void RhiRenderer::shutdown_rhi_on_thread()
{
    clear_tile_srb_cache();

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
    _perFrameUboSlots = 0;
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
    if (_placeholderTex2D) {
        _placeholderTex2D->destroy();
        delete _placeholderTex2D;
        _placeholderTex2D = nullptr;
    }
    if (_placeholderLut3D) {
        _placeholderLut3D->destroy();
        delete _placeholderLut3D;
        _placeholderLut3D = nullptr;
    }
    if (_placeholderLut2D) {
        _placeholderLut2D->destroy();
        delete _placeholderLut2D;
        _placeholderLut2D = nullptr;
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

    _shutdown_metal_hw_import();
    _shutdown_d3d11_hw_import();
    _shutdown_vulkan_hw_import();

    delete _rhi;
    _rhi = nullptr;
    _debug_stats = DebugStats{};
}

void RhiRenderer::_wake_render_thread()
{
    _redraw_needed = true;
    _render_cond.notify_one();
}

void RhiRenderer::set_transport_program(const TransportProgram& program)
{
    {
        std::lock_guard<std::mutex> lock(_mutex);
        _pending_transport_program = program;
        _transport_program_dirty = true;
    }
    _wake_render_thread();
}

void RhiRenderer::transport_play()
{
    double media_t = 0.0;
    {
        std::lock_guard<std::mutex> lock(_mutex);
        _transport.play();
        media_t = _audio_media_time_unlocked(_transport.current_frame());
    }
    _audio.seek_media_time(media_t);
    _audio.play();
    _wake_render_thread();
}

void RhiRenderer::transport_pause()
{
    {
        std::lock_guard<std::mutex> lock(_mutex);
        _transport.pause();
    }
    _audio.pause();
}

void RhiRenderer::transport_seek(int global_frame, bool scrub_preview)
{
    double media_t = 0.0;
    bool playing = false;
    {
        std::lock_guard<std::mutex> lock(_mutex);
        _transport.seek(global_frame);
        _apply_transport_frame_to_params(_pending_render_params, _transport.current_frame());
        _render_params_dirty = true;
        media_t = _audio_media_time_unlocked(_transport.current_frame());
        playing = _transport.is_playing();
    }
    // Trust the UI preview flag (already gated on the scrub-audio preference).
    const bool preview = scrub_preview && !playing;
    _audio.seek_media_time(media_t, preview);
    _wake_render_thread();
}

void RhiRenderer::set_audio_media_path(const std::string& path, int media_origin_frame)
{
    {
        std::lock_guard<std::mutex> lock(_mutex);
        _audio_media_origin_frame = media_origin_frame;
    }
    _audio.set_media_path(path);
}

double RhiRenderer::_audio_media_time_unlocked(int global_frame) const
{
    const double fps = std::max(1e-6, _transport.fps());
    const auto prog = _transport.program();
    if (!prog.slots.empty()) {
        // Slot 0 is the active A version — same source Python binds for audio.
        const int source_index = prog.slots[0].source_index;
        const int dec = _transport.decoder_frame_for_source(source_index, global_frame);
        if (dec >= 0) {
            const double t =
                static_cast<double>(dec - _audio_media_origin_frame) / fps;
            return std::max(0.0, t);
        }
        const auto& slot = prog.slots[0];
        int local = global_frame - slot.segment_global_start;
        if (local < 0) {
            local = 0;
        }
        return static_cast<double>(local) / fps;
    }
    return static_cast<double>(std::max(0, global_frame - _audio_media_origin_frame)) / fps;
}

void RhiRenderer::set_audio_volume(float volume)
{
    _audio.set_volume(volume);
}

void RhiRenderer::set_audio_muted(bool muted)
{
    _audio.set_muted(muted);
}

void RhiRenderer::set_audio_scrub(bool enabled)
{
    _audio.set_scrub_audio(enabled);
}

void RhiRenderer::begin_audio_scrub()
{
    _audio.begin_scrub();
}

void RhiRenderer::end_audio_scrub()
{
    _audio.end_scrub();
}

void RhiRenderer::set_audio_output_device(const std::string& device_id)
{
    _audio.set_output_device(device_id);
}

std::string RhiRenderer::audio_output_device() const
{
    return _audio.output_device_id();
}

std::string RhiRenderer::audio_last_error() const
{
    return _audio.last_error();
}

bool RhiRenderer::audio_has_audio() const
{
    return _audio.has_audio();
}

std::vector<float> RhiRenderer::audio_peaks(int peaks_per_second)
{
    return _audio.peaks(peaks_per_second);
}

std::vector<AudioDeviceInfo> RhiRenderer::list_audio_output_devices()
{
    return AudioEngine::list_output_devices();
}

int RhiRenderer::get_transport_frame() const
{
    std::lock_guard<std::mutex> lock(_mutex);
    return _transport.current_frame();
}

int RhiRenderer::get_transport_direction() const
{
    std::lock_guard<std::mutex> lock(_mutex);
    return _transport.direction();
}

bool RhiRenderer::is_transport_playing() const
{
    std::lock_guard<std::mutex> lock(_mutex);
    return _transport.is_playing();
}

void RhiRenderer::set_frame_changed_callback(std::function<void(int, int)> cb)
{
    std::lock_guard<std::mutex> lock(_mutex);
    _frame_changed_callback = std::move(cb);
}

void RhiRenderer::set_segment_boundary_callback(std::function<void(int, int)> cb)
{
    std::lock_guard<std::mutex> lock(_mutex);
    _segment_boundary_callback = std::move(cb);
}

void RhiRenderer::ack_transport_frame_notify()
{
    _frame_notify_pending.store(false);
}

bool RhiRenderer::poll_transport_frame_notify(int& frame_out, int& direction_out)
{
    if (!_frame_notify_pending.exchange(false)) {
        return false;
    }
    frame_out = _pending_notify_frame.load();
    direction_out = _pending_notify_direction.load();
    return frame_out >= 0;
}

bool RhiRenderer::poll_transport_boundary_notify(int& frame_out, int& direction_out)
{
    if (!_boundary_notify_pending.exchange(false)) {
        return false;
    }
    frame_out = _pending_boundary_frame.load();
    direction_out = _pending_boundary_direction.load();
    return true;
}

bool RhiRenderer::_transport_can_advance_unlocked(int global_frame)
{
    const auto prog = _transport.program();
    if (prog.slots.empty()) {
        return true;
    }
    for (const auto& slot : prog.slots) {
        const int decoder_frame =
            _transport.decoder_frame_for_source(slot.source_index, global_frame);
        if (decoder_frame < 0) {
            continue;
        }
        auto it = _caches.find(slot.source_index);
        if (it == _caches.end() || !it->second) {
            continue;
        }
        if (it->second->has_frame(decoder_frame)) {
            continue;
        }
        if (_displayCache.contains(slot.source_index, decoder_frame)) {
            continue;
        }
        if (_uploadQueue.has_job(slot.source_index, decoder_frame)) {
            continue;
        }
        return false;
    }
    return true;
}

bool RhiRenderer::_transport_can_advance(int global_frame)
{
    std::lock_guard<std::mutex> lock(_mutex);
    return _transport_can_advance_unlocked(global_frame);
}

void RhiRenderer::_apply_transport_frame_to_params(RenderParams& params, int global_frame)
{
    for (auto& slot : params.slots) {
        const int decoder_frame =
            _transport.decoder_frame_for_source(slot.source_index, global_frame);
        if (decoder_frame >= 0) {
            slot.frame_index = decoder_frame;
            slot.upload_token = decoder_frame;
        }
    }
}

void RhiRenderer::_update_transport_playheads(int global_frame, int direction)
{
    // Caller must hold _mutex. Only updates _pending_playheads — CacheManager
    // playhead writes happen after the lock is released (see _tick_transport_and_prepare).
    const auto prog = _transport.program();
    for (const auto& mapping : prog.slots) {
        const int decoder_frame =
            _transport.decoder_frame_for_source(mapping.source_index, global_frame);
        if (decoder_frame < 0) {
            continue;
        }
        SourcePlayhead ph;
        ph.playhead = decoder_frame;
        ph.direction = direction;
        ph.in_point = mapping.playback_in;
        ph.out_point = mapping.playback_out;
        _pending_playheads[mapping.source_index] = ph;
    }
}

void RhiRenderer::_emit_transport_frame_changed(int frame, int direction)
{
    // Lock-free coalesced notify — never acquire the GIL on the render thread.
    _pending_notify_frame.store(frame);
    _pending_notify_direction.store(direction);
    _frame_notify_pending.store(true);
    std::function<void(int, int)> cb;
    {
        std::lock_guard<std::mutex> lock(_mutex);
        cb = _frame_changed_callback;
    }
    // Optional callback for tests; production UI should poll instead.
    if (cb) {
        cb(frame, direction);
    }
}

void RhiRenderer::_emit_transport_segment_boundary(int frame, int direction)
{
    _pending_boundary_frame.store(frame);
    _pending_boundary_direction.store(direction);
    _boundary_notify_pending.store(true);
    std::function<void(int, int)> cb;
    {
        std::lock_guard<std::mutex> lock(_mutex);
        cb = _segment_boundary_callback;
    }
    if (cb) {
        cb(frame, direction);
    }
}

void RhiRenderer::_prepare_transport_program_and_slots()
{
    struct PlayheadApply {
        CacheManager* cache = nullptr;
        int playhead = 0;
        int direction = 1;
        int in_point = 0;
        int out_point = 0;
    };
    std::vector<PlayheadApply> playhead_applies;
    bool apply_audio = false;
    double audio_time = 0.0;
    bool audio_playing = false;
    int audio_direction = 1;
    {
        std::lock_guard<std::mutex> lock(_mutex);
        bool had_program = false;
        if (_transport_program_dirty.exchange(false)) {
            const bool was_playing = _pending_transport_program.playing;
            _transport.set_program(_pending_transport_program);
            if (was_playing) {
                _transport.play();
            }
            had_program = true;
        }

        if (!_transport.is_playing() && !had_program) {
            return;
        }

        _apply_transport_frame_to_params(
            _pending_render_params, _transport.current_frame());
        _apply_transport_frame_to_params(
            _active_render_params, _transport.current_frame());
        _render_params_dirty = true;
        _update_transport_playheads(_transport.current_frame(), _transport.direction());

        const auto prog = _transport.program();
        const int direction = _transport.direction();
        for (const auto& mapping : prog.slots) {
            const int decoder_frame =
                _transport.decoder_frame_for_source(
                    mapping.source_index, _transport.current_frame());
            if (decoder_frame < 0) {
                continue;
            }
            auto it = _caches.find(mapping.source_index);
            if (it == _caches.end() || !it->second) {
                continue;
            }
            playhead_applies.push_back(PlayheadApply{
                it->second,
                decoder_frame,
                direction,
                mapping.playback_in,
                mapping.playback_out});
        }

        apply_audio = true;
        audio_time = _audio_media_time_unlocked(_transport.current_frame());
        audio_playing = _transport.is_playing();
        audio_direction = _transport.direction();
    }

    for (const auto& apply : playhead_applies) {
        apply.cache->set_playhead(
            apply.playhead, apply.direction, apply.in_point, apply.out_point);
    }
    if (apply_audio) {
        _audio.sync_to_media_time(audio_time, audio_playing, audio_direction);
    }
}

void RhiRenderer::_advance_transport_after_present(TransportClock::TimePoint now)
{
    TransportAdvanceResult advanced;
    bool should_emit_move = false;
    bool should_emit_boundary = false;
    bool should_emit_stop = false;
    int emit_frame = 0;
    int emit_direction = 1;
    struct PlayheadApply {
        CacheManager* cache = nullptr;
        int playhead = 0;
        int direction = 1;
        int in_point = 0;
        int out_point = 0;
    };
    std::vector<PlayheadApply> playhead_applies;
    {
        std::lock_guard<std::mutex> lock(_mutex);
        if (!_transport.is_playing()) {
            return;
        }

        advanced = _transport.tick(
            now,
            [this](int global_frame) {
                return _transport_can_advance_unlocked(global_frame);
            });

        if (advanced.moved) {
            _apply_transport_frame_to_params(
                _pending_render_params, _transport.current_frame());
            _apply_transport_frame_to_params(
                _active_render_params, _transport.current_frame());
            _render_params_dirty = true;
            _update_transport_playheads(_transport.current_frame(), _transport.direction());

            const auto prog = _transport.program();
            const int direction = _transport.direction();
            for (const auto& mapping : prog.slots) {
                const int decoder_frame =
                    _transport.decoder_frame_for_source(
                        mapping.source_index, _transport.current_frame());
                if (decoder_frame < 0) {
                    continue;
                }
                auto it = _caches.find(mapping.source_index);
                if (it == _caches.end() || !it->second) {
                    continue;
                }
                playhead_applies.push_back(PlayheadApply{
                    it->second,
                    decoder_frame,
                    direction,
                    mapping.playback_in,
                    mapping.playback_out});
            }
        }

        emit_frame = advanced.frame;
        emit_direction = advanced.direction;
        should_emit_boundary = advanced.segment_boundary;
        should_emit_move = advanced.moved;
        should_emit_stop = advanced.stop;

        // Presentation-master → audio slave.
        {
            const double t = _audio_media_time_unlocked(_transport.current_frame());
            _audio.sync_to_media_time(
                t, _transport.is_playing(), _transport.direction());
        }
    }

    for (const auto& apply : playhead_applies) {
        apply.cache->set_playhead(
            apply.playhead, apply.direction, apply.in_point, apply.out_point);
    }

    if (should_emit_boundary) {
        _emit_transport_segment_boundary(emit_frame, emit_direction);
    } else if (should_emit_move || should_emit_stop) {
        _emit_transport_frame_changed(emit_frame, emit_direction);
    }
}

void RhiRenderer::_tick_transport_and_prepare()
{
    // Null/offscreen path: wall-clock advance then map playhead (pre-present).
    TransportAdvanceResult advanced;
    bool had_program = false;
    bool should_emit_move = false;
    bool should_emit_boundary = false;
    bool should_emit_stop = false;
    int emit_frame = 0;
    int emit_direction = 1;
    struct PlayheadApply {
        CacheManager* cache = nullptr;
        int playhead = 0;
        int direction = 1;
        int in_point = 0;
        int out_point = 0;
    };
    std::vector<PlayheadApply> playhead_applies;
    {
        std::lock_guard<std::mutex> lock(_mutex);
        if (_transport_program_dirty.exchange(false)) {
            const bool was_playing = _pending_transport_program.playing;
            _transport.set_program(_pending_transport_program);
            if (was_playing) {
                _transport.play();
            }
            had_program = true;
            _apply_transport_frame_to_params(
                _pending_render_params, _transport.current_frame());
            _render_params_dirty = true;
        }

        if (!_transport.is_playing() && !had_program) {
            return;
        }

        // Mutex is held: can_advance must use the unlocked helper.
        advanced = _transport.tick(
            TransportClock::Clock::now(),
            [this](int global_frame) {
                return _transport_can_advance_unlocked(global_frame);
            });

        if (advanced.moved || had_program) {
            _apply_transport_frame_to_params(
                _pending_render_params, _transport.current_frame());
            _apply_transport_frame_to_params(
                _active_render_params, _transport.current_frame());
            _render_params_dirty = true;
            _update_transport_playheads(_transport.current_frame(), _transport.direction());

            const auto prog = _transport.program();
            const int direction = _transport.direction();
            for (const auto& mapping : prog.slots) {
                const int decoder_frame =
                    _transport.decoder_frame_for_source(
                        mapping.source_index, _transport.current_frame());
                if (decoder_frame < 0) {
                    continue;
                }
                auto it = _caches.find(mapping.source_index);
                if (it == _caches.end() || !it->second) {
                    continue;
                }
                playhead_applies.push_back(PlayheadApply{
                    it->second,
                    decoder_frame,
                    direction,
                    mapping.playback_in,
                    mapping.playback_out});
            }
        }

        emit_frame = advanced.frame;
        emit_direction = advanced.direction;
        should_emit_boundary = advanced.segment_boundary;
        should_emit_move = advanced.moved;
        should_emit_stop = advanced.stop;

        {
            const double t = _audio_media_time_unlocked(_transport.current_frame());
            _audio.sync_to_media_time(
                t, _transport.is_playing(), _transport.direction());
        }
    }

    for (const auto& apply : playhead_applies) {
        apply.cache->set_playhead(
            apply.playhead, apply.direction, apply.in_point, apply.out_point);
    }

    if (should_emit_boundary) {
        _emit_transport_segment_boundary(emit_frame, emit_direction);
    } else if (should_emit_move || should_emit_stop) {
        _emit_transport_frame_changed(emit_frame, emit_direction);
    }
}

void RhiRenderer::set_display_cache_limit_gb(double limit_gb)
{
    std::lock_guard<std::mutex> lock(_mutex);
    _pending_limit_gb = limit_gb;
    _pending_limit_dirty = true;
    _wake_render_thread();
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
    _pending_playheads[source_index] = ph;
    _wake_render_thread();
}

void RhiRenderer::invalidate_display_cache_source(int source_index)
{
    std::lock_guard<std::mutex> lock(_mutex);
    _pending_invalidate_sources.push_back(source_index);
    _wake_render_thread();
}

GpuTextureCache::Stats RhiRenderer::get_display_cache_stats() const
{
    std::lock_guard<std::mutex> lock(_mutex);
    return _display_stats_snapshot;
}

std::vector<int> RhiRenderer::get_display_cached_frames(int source_index)
{
    std::lock_guard<std::mutex> lock(_mutex);
    auto it = _display_frames_snapshot.find(source_index);
    if (it == _display_frames_snapshot.end()) {
        return {};
    }
    return it->second;
}

void RhiRenderer::set_upload_queue_policy(UploadQueuePolicy policy)
{
    std::lock_guard<std::mutex> lock(_mutex);
    _uploadQueue.set_policy(policy);
}

UploadQueuePolicy RhiRenderer::upload_queue_policy() const
{
    std::lock_guard<std::mutex> lock(_mutex);
    return _uploadQueue.policy();
}

UploadQueueStats RhiRenderer::get_upload_queue_stats() const
{
    std::lock_guard<std::mutex> lock(_mutex);
    return _uploadQueue.stats();
}

void RhiRenderer::update_render_params(const RenderParams& params)
{
    std::lock_guard<std::mutex> lock(_mutex);
    _pending_render_params = params;
    _render_params_dirty = true;

    // Enqueue every visited slot so coalesced present params cannot skip puts.
    // Residency is checked against the render-thread snapshot (safe under _mutex).
    for (const auto& slot : params.slots) {
        if (slot.frame_index < 0) {
            continue;
        }
        bool resident = false;
        auto snap = _display_frames_snapshot.find(slot.source_index);
        if (snap != _display_frames_snapshot.end()) {
            const auto& frames = snap->second;
            resident = std::binary_search(frames.begin(), frames.end(), slot.frame_index);
        }
        _uploadQueue.enqueue(
            UploadJobRequest{slot.source_index, slot.frame_index, slot.upload_token},
            resident);
    }

    _wake_render_thread();
}

void RhiRenderer::request_redraw()
{
    _wake_render_thread();
}

RhiRenderer::DebugStats RhiRenderer::get_debug_stats() const
{
    DebugStats stats = _debug_stats;
    stats.pipeline_lut_count = _pipeline_lut_count;
    return stats;
}

int RhiRenderer::pipeline_lut_count() const
{
    std::lock_guard<std::mutex> lock(_mutex);
    return _pipeline_lut_count;
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

    apply_pending_display_cache_ops();

    if (_clear_cache_pending.exchange(false)) {
        if (!_displayCache.enabled()) {
            if (_texAState.texture && _texAState.texture != _placeholderTex2D) {
                _texAState.texture->destroy();
                delete _texAState.texture;
            }
            if (_texBState.texture && _texBState.texture != _placeholderTex2D) {
                _texBState.texture->destroy();
                delete _texBState.texture;
            }
        }
        {
            std::lock_guard<std::mutex> lock(_mutex);
            _uploadQueue.clear_with([this](void* tex) { destroy_upload_texture(tex); });
            _displayCache.clear();
            // Keep _caches — registry is owned by register_cache only.
            _display_frames_snapshot.clear();
            _display_stats_snapshot = GpuTextureCache::Stats{};
        }
        _texAState.texture = nullptr;
        _texBState.texture = nullptr;
        _last_bound_tex_a = nullptr;
        _last_bound_tex_b = nullptr;
        for (auto& state : _texturePool) {
            state.texture = nullptr;
        }
        clear_tile_srb_cache();
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
    bool lut_count_changed = false;
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
        if (_ocio_lut_dims_dirty) {
            _ocio_lut_slot_dims = _pending_ocio_lut_dims;
            _ocio_lut_dims_dirty = false;
            // Exact count (may shrink) so leftover LUT placeholders cannot steal UBO bindings.
            const int needed = static_cast<int>(_ocio_lut_slot_dims.size());
            if (needed != _pipeline_lut_count) {
                _pipeline_lut_count = needed;
                lut_count_changed = true;
            }
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
                    _active_ocio_luts.push_back(OcioLut());
                }
                auto& lut = _active_ocio_luts[pending.index];
                lut.is_3d = pending.is_3d;
                lut.size = pending.size;
                lut.width = pending.width;
                lut.height = pending.height;
                lut.channels = pending.channels;
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

    if ((shaders_dirty && !frag_src_for_layout.empty()) || lut_count_changed) {
        if (shaders_dirty && !frag_src_for_layout.empty()) {
            parse_ocio_ubo_layout(frag_src_for_layout);
        }
        clear_tile_srb_cache();
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
    CacheManager* previous = nullptr;
    {
        std::lock_guard<std::mutex> lock(_mutex);
        auto it = _caches.find(source_index);
        if (it != _caches.end()) {
            previous = it->second;
        }
        _caches[source_index] = cache;
    }
    if (previous && previous != cache) {
        HwFrameDispatch::unbind(previous);
    }
    if (!cache) {
        return;
    }
    HwFrameDispatch::bind(
        cache,
        [this, source_index](int decoder_frame, HwFrameTicket ticket) -> bool {
            std::lock_guard<std::mutex> lock(_mutex);
            if (!_hw_import_ready()) {
                return false;
            }
            const bool already = _displayCache.contains(source_index, decoder_frame);
            UploadJobRequest req;
            req.source_index = source_index;
            req.decoder_frame = decoder_frame;
            req.upload_token = decoder_frame;
            req.kind = UploadJobKind::HwImport;
            req.width = ticket.width();
            req.height = ticket.height();
            req.channels = 4;
            if (!_uploadQueue.enqueue_hw(req, std::move(ticket), already)) {
                return already; // already resident counts as accepted
            }
            _log_movie_present_mode(_hw_import_mode_name());
            _wake_render_thread();
            return true;
        });
}

void RhiRenderer::set_shader_sources(const std::string& pipeline_key, const std::string& vert_src, const std::string& frag_src)
{
    std::lock_guard<std::mutex> lock(_mutex);
    // Key alone is not enough: OCIO config edits can reuse a look name while the
    // generated GLSL changes. Also compare sources so we never keep a stale bake.
    if (_cached_pipeline_key == pipeline_key
        && _pending_frag_src_for_layout == frag_src) {
        return;
    }
    
    // Bake shaders on the calling thread (CPU-only). GPU resource setup
    // (Ocio UBO, pipeline) happens in sync_and_render() on the GUI thread.
    if (bake_shaders(pipeline_key, vert_src, frag_src)) {
        _cached_pipeline_key = pipeline_key;
        _pending_frag_src_for_layout = frag_src;
        _shaders_dirty = true;
        _render_params_dirty = true;
        _wake_render_thread();
    }
}

std::string RhiRenderer::cached_pipeline_key() const
{
    std::lock_guard<std::mutex> lock(_mutex);
    return _cached_pipeline_key;
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
    PendingOcioLut lut;
    lut.index = index;
    lut.is_3d = true;
    lut.size = size;
    lut.rgba_data.resize(static_cast<size_t>(size) * size * size * 4);
    
    // Convert RGB to RGBA
    for (int i = 0; i < size * size * size; ++i) {
        lut.rgba_data[static_cast<size_t>(i) * 4 + 0] = data[static_cast<size_t>(i) * 3 + 0];
        lut.rgba_data[static_cast<size_t>(i) * 4 + 1] = data[static_cast<size_t>(i) * 3 + 1];
        lut.rgba_data[static_cast<size_t>(i) * 4 + 2] = data[static_cast<size_t>(i) * 3 + 2];
        lut.rgba_data[static_cast<size_t>(i) * 4 + 3] = 1.0f;
    }
    _pending_ocio_luts.push_back(std::move(lut));
    _ocio_luts_dirty = true;
    _wake_render_thread();
}

void RhiRenderer::upload_ocio_lut_2d(
    int index,
    int width,
    int height,
    int channels,
    const std::vector<float>& data)
{
    if (width <= 0 || height <= 0 || channels <= 0) {
        return;
    }
    std::lock_guard<std::mutex> lock(_mutex);
    PendingOcioLut lut;
    lut.index = index;
    lut.is_3d = false;
    lut.width = width;
    lut.height = height;
    lut.channels = channels;
    const int pixels = width * height;
    lut.rgba_data.resize(static_cast<size_t>(pixels) * 4);
    for (int i = 0; i < pixels; ++i) {
        if (channels == 1) {
            const float r = data[static_cast<size_t>(i)];
            lut.rgba_data[static_cast<size_t>(i) * 4 + 0] = r;
            lut.rgba_data[static_cast<size_t>(i) * 4 + 1] = r;
            lut.rgba_data[static_cast<size_t>(i) * 4 + 2] = r;
            lut.rgba_data[static_cast<size_t>(i) * 4 + 3] = 1.0f;
        } else if (channels == 3) {
            lut.rgba_data[static_cast<size_t>(i) * 4 + 0] = data[static_cast<size_t>(i) * 3 + 0];
            lut.rgba_data[static_cast<size_t>(i) * 4 + 1] = data[static_cast<size_t>(i) * 3 + 1];
            lut.rgba_data[static_cast<size_t>(i) * 4 + 2] = data[static_cast<size_t>(i) * 3 + 2];
            lut.rgba_data[static_cast<size_t>(i) * 4 + 3] = 1.0f;
        } else {
            // Assume RGBA (or pad from available channels)
            const int src_ch = std::min(channels, 4);
            for (int c = 0; c < 4; ++c) {
                lut.rgba_data[static_cast<size_t>(i) * 4 + c] =
                    (c < src_ch) ? data[static_cast<size_t>(i) * channels + c] : (c == 3 ? 1.0f : 0.0f);
            }
        }
    }
    _pending_ocio_luts.push_back(std::move(lut));
    _ocio_luts_dirty = true;
    _wake_render_thread();
}

void RhiRenderer::set_ocio_lut_slot_dims(const std::vector<std::string>& dims)
{
    std::lock_guard<std::mutex> lock(_mutex);
    _pending_ocio_lut_dims = dims;
    _ocio_lut_dims_dirty = true;
    _wake_render_thread();
}

void RhiRenderer::clear_ocio_luts()
{
    std::lock_guard<std::mutex> lock(_mutex);
    _pending_ocio_luts.clear();
    _pending_ocio_lut_dims.clear();
    _ocio_lut_dims_dirty = true;
    _ocio_luts_dirty = true;
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
    _debug_stats.last_upload_jobs = 0;
    _reset_present_upload_budget();

    QRhi::FrameOpResult result = _rhi->beginFrame(_swapChain);
    if (result != QRhi::FrameOpSuccess) {
        _debug_stats.begin_frame_fail++;
        return;
    }
    _debug_stats.begin_frame_ok++;

    QRhiResourceUpdateBatch* batch = _rhi->nextResourceUpdateBatch();
    
    update_rhi_resources(batch);

    const auto upload_start = Clock::now();

    // Complete prior GPU uploads, put into display cache, then submit new jobs.
    {
        std::lock_guard<std::mutex> lock(_mutex);
        if (_completed_upload_generation > 0) {
            _uploadQueue.complete_generation(_completed_upload_generation);
        }
    }
    put_ready_upload_jobs();
    enqueue_gpu_lookahead();
    drain_upload_queue(batch);

    // Present: bind from display cache (or sync upload when cache disabled).
    const int compare_mode = _active_render_params.compare_mode;
    bool bindings_dirty = false;
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
            bool have_dims = false;
            {
                std::lock_guard<std::mutex> lock(_mutex);
                have_dims = _resolve_slot_dimensions(
                    slot.source_index, slot.frame_index, cpu_cache, w, h, channels);
            }
            if (have_dims) {
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
                        bindings_dirty);
                } else if (compare_mode == 0) {
                    if (slot.source_index == _active_render_params.sequence_index) {
                        resolve_display_texture(
                            slot.source_index,
                            cpu_cache,
                            slot,
                            _texAState,
                            batch,
                            bindings_dirty);
                    }
                } else {
                    if (slot.source_index == 0) {
                        resolve_display_texture(
                            slot.source_index,
                            cpu_cache,
                            slot,
                            _texAState,
                            batch,
                            bindings_dirty);
                    } else if (slot.source_index == 1) {
                        resolve_display_texture(
                            slot.source_index,
                            cpu_cache,
                            slot,
                            _texBState,
                            batch,
                            bindings_dirty);
                    }
                }
            } else {
                _debug_stats.cache_misses++;
            }
        }
    }

    {
        auto display_stats = _displayCache.stats();
        _debug_stats.gpu_cache_hits = display_stats.hits;
        _debug_stats.gpu_cache_misses = display_stats.misses;
        if (_displayCache.enabled()) {
            _debug_stats.textures_created = display_stats.textures_created;
            _debug_stats.textures_pooled_reuses = display_stats.textures_pooled_reuses;
        }
        std::lock_guard<std::mutex> lock(_mutex);
        _display_stats_snapshot = display_stats;
    }

    if (bindings_dirty || _texAState.texture != _last_bound_tex_a || _texBState.texture != _last_bound_tex_b) {
        update_srb_resources();
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

    // 2. Upload OCIO LUTs (1D-as-2D and 3D)
    for (auto& lut : _active_ocio_luts) {
        if (!lut.dirty) {
            continue;
        }
        if (lut.is_3d && lut.size > 0) {
            upload_texture_3d(lut, batch);
            lut.dirty = false;
        } else if (!lut.is_3d && lut.width > 0 && lut.height > 0) {
            upload_texture_2d_lut(lut, batch);
            lut.dirty = false;
        }
    }

    // 3. Update dynamic uniform buffers (packed before beginPass)
    const bool tile_compare = (_active_render_params.compare_mode == 3);
    if (tile_compare) {
        const int slot_count = std::max(1, static_cast<int>(_active_render_params.tiles.size()));
        ensure_per_frame_ubo(slot_count);
        const quint32 stride = per_frame_ubo_stride();
        std::vector<char> uboBlob(static_cast<size_t>(slot_count) * stride, 0);
        for (int i = 0; i < static_cast<int>(_active_render_params.tiles.size()); ++i) {
            const auto& tile = _active_render_params.tiles[i];
            PerFrameUboData tileFrame;
            tileFrame.scale_x = tile.scale_x;
            tileFrame.scale_y = tile.scale_y;
            tileFrame.pan_x = tile.offset_x;
            tileFrame.pan_y = tile.offset_y;
            tileFrame.compare_mode = 0;
            tileFrame.wipe_pos = 0.5f;
            tileFrame.channel_mask = _active_render_params.channel_mask;
            tileFrame.false_color_mode = _active_render_params.false_color_mode;
            tileFrame.zebra_lo = _active_render_params.zebra_lo;
            tileFrame.zebra_hi = _active_render_params.zebra_hi;
            tileFrame.pad0 = 0.0f;
            tileFrame.pad1 = 0.0f;
            std::memcpy(uboBlob.data() + static_cast<size_t>(i) * stride, &tileFrame, sizeof(tileFrame));
        }
        if (_perFrameUbo) {
            batch->updateDynamicBuffer(_perFrameUbo, 0, uboBlob.size(), uboBlob.data());
        }
    } else {
        ensure_per_frame_ubo(1);
        PerFrameUboData perFrame;
        perFrame.scale_x = _active_render_params.scale_x;
        perFrame.scale_y = _active_render_params.scale_y;
        perFrame.pan_x = _active_render_params.pan_x;
        perFrame.pan_y = _active_render_params.pan_y;
        perFrame.compare_mode = _active_render_params.compare_mode;
        perFrame.wipe_pos = _active_render_params.wipe_pos;
        perFrame.channel_mask = _active_render_params.channel_mask;
        perFrame.false_color_mode = _active_render_params.false_color_mode;
        perFrame.zebra_lo = _active_render_params.zebra_lo;
        perFrame.zebra_hi = _active_render_params.zebra_hi;
        perFrame.pad0 = 0.0f;
        perFrame.pad1 = 0.0f;
        if (_perFrameUbo) {
            batch->updateDynamicBuffer(_perFrameUbo, 0, sizeof(perFrame), &perFrame);
        }
    }

    if (_ocioUbo && _ocioUboLayout.size > 0) {
        std::vector<char> ocioData = pack_ocio_ubo();
        batch->updateDynamicBuffer(_ocioUbo, 0, ocioData.size(), ocioData.data());
    }

    // 4. Ensure graphics pipeline is valid for this render pass
    build_pipeline(_swapChain->renderPassDescriptor());

    const auto draw_start = Clock::now();
    bool drew_this_frame = false;

    QRhiCommandBuffer* cb = _swapChain->currentFrameCommandBuffer();
    QRhiRenderTarget* rt = _swapChain->currentFrameRenderTarget();
    QColor clearColor(0, 0, 0, 1);

    if (tile_compare) {
        // Tile Layout Compare Mode: Multiple Draw passes, shared pipeline + per-tile SRBs
        cb->beginPass(rt, clearColor, {1.0f, 0}, batch);
        
        QSize outputSize = rt->pixelSize();
        cb->setViewport(QRhiViewport(0, 0, outputSize.width(), outputSize.height()));

        const quint32 stride = per_frame_ubo_stride();
        bool drew_tile = false;
        for (int i = 0; i < static_cast<int>(_active_render_params.tiles.size()); ++i) {
            const auto& tile = _active_render_params.tiles[i];
            if (tile.source_index < 0 || tile.source_index >= static_cast<int>(_texturePool.size())) {
                continue;
            }
            auto& texState = _texturePool[tile.source_index];
            if (!texState.texture) {
                continue;
            }

            QRhiShaderResourceBindings* tileSrb = get_or_create_tile_srb(texState.texture);
            if (!_pipeline || !tileSrb) {
                continue;
            }

            cb->setGraphicsPipeline(_pipeline);
            QRhiCommandBuffer::DynamicOffset dynOffset{0, static_cast<quint32>(i) * stride};
            cb->setShaderResources(tileSrb, 1, &dynOffset);
            
            QRhiCommandBuffer::VertexInput vInput(_vertexBuffer, 0);
            cb->setVertexInput(0, 1, &vInput);
            
            cb->draw(4);
            drew_tile = true;
        }
        cb->endPass();
        if (drew_tile) {
            _debug_stats.frames_drawn++;
            drew_this_frame = true;
        } else {
            _debug_stats.frames_cleared_only++;
        }
    } else {
        // Normal rendering pass
        cb->beginPass(rt, clearColor, {1.0f, 0}, batch);
        if (_pipeline && _texAState.texture) {
            QSize outputSize = rt->pixelSize();
            cb->setViewport(QRhiViewport(0, 0, outputSize.width(), outputSize.height()));
            cb->setGraphicsPipeline(_pipeline);
            QRhiCommandBuffer::DynamicOffset dynOffset{0, 0};
            cb->setShaderResources(_srb, 1, &dynOffset);
            
            QRhiCommandBuffer::VertexInput vInput(_vertexBuffer, 0);
            cb->setVertexInput(0, 1, &vInput);
            
            cb->draw(4);
            _debug_stats.frames_drawn++;
            drew_this_frame = true;
        } else {
            _debug_stats.frames_cleared_only++;
        }
        cb->endPass();
    }

    _debug_stats.pipeline_ready = _pipeline != nullptr;
    bool present_ok = false;
    {
        const auto end_frame_start = Clock::now();
        const QRhi::FrameOpResult end_result = _rhi->endFrame(_swapChain);
        present_ok = (end_result == QRhi::FrameOpSuccess);
        const auto end_frame_end = Clock::now();
        const double end_ms =
            std::chrono::duration<double, std::milli>(end_frame_end - end_frame_start).count();
        _debug_stats.last_end_frame_ms = end_ms;
        if (end_ms > _debug_stats.end_frame_ms_max) {
            _debug_stats.end_frame_ms_max = end_ms;
        }
    }

    // Real swapchain: TransportClock advances from successful present timestamps.
    // Null/offscreen already ticked before present in the render loop.
    if (present_ok && !_is_fallback_null_backend.load() && _transport.is_playing()) {
        _advance_transport_after_present(Clock::now());
    }

    // Staging/GPU uploads submitted this frame are safe to treat as complete next frame.
    _completed_upload_generation = _upload_generation;
    ++_upload_generation;
    publish_display_cache_snapshot();
    if (_window) {
        _window->requestUpdate();
    }

    const auto frame_end = Clock::now();
    _debug_stats.last_upload_ms = std::chrono::duration<double, std::milli>(upload_end - upload_start).count();
    _debug_stats.last_draw_ms = std::chrono::duration<double, std::milli>(frame_end - draw_start).count();
    _debug_stats.last_render_ms = std::chrono::duration<double, std::milli>(frame_end - frame_start).count();
    _debug_stats.last_upload_jobs = _debug_stats.last_upload_count;
    _debug_stats.upload_ms_total += _debug_stats.last_upload_ms;

    // Idle GPU warming: keep rendering while the display cache can still absorb
    // CPU-cached frames ahead of the playhead.
    if (gpu_warmup_work_remaining()) {
        _wake_render_thread();
    }
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

    ensure_per_frame_ubo(1);

    if (!_sampler) {
        _sampler = _rhi->newSampler(QRhiSampler::Linear, QRhiSampler::Linear, QRhiSampler::None, QRhiSampler::ClampToEdge, QRhiSampler::ClampToEdge);
        _sampler->create();
    }

    if (!_lutSampler) {
        _lutSampler = _rhi->newSampler(QRhiSampler::Linear, QRhiSampler::Linear, QRhiSampler::None, QRhiSampler::ClampToEdge, QRhiSampler::ClampToEdge);
        _lutSampler->create();
    }

    if (!_placeholderTex2D) {
        _placeholderTex2D = _rhi->newTexture(QRhiTexture::RGBA16F, QSize(1, 1));
        _placeholderTex2D->create();
        if (batch) {
            static const uint16_t kBlackRgba16F[4] = {0, 0, 0, 0};
            QByteArray arr = QByteArray::fromRawData(
                reinterpret_cast<const char*>(kBlackRgba16F),
                static_cast<int>(sizeof(kBlackRgba16F)));
            batch->uploadTexture(
                _placeholderTex2D,
                QRhiTextureUploadDescription(
                    QRhiTextureUploadEntry(0, 0, QRhiTextureSubresourceUploadDescription(arr))));
        }
    }

    if (!_placeholderLut3D) {
        _placeholderLut3D = _rhi->newTexture(QRhiTexture::RGBA32F, 1, 1, 1);
        _placeholderLut3D->create();
        if (batch) {
            static const float kBlackRgba32F[4] = {0.0f, 0.0f, 0.0f, 0.0f};
            QByteArray arr = QByteArray::fromRawData(
                reinterpret_cast<const char*>(kBlackRgba32F),
                static_cast<int>(sizeof(kBlackRgba32F)));
            batch->uploadTexture(
                _placeholderLut3D,
                QRhiTextureUploadDescription(
                    QRhiTextureUploadEntry(0, 0, QRhiTextureSubresourceUploadDescription(arr))));
        }
    }

    if (!_placeholderLut2D) {
        _placeholderLut2D = _rhi->newTexture(QRhiTexture::RGBA32F, QSize(1, 1));
        _placeholderLut2D->create();
        if (batch) {
            static const float kBlackRgba32F2D[4] = {0.0f, 0.0f, 0.0f, 0.0f};
            QByteArray arr = QByteArray::fromRawData(
                reinterpret_cast<const char*>(kBlackRgba32F2D),
                static_cast<int>(sizeof(kBlackRgba32F2D)));
            batch->uploadTexture(
                _placeholderLut2D,
                QRhiTextureUploadDescription(
                    QRhiTextureUploadEntry(0, 0, QRhiTextureSubresourceUploadDescription(arr))));
        }
    }

    if (!_texAState.texture) {
        _texAState.texture = _placeholderTex2D;
    }
    if (!_texBState.texture) {
        _texBState.texture = _placeholderTex2D;
    }
}

std::vector<QRhiShaderResourceBinding> RhiRenderer::build_srb_bindings(
    QRhiTexture* tex_a,
    QRhiTexture* tex_b) const
{
    std::vector<QRhiShaderResourceBinding> bindings;
    if (!_perFrameUbo) {
        return bindings;
    }

    bindings.push_back(QRhiShaderResourceBinding::uniformBufferWithDynamicOffset(
        0,
        QRhiShaderResourceBinding::VertexStage | QRhiShaderResourceBinding::FragmentStage,
        _perFrameUbo,
        static_cast<quint32>(sizeof(PerFrameUboData))));

    QRhiTexture* a = tex_a ? tex_a : _placeholderTex2D;
    QRhiTexture* b = tex_b ? tex_b : _placeholderTex2D;
    if (a && _sampler) {
        bindings.push_back(QRhiShaderResourceBinding::sampledTexture(
            1, QRhiShaderResourceBinding::FragmentStage, a, _sampler));
    }
    if (b && _sampler) {
        bindings.push_back(QRhiShaderResourceBinding::sampledTexture(
            2, QRhiShaderResourceBinding::FragmentStage, b, _sampler));
    }

    for (int i = 0; i < _pipeline_lut_count; ++i) {
        const bool slot_is_3d = [&]() {
            if (i < static_cast<int>(_ocio_lut_slot_dims.size())) {
                return _ocio_lut_slot_dims[static_cast<size_t>(i)] != "2D";
            }
            if (i < static_cast<int>(_active_ocio_luts.size())) {
                return _active_ocio_luts[static_cast<size_t>(i)].is_3d;
            }
            return true;
        }();
        QRhiTexture* lutTex = slot_is_3d ? _placeholderLut3D : _placeholderLut2D;
        if (i < static_cast<int>(_active_ocio_luts.size()) && _active_ocio_luts[static_cast<size_t>(i)].texture) {
            lutTex = _active_ocio_luts[static_cast<size_t>(i)].texture;
        }
        if (lutTex && _lutSampler) {
            bindings.push_back(QRhiShaderResourceBinding::sampledTexture(
                3 + i, QRhiShaderResourceBinding::FragmentStage, lutTex, _lutSampler));
        }
    }

    if (_ocioUbo && _ocioUboLayout.binding >= 0) {
        bindings.push_back(QRhiShaderResourceBinding::uniformBuffer(
            _ocioUboLayout.binding,
            QRhiShaderResourceBinding::FragmentStage,
            _ocioUbo));
    }

    return bindings;
}

void RhiRenderer::clear_tile_srb_cache()
{
    for (auto& pair : _tileSrbCache) {
        if (pair.second) {
            pair.second->destroy();
            delete pair.second;
        }
    }
    _tileSrbCache.clear();
}

QRhiShaderResourceBindings* RhiRenderer::get_or_create_tile_srb(QRhiTexture* tex_a)
{
    if (!_rhi || !tex_a) {
        return nullptr;
    }

    auto it = _tileSrbCache.find(tex_a);
    if (it != _tileSrbCache.end() && it->second) {
        return it->second;
    }

    auto bindings = build_srb_bindings(tex_a, _texBState.texture);
    QRhiShaderResourceBindings* srb = _rhi->newShaderResourceBindings();
    srb->setBindings(bindings.begin(), bindings.end());
    if (!srb->create()) {
        QStringList indices{QStringLiteral("0"), QStringLiteral("1"), QStringLiteral("2")};
        for (int i = 0; i < _pipeline_lut_count; ++i) {
            indices << QString::number(3 + i);
        }
        if (_ocioUboLayout.binding >= 0) {
            indices << QString::number(_ocioUboLayout.binding);
        }
        qWarning() << "RhiRenderer: tile SRB create failed; bindings:" << indices.join(',')
                   << "ocioUboBinding:" << _ocioUboLayout.binding
                   << "lutCount:" << _pipeline_lut_count
                   << "bindingCount:" << bindings.size();
        delete srb;
        return nullptr;
    }
    _tileSrbCache[tex_a] = srb;
    return srb;
}

quint32 RhiRenderer::per_frame_ubo_stride() const
{
    const quint32 size = static_cast<quint32>(sizeof(PerFrameUboData));
    if (!_rhi) {
        return size;
    }
    const quint32 align = static_cast<quint32>(_rhi->ubufAlignment());
    if (align <= 1) {
        return size;
    }
    return (size + align - 1u) / align * align;
}

void RhiRenderer::ensure_per_frame_ubo(int slot_count)
{
    if (!_rhi || slot_count <= 0) {
        return;
    }
    if (_perFrameUbo && _perFrameUboSlots >= slot_count) {
        return;
    }

    const bool had_ubo = (_perFrameUbo != nullptr);
    if (_perFrameUbo) {
        _perFrameUbo->destroy();
        delete _perFrameUbo;
        _perFrameUbo = nullptr;
    }

    const quint32 stride = per_frame_ubo_stride();
    _perFrameUbo = _rhi->newBuffer(
        QRhiBuffer::Dynamic,
        QRhiBuffer::UniformBuffer,
        static_cast<quint32>(slot_count) * stride);
    _perFrameUbo->create();
    _perFrameUboSlots = slot_count;

    // SRB holds the buffer pointer — force a full rebuild when replacing.
    if (had_ubo && _swapChain) {
        build_pipeline(_swapChain->renderPassDescriptor(), true);
    }
}

void RhiRenderer::update_srb_resources()
{
    if (!_srb) {
        if (_swapChain) {
            build_pipeline(_swapChain->renderPassDescriptor());
        }
        return;
    }

    auto bindings = build_srb_bindings(_texAState.texture, _texBState.texture);
    _srb->setBindings(bindings.begin(), bindings.end());
    _srb->updateResources();
    _debug_stats.srb_updates++;
    _last_bound_tex_a = _texAState.texture;
    _last_bound_tex_b = _texBState.texture;
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
    clear_tile_srb_cache();

    _pipeline_lut_count = static_cast<int>(_ocio_lut_slot_dims.size());
    if (_pipeline_lut_count == 0 && !_active_ocio_luts.empty()) {
        // Fallback when dims were not set (legacy callers).
        _pipeline_lut_count = static_cast<int>(_active_ocio_luts.size());
    }

    auto bindings = build_srb_bindings(_texAState.texture, _texBState.texture);

    _srb = _rhi->newShaderResourceBindings();
    _srb->setBindings(bindings.begin(), bindings.end());
    if (!_srb->create()) {
        QStringList indices{QStringLiteral("0"), QStringLiteral("1"), QStringLiteral("2")};
        for (int i = 0; i < _pipeline_lut_count; ++i) {
            indices << QString::number(3 + i);
        }
        if (_ocioUboLayout.binding >= 0) {
            indices << QString::number(_ocioUboLayout.binding);
        }
        qWarning() << "RhiRenderer: SRB create failed; bindings:" << indices.join(',')
                   << "ocioUboBinding:" << _ocioUboLayout.binding
                   << "lutCount:" << _pipeline_lut_count
                   << "bindingCount:" << bindings.size();
    }

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

    _debug_stats.pipeline_rebuilds++;
    _last_bound_tex_a = _texAState.texture;
    _last_bound_tex_b = _texBState.texture;
}

bool RhiRenderer::bake_shaders(
    const std::string& pipeline_key,
    const std::string& vert_src,
    const std::string& frag_src)
{
    // Disk cache: .../framecycler/qsb/<pipeline_key>_<fragHash>/{vert,frag}.qsb
    // Fragment hash prevents loading a stale QSB when pipeline_key is reused but
    // OCIO-generated GLSL changed (e.g. look definition edited on disk).
    const QByteArray fragHash = QCryptographicHash::hash(
        QByteArray::fromStdString(frag_src), QCryptographicHash::Sha256).toHex().left(16);
    QString cacheDir;
    if (!pipeline_key.empty()) {
        const QString base = QStandardPaths::writableLocation(QStandardPaths::CacheLocation);
        cacheDir = base + QStringLiteral("/framecycler/qsb/")
            + QString::fromStdString(pipeline_key) + QLatin1Char('_')
            + QString::fromUtf8(fragHash);
        const QString vertPath = cacheDir + QStringLiteral("/vert.qsb");
        const QString fragPath = cacheDir + QStringLiteral("/frag.qsb");
        QFile vertFile(vertPath);
        QFile fragFile(fragPath);
        if (vertFile.open(QIODevice::ReadOnly) && fragFile.open(QIODevice::ReadOnly)) {
            const QByteArray vertBlob = vertFile.readAll();
            const QByteArray fragBlob = fragFile.readAll();
            QShader vertShader = QShader::fromSerialized(vertBlob);
            QShader fragShader = QShader::fromSerialized(fragBlob);
            if (vertShader.isValid() && fragShader.isValid()) {
                _vertexShader = vertShader;
                _fragmentShader = fragShader;
                return true;
            }
        }
    }

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

    if (!cacheDir.isEmpty()) {
        QDir().mkpath(cacheDir);
        const QByteArray vertBlob = vertShader.serialized();
        const QByteArray fragBlob = fragShader.serialized();
        QFile vertFile(cacheDir + QStringLiteral("/vert.qsb"));
        QFile fragFile(cacheDir + QStringLiteral("/frag.qsb"));
        if (vertFile.open(QIODevice::WriteOnly | QIODevice::Truncate)) {
            vertFile.write(vertBlob);
        }
        if (fragFile.open(QIODevice::WriteOnly | QIODevice::Truncate)) {
            fragFile.write(fragBlob);
        }
    }
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

    // Seed identity defaults so a missing uniform write cannot desaturate (sat=0)
    // or crush contrast (gamma=0). Live grading params overlay these next.
    auto write_float = [&](const char* name, float val) {
        auto it = _ocioUboLayout.members.find(name);
        if (it == _ocioUboLayout.members.end() || it->second.is_vec3) {
            return;
        }
        std::memcpy(buf.data() + it->second.offset, &val, sizeof(float));
    };
    auto write_vec3 = [&](const char* name, float x, float y, float z) {
        auto it = _ocioUboLayout.members.find(name);
        if (it == _ocioUboLayout.members.end() || !it->second.is_vec3) {
            return;
        }
        const float v[3] = {x, y, z};
        std::memcpy(buf.data() + it->second.offset, v, sizeof(float) * 3);
    };

    write_float("ocio_exposure_contrast_exposureVal", 0.0f);
    write_float("ocio_exposure_contrast_gammaVal", 1.0f);
    write_vec3("ocio_grading_primary_brightness", 0.0f, 0.0f, 0.0f);
    write_vec3("ocio_grading_primary_contrast", 1.0f, 1.0f, 1.0f);
    write_vec3("ocio_grading_primary_gamma", 1.0f, 1.0f, 1.0f);
    write_float("ocio_grading_primary_saturation", 1.0f);
    write_float("ocio_grading_primary_localBypass", 0.0f);
    write_vec3("fc_cdl_slope", 1.0f, 1.0f, 1.0f);
    write_vec3("fc_cdl_offset", 0.0f, 0.0f, 0.0f);
    write_vec3("fc_cdl_power", 1.0f, 1.0f, 1.0f);
    write_float("fc_cdl_saturation", 1.0f);
    write_float("fc_cdl_enable", 0.0f);

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

void RhiRenderer::apply_pending_display_cache_ops()
{
    std::lock_guard<std::mutex> lock(_mutex);
    if (_pending_limit_dirty) {
        _displayCache.set_limit_gb(_pending_limit_gb);
        _pending_limit_dirty = false;
    }
    for (const auto& pair : _pending_playheads) {
        _displayCache.set_source_playhead(pair.first, pair.second);
    }
    _pending_playheads.clear();
    if (!_pending_invalidate_sources.empty()) {
        for (int source_index : _pending_invalidate_sources) {
            _displayCache.invalidate_source(source_index);
        }
        _pending_invalidate_sources.clear();
        _uploadQueue.clear_with([this](void* tex) { destroy_upload_texture(tex); });
    }
}

void RhiRenderer::publish_display_cache_snapshot()
{
    std::unordered_map<int, std::vector<int>> snapshot;
    // Collect source indices from registered caches + known playheads.
    std::vector<int> sources;
    {
        std::lock_guard<std::mutex> lock(_mutex);
        for (const auto& pair : _caches) {
            sources.push_back(pair.first);
        }
    }
    for (int source_index : sources) {
        snapshot[source_index] = _displayCache.cached_frames_for_source(source_index);
    }
    std::lock_guard<std::mutex> lock(_mutex);
    _display_frames_snapshot = std::move(snapshot);
    _display_stats_snapshot = _displayCache.stats();
}

void RhiRenderer::destroy_upload_texture(void* texture)
{
    auto* tex = static_cast<QRhiTexture*>(texture);
    if (!tex) {
        return;
    }
    tex->destroy();
    delete tex;
}

std::optional<size_t> RhiRenderer::acquire_staging_slot()
{
    if (_stagingRing.empty()) {
        _debug_stats.staging_waits++;
        return std::nullopt;
    }

    const size_t n = _stagingRing.size();
    for (size_t attempt = 0; attempt < n; ++attempt) {
        const size_t i = (_stagingRingIndex + attempt) % n;
        if (_stagingGeneration[i] != 0 && _stagingGeneration[i] > _completed_upload_generation) {
            continue;
        }
        _stagingRingIndex = (i + 1) % n;
        _stagingGeneration[i] = _upload_generation;
        return i;
    }

    _debug_stats.staging_waits++;
    return std::nullopt;
}

QRhiTexture* RhiRenderer::acquire_frame_texture(int width, int height, int channels)
{
    if (!_rhi || width <= 0 || height <= 0) {
        return nullptr;
    }

    const QRhiTexture::Format format = (channels == 1) ? QRhiTexture::R16F : QRhiTexture::RGBA16F;
    if (_displayCache.enabled()) {
        QRhiTexture* tex = _displayCache.acquire(width, height, format);
        auto stats = _displayCache.stats();
        _debug_stats.textures_created = stats.textures_created;
        _debug_stats.textures_pooled_reuses = stats.textures_pooled_reuses;
        return tex;
    }

    QRhiTexture* texture = _rhi->newTexture(format, QSize(width, height));
    if (!texture || !texture->create()) {
        delete texture;
        return nullptr;
    }
    _debug_stats.textures_created++;
    return texture;
}

QRhiTexture* RhiRenderer::acquire_hw_import_texture(int width, int height)
{
    if (!_rhi || width <= 0 || height <= 0) {
        return nullptr;
    }
    // UsedWithLoadStore so Metal/D3D11/Vulkan compute can write RGBA16F.
    QRhiTexture* texture = _rhi->newTexture(
        QRhiTexture::RGBA16F,
        QSize(width, height),
        1,
        QRhiTexture::UsedWithLoadStore);
    if (!texture || !texture->create()) {
        delete texture;
        return nullptr;
    }
    _debug_stats.textures_created++;
    return texture;
}

void RhiRenderer::_log_movie_present_mode(const char* mode)
{
    if (_movie_present_mode_logged || !mode) {
        return;
    }
    _movie_present_mode_logged = true;
    qInfo().noquote() << QStringLiteral("movie_present=%1").arg(mode);
}

void RhiRenderer::_init_metal_hw_import()
{
    _shutdown_metal_hw_import();
#if defined(Q_OS_MACOS)
    if (!_rhi || _rhi->backend() != QRhi::Metal) {
        return;
    }
    const auto* handles =
        static_cast<const QRhiMetalNativeHandles*>(_rhi->nativeHandles());
    if (!handles || !handles->dev) {
        return;
    }
    std::string err;
    _metal_hw_import.mtl_device = handles->dev;
    _metal_hw_import.mtl_command_queue = handles->cmdQueue;
    _metal_hw_import.cv_metal_texture_cache =
        fc_metal_create_cv_texture_cache(handles->dev, &err);
    _metal_hw_import_ready = _metal_hw_import.cv_metal_texture_cache != nullptr;
    if (!_metal_hw_import_ready) {
        qWarning() << "RhiRenderer: Metal HW import unavailable:"
                    << QString::fromStdString(err);
        _log_movie_present_mode("cpu_upload");
    }
#endif
}

void RhiRenderer::_shutdown_metal_hw_import()
{
    if (_metal_hw_import.cv_metal_texture_cache) {
        fc_metal_release_cv_texture_cache(_metal_hw_import.cv_metal_texture_cache);
        _metal_hw_import.cv_metal_texture_cache = nullptr;
    }
    _metal_hw_import.mtl_device = nullptr;
    _metal_hw_import.mtl_command_queue = nullptr;
    _metal_hw_import_ready = false;
}

void RhiRenderer::_init_d3d11_hw_import()
{
    _shutdown_d3d11_hw_import();
#if defined(Q_OS_WIN)
    if (!_rhi || _rhi->backend() != QRhi::D3D11) {
        return;
    }
    const auto* handles =
        static_cast<const QRhiD3D11NativeHandles*>(_rhi->nativeHandles());
    if (!handles || !handles->dev || !handles->context) {
        return;
    }
    _d3d11_hw_import.device = handles->dev;
    _d3d11_hw_import.context = handles->context;
    // Share with FFmpeg D3D11VA so decoder textures live on the same device.
    fc_d3d11_set_shared_device(handles->dev, handles->context);

    std::string err;
    _d3d11_hw_import_ready = fc_d3d11_create_import_context(&_d3d11_hw_import, &err);
    if (!_d3d11_hw_import_ready) {
        qWarning() << "RhiRenderer: D3D11 HW import unavailable:"
                    << QString::fromStdString(err);
        fc_d3d11_clear_shared_device();
        _log_movie_present_mode("cpu_upload");
    }
#endif
}

void RhiRenderer::_shutdown_d3d11_hw_import()
{
    fc_d3d11_release_import_context(&_d3d11_hw_import);
    _d3d11_hw_import.device = nullptr;
    _d3d11_hw_import.context = nullptr;
    _d3d11_hw_import_ready = false;
#if defined(Q_OS_WIN)
    fc_d3d11_clear_shared_device();
#endif
}

void RhiRenderer::_init_vulkan_hw_import()
{
    _shutdown_vulkan_hw_import();
#if defined(Q_OS_LINUX)
    if (!_rhi || _rhi->backend() != QRhi::Vulkan) {
        return;
    }
    const auto* handles =
        static_cast<const QRhiVulkanNativeHandles*>(_rhi->nativeHandles());
    if (!handles || !handles->dev || !handles->physDev || !handles->inst
        || !handles->gfxQueue) {
        return;
    }
    _vulkan_hw_import.instance = handles->inst->vkInstance();
    _vulkan_hw_import.phys_dev = handles->physDev;
    _vulkan_hw_import.device = handles->dev;
    _vulkan_hw_import.queue = handles->gfxQueue;
    _vulkan_hw_import.queue_family = handles->gfxQueueFamilyIdx;

    std::string err;
    _vulkan_hw_import_ready = fc_vulkan_create_import_context(&_vulkan_hw_import, &err);
    if (!_vulkan_hw_import_ready) {
        qWarning() << "RhiRenderer: Vulkan HW import unavailable:"
                    << QString::fromStdString(err);
        _log_movie_present_mode("cpu_upload");
    }
#endif
}

void RhiRenderer::_shutdown_vulkan_hw_import()
{
    fc_vulkan_release_import_context(&_vulkan_hw_import);
    _vulkan_hw_import.instance = nullptr;
    _vulkan_hw_import.phys_dev = nullptr;
    _vulkan_hw_import.device = nullptr;
    _vulkan_hw_import.queue = nullptr;
    _vulkan_hw_import.queue_family = 0;
    _vulkan_hw_import_ready = false;
}

bool RhiRenderer::_hw_import_ready() const
{
    return _metal_hw_import_ready || _d3d11_hw_import_ready || _vulkan_hw_import_ready;
}

const char* RhiRenderer::_hw_import_mode_name() const
{
    if (_vulkan_hw_import_ready) {
        return "zerocopy_vulkan";
    }
    if (_d3d11_hw_import_ready) {
        return "zerocopy_d3d11";
    }
    if (_metal_hw_import_ready) {
        return "zerocopy_metal";
    }
    return "cpu_upload";
}

bool RhiRenderer::_resolve_slot_dimensions(
    int source_index,
    int decoder_frame,
    CacheManager* cpu_cache,
    int& width,
    int& height,
    int& channels)
{
    if (cpu_cache && cpu_cache->get_frame_dimensions(decoder_frame, width, height, channels)
        && width > 0 && height > 0) {
        return true;
    }
    if (_displayCache.try_get_dimensions(source_index, decoder_frame, width, height, channels)) {
        return true;
    }
    if (UploadJob* job = _uploadQueue.find_job(source_index, decoder_frame)) {
        if (job->width > 0 && job->height > 0) {
            width = job->width;
            height = job->height;
            channels = job->channels > 0 ? job->channels : 4;
            return true;
        }
    }
    return false;
}

bool RhiRenderer::_import_hw_job(UploadJob* job)
{
    if (!job || job->kind != UploadJobKind::HwImport || !job->hw_ticket.valid()) {
        return false;
    }
    if (!_hw_import_ready() || !_rhi) {
        return false;
    }
    const int w = job->hw_ticket.width();
    const int h = job->hw_ticket.height();
    if (w <= 0 || h <= 0) {
        return false;
    }

    QRhiTexture* texture = acquire_hw_import_texture(w, h);
    if (!texture) {
        return false;
    }

    const QRhiTexture::NativeTexture native = texture->nativeTexture();
    void* native_tex = reinterpret_cast<void*>(static_cast<uintptr_t>(native.object));
    if (!native_tex) {
        texture->destroy();
        delete texture;
        return false;
    }

    std::string err;
    bool ok = false;
    const auto kind = job->hw_ticket.kind();
    if (kind == HwFrameTicket::Kind::CVPixelBuffer && _metal_hw_import_ready) {
        ok = fc_metal_import_cvpixelbuffer_to_rgba16f(
            &_metal_hw_import,
            job->hw_ticket.native(),
            native_tex,
            w,
            h,
            &err);
    } else if (kind == HwFrameTicket::Kind::D3D11Texture2D && _d3d11_hw_import_ready) {
        ok = fc_d3d11_import_texture_to_rgba16f(
            &_d3d11_hw_import,
            job->hw_ticket.native(),
            job->hw_ticket.array_index(),
            native_tex,
            w,
            h,
            &err);
    } else if (kind == HwFrameTicket::Kind::DrmPrimeFrame && _vulkan_hw_import_ready) {
        ok = fc_vulkan_import_drm_prime_to_rgba16f(
            &_vulkan_hw_import,
            job->hw_ticket.native(),
            native_tex,
            w,
            h,
            &err);
    } else {
        err = "HW ticket kind does not match active import backend";
    }

    if (!ok) {
        qWarning() << "RhiRenderer: HW import failed:" << QString::fromStdString(err);
        texture->destroy();
        delete texture;
        return false;
    }

    job->width = w;
    job->height = h;
    job->channels = 4;
    job->texture = texture;
    job->hw_ticket.reset();
    return true;
}

void RhiRenderer::put_ready_upload_jobs()
{
    std::vector<UploadJob> ready;
    {
        std::lock_guard<std::mutex> lock(_mutex);
        ready = _uploadQueue.take_ready();
        _uploadQueue.compact_failed();
    }
    for (auto& job : ready) {
        auto* tex = static_cast<QRhiTexture*>(job.texture);
        if (!tex || !_displayCache.enabled()) {
            destroy_upload_texture(job.texture);
            continue;
        }
        const size_t bytes = static_cast<size_t>(job.width) * static_cast<size_t>(job.height)
            * static_cast<size_t>(job.channels) * sizeof(uint16_t);
        _displayCache.put(
            job.source_index,
            job.decoder_frame,
            job.width,
            job.height,
            job.channels,
            tex,
            bytes,
            job.kind == UploadJobKind::HwImport);
        job.texture = nullptr;
        job.hw_ticket.reset();
    }
}

void RhiRenderer::enqueue_gpu_lookahead()
{
    if (!_displayCache.enabled()) {
        return;
    }

    UploadQueuePolicy policy;
    {
        std::lock_guard<std::mutex> lock(_mutex);
        policy = _uploadQueue.policy();
    }
    if (policy != UploadQueuePolicy::EveryFrame) {
        return;
    }

    const size_t max_bytes = _displayCache.max_bytes();
    if (max_bytes == 0) {
        return;
    }

    // Bound how many new jobs we enqueue per pass (staging ring depth / present budget).
    size_t max_enqueue = _present_upload_job_cap();
    if (max_enqueue == 0) {
        return;
    }
    // While playing, reserve one slot for present sync; enqueue at most the rest.
    {
        std::lock_guard<std::mutex> lock(_mutex);
        if (_transport.is_playing() && max_enqueue > 0) {
            --max_enqueue;
        }
    }
    if (max_enqueue == 0) {
        return;
    }

    size_t enqueued = 0;
    std::unordered_map<int, CacheManager*> caches;
    {
        std::lock_guard<std::mutex> lock(_mutex);
        caches = _caches;
    }

    for (const auto& slot : _active_render_params.slots) {
        if (enqueued >= max_enqueue) {
            break;
        }
        auto cache_it = caches.find(slot.source_index);
        if (cache_it == caches.end() || !cache_it->second) {
            continue;
        }
        CacheManager* cpu_cache = cache_it->second;

        SourcePlayhead ph;
        if (!_displayCache.playhead_for_source(slot.source_index, ph)) {
            ph.playhead = slot.frame_index;
            ph.direction = 1;
            ph.in_point = slot.frame_index;
            ph.out_point = slot.frame_index;
        }
        if (ph.out_point < ph.in_point) {
            std::swap(ph.in_point, ph.out_point);
        }
        const int direction = (ph.direction >= 0) ? 1 : -1;
        const int range_size = std::max(1, ph.out_point - ph.in_point + 1);

        // Estimate remaining display budget (resident + in-flight queued/uploading).
        UploadQueueStats qstats;
        {
            std::lock_guard<std::mutex> lock(_mutex);
            qstats = _uploadQueue.stats();
        }
        size_t bytes_per = cpu_cache->bytes_per_frame();
        if (bytes_per == 0) {
            int w = 0, h = 0, c = 0;
            if (cpu_cache->get_frame_dimensions(ph.playhead, w, h, c) && w > 0 && h > 0) {
                bytes_per = static_cast<size_t>(w) * static_cast<size_t>(h)
                    * static_cast<size_t>(std::max(1, c)) * sizeof(uint16_t);
            }
        }
        if (bytes_per == 0) {
            continue;
        }

        int curr = ph.playhead;
        for (int distance = 1; distance <= range_size && enqueued < max_enqueue; ++distance) {
            const size_t committed = _displayCache.resident_bytes()
                + static_cast<size_t>(qstats.pending + qstats.inflight) * bytes_per;
            if (committed + bytes_per > max_bytes) {
                break;
            }

            curr += direction;
            if (curr > ph.out_point) {
                curr = ph.in_point;
            } else if (curr < ph.in_point) {
                curr = ph.out_point;
            }

            if (!cpu_cache->has_frame(curr)) {
                continue;
            }
            if (_displayCache.contains(slot.source_index, curr)) {
                continue;
            }

            bool inserted = false;
            {
                std::lock_guard<std::mutex> lock(_mutex);
                if (_uploadQueue.has_job(slot.source_index, curr)) {
                    continue;
                }
                inserted = _uploadQueue.enqueue(
                    UploadJobRequest{slot.source_index, curr, 0},
                    false);
                qstats = _uploadQueue.stats();
            }
            if (inserted) {
                ++enqueued;
            } else {
                // Queue full — stop this pass.
                break;
            }
        }
    }
}

bool RhiRenderer::gpu_warmup_work_remaining() const
{
    if (!_displayCache.enabled()) {
        return false;
    }
    UploadQueuePolicy policy;
    UploadQueueStats qstats;
    std::unordered_map<int, CacheManager*> caches;
    {
        std::lock_guard<std::mutex> lock(_mutex);
        policy = _uploadQueue.policy();
        qstats = _uploadQueue.stats();
        caches = _caches;
    }
    if (policy != UploadQueuePolicy::EveryFrame) {
        return false;
    }
    if (qstats.pending > 0 || qstats.inflight > 0 || qstats.ready > 0) {
        return true;
    }

    const size_t max_bytes = _displayCache.max_bytes();
    if (max_bytes == 0) {
        return false;
    }

    for (const auto& slot : _active_render_params.slots) {
        auto cache_it = caches.find(slot.source_index);
        if (cache_it == caches.end() || !cache_it->second) {
            continue;
        }
        CacheManager* cpu_cache = cache_it->second;
        SourcePlayhead ph;
        if (!_displayCache.playhead_for_source(slot.source_index, ph)) {
            continue;
        }
        if (ph.out_point < ph.in_point) {
            std::swap(ph.in_point, ph.out_point);
        }
        size_t bytes_per = cpu_cache->bytes_per_frame();
        if (bytes_per == 0) {
            continue;
        }
        if (_displayCache.resident_bytes() + bytes_per > max_bytes) {
            continue;
        }

        const int direction = (ph.direction >= 0) ? 1 : -1;
        const int range_size = std::max(1, ph.out_point - ph.in_point + 1);
        int curr = ph.playhead;
        for (int distance = 1; distance <= range_size; ++distance) {
            curr += direction;
            if (curr > ph.out_point) {
                curr = ph.in_point;
            } else if (curr < ph.in_point) {
                curr = ph.out_point;
            }
            if (cpu_cache->has_frame(curr) && !_displayCache.contains(slot.source_index, curr)) {
                return true;
            }
        }
    }
    return false;
}

void RhiRenderer::_reset_present_upload_budget()
{
    _present_upload_jobs_done = 0;
    _present_upload_job_limit = _present_upload_job_cap();
}

size_t RhiRenderer::_present_upload_job_cap() const
{
    const size_t ring = _stagingRing.empty() ? 0 : _stagingRing.size();
    std::lock_guard<std::mutex> lock(_mutex);
    if (!_transport.is_playing()) {
        return ring;
    }
    // Bound inline GPU transfers while the clock advances content at realtime.
    return std::min(ring, kPlayingUploadJobsPerPresent);
}

bool RhiRenderer::_try_consume_upload_job()
{
    if (_present_upload_job_limit == 0) {
        return false;
    }
    if (_present_upload_jobs_done >= _present_upload_job_limit) {
        return false;
    }
    ++_present_upload_jobs_done;
    return true;
}

void RhiRenderer::drain_upload_queue(QRhiResourceUpdateBatch* batch)
{
    if (!_displayCache.enabled() || !batch || !_rhi) {
        return;
    }

    // Reserve one budget slot for a possible present-authoritative sync upload
    // later in this frame when transport is playing.
    size_t remaining = (_present_upload_jobs_done < _present_upload_job_limit)
        ? (_present_upload_job_limit - _present_upload_jobs_done)
        : 0;
    {
        std::lock_guard<std::mutex> lock(_mutex);
        if (_transport.is_playing() && remaining > 0) {
            --remaining;
        }
    }
    if (remaining == 0) {
        return;
    }

    std::vector<UploadJob*> jobs;
    {
        std::lock_guard<std::mutex> lock(_mutex);
        jobs = _uploadQueue.take_queued_for_submit(remaining);
    }

    for (UploadJob* job : jobs) {
        if (!job) {
            continue;
        }

        if (_displayCache.contains(job->source_index, job->decoder_frame)) {
            std::lock_guard<std::mutex> lock(_mutex);
            _uploadQueue.mark_failed(job);
            continue;
        }

        if (job->kind == UploadJobKind::HwImport) {
            if (!_try_consume_upload_job()) {
                break;
            }
            if (!_import_hw_job(job)) {
                std::lock_guard<std::mutex> lock(_mutex);
                _uploadQueue.mark_failed(job);
                if (_present_upload_jobs_done > 0) {
                    --_present_upload_jobs_done;
                }
                _log_movie_present_mode("cpu_upload");
                continue;
            }
            // Sync Metal compute — resident immediately (no staging generation).
            auto* tex = static_cast<QRhiTexture*>(job->texture);
            const size_t bytes = static_cast<size_t>(job->width) * static_cast<size_t>(job->height)
                * static_cast<size_t>(job->channels) * sizeof(uint16_t);
            if (tex && _displayCache.enabled()) {
                _displayCache.put(
                    job->source_index,
                    job->decoder_frame,
                    job->width,
                    job->height,
                    job->channels,
                    tex,
                    bytes,
                    /*gpu_only=*/true);
                job->texture = nullptr;
            } else {
                destroy_upload_texture(job->texture);
                job->texture = nullptr;
            }
            {
                std::lock_guard<std::mutex> lock(_mutex);
                _uploadQueue.discard(job);
            }
            _debug_stats.last_upload_count++;
            continue;
        }

        CacheManager* cpu_cache = nullptr;
        {
            std::lock_guard<std::mutex> lock(_mutex);
            auto it = _caches.find(job->source_index);
            if (it != _caches.end()) {
                cpu_cache = it->second;
            }
        }
        if (!cpu_cache) {
            std::lock_guard<std::mutex> lock(_mutex);
            _uploadQueue.mark_failed(job);
            continue;
        }

        int w = 0, h = 0, channels = 0;
        if (!cpu_cache->get_frame_dimensions(job->decoder_frame, w, h, channels) || w <= 0 || h <= 0) {
            // Keep Queued — decode may still be in flight.
            continue;
        }

        auto staging_slot = acquire_staging_slot();
        if (!staging_slot) {
            // All staging slots in flight; retry remaining jobs next frame.
            break;
        }

        if (!_try_consume_upload_job()) {
            break;
        }

        QRhiTexture::Format texFormat = (channels == 1) ? QRhiTexture::R16F : QRhiTexture::RGBA16F;
        QRhiTexture* texture = acquire_frame_texture(w, h, channels);
        if (!texture) {
            std::lock_guard<std::mutex> lock(_mutex);
            _uploadQueue.mark_failed(job);
            if (_present_upload_jobs_done > 0) {
                --_present_upload_jobs_done;
            }
            continue;
        }

        StagingBuffer& ringBuf = _stagingRing[*staging_slot];
        const size_t reqElements = static_cast<size_t>(w) * static_cast<size_t>(h) * static_cast<size_t>(channels);
        if (ringBuf.data.size() < reqElements) {
            ringBuf.data.resize(reqElements);
        }
        if (!cpu_cache->copy_frame_data(job->decoder_frame, ringBuf.data.data(), reqElements)) {
            _displayCache.release_to_pool(texture, w, h, texFormat);
            if (_present_upload_jobs_done > 0) {
                --_present_upload_jobs_done;
            }
            continue; // remain Queued
        }

        const int byte_size = static_cast<int>(reqElements * sizeof(uint16_t));
        QByteArray wrappedData = QByteArray::fromRawData(
            reinterpret_cast<const char*>(ringBuf.data.data()),
            byte_size);
        QRhiTextureSubresourceUploadDescription subres(wrappedData);
        subres.setDataStride(w * channels * static_cast<int>(sizeof(uint16_t)));
        QRhiTextureUploadDescription desc(QRhiTextureUploadEntry(0, 0, subres));
        batch->uploadTexture(texture, desc);

        job->width = w;
        job->height = h;
        job->channels = channels;
        job->texture = texture;
        {
            std::lock_guard<std::mutex> lock(_mutex);
            _uploadQueue.mark_uploading(job, _upload_generation, *staging_slot);
        }
        _debug_stats.last_upload_bytes += byte_size;
        _debug_stats.last_upload_count++;
    }
}

bool RhiRenderer::resolve_display_texture(
    int source_index,
    CacheManager* cpu_cache,
    const FrameSlotSpec& spec,
    TextureState& bind_state,
    QRhiResourceUpdateBatch* batch,
    bool& bindings_dirty)
{
    if (spec.width <= 0 || spec.height <= 0) {
        return false;
    }

    if (!_displayCache.enabled()) {
        QRhiTexture* previous = bind_state.texture;
        upload_texture(bind_state, spec, cpu_cache, batch);
        if (bind_state.texture != previous) {
            bindings_dirty = true;
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
            bindings_dirty = true;
        }
        bind_state.texture = cached;
        bind_state.last_w = spec.width;
        bind_state.last_h = spec.height;
        bind_state.last_channels = spec.channels;
        bind_state.last_upload_token = spec.upload_token;
        return true;
    }

    // Present-authoritative: if CPU has the frame, upload into this batch and bind now.
    if (cpu_cache && batch && _rhi) {
        QRhiTexture* tex = upload_present_to_display_cache(
            source_index, cpu_cache, spec, batch);
        if (tex) {
            {
                std::lock_guard<std::mutex> lock(_mutex);
                _uploadQueue.discard_queued(source_index, spec.frame_index);
            }
            if (bind_state.texture != tex) {
                bindings_dirty = true;
            }
            bind_state.texture = tex;
            bind_state.last_w = spec.width;
            bind_state.last_h = spec.height;
            bind_state.last_channels = spec.channels;
            bind_state.last_upload_token = spec.upload_token;
            return true;
        }
    }

    // CPU miss: keep previous bind; enqueue for when decode catches up.
    {
        std::lock_guard<std::mutex> lock(_mutex);
        _uploadQueue.enqueue(
            UploadJobRequest{source_index, spec.frame_index, spec.upload_token},
            false);
    }
    return bind_state.texture != nullptr;
}

QRhiTexture* RhiRenderer::upload_present_to_display_cache(
    int source_index,
    CacheManager* cpu_cache,
    const FrameSlotSpec& spec,
    QRhiResourceUpdateBatch* batch)
{
    if (!cpu_cache || !batch || !_rhi || spec.width <= 0 || spec.height <= 0) {
        return nullptr;
    }
    if (_displayCache.contains(source_index, spec.frame_index)) {
        return _displayCache.try_get(
            source_index, spec.frame_index, spec.width, spec.height, spec.channels, cpu_cache);
    }

    auto staging_slot = acquire_staging_slot();
    if (!staging_slot) {
        return nullptr;
    }

    QRhiTexture::Format texFormat = (spec.channels == 1) ? QRhiTexture::R16F : QRhiTexture::RGBA16F;
    QRhiTexture* texture = acquire_frame_texture(spec.width, spec.height, spec.channels);
    if (!texture) {
        return nullptr;
    }

    StagingBuffer& ringBuf = _stagingRing[*staging_slot];
    size_t reqElements = static_cast<size_t>(spec.width) * static_cast<size_t>(spec.height)
        * static_cast<size_t>(spec.channels);
    if (spec.data_size > 0) {
        reqElements = spec.data_size;
    }
    if (ringBuf.data.size() < reqElements) {
        ringBuf.data.resize(reqElements);
    }
    if (!cpu_cache->copy_frame_data(spec.frame_index, ringBuf.data.data(), reqElements)) {
        _displayCache.release_to_pool(texture, spec.width, spec.height, texFormat);
        return nullptr;
    }

    const int byte_size = static_cast<int>(reqElements * sizeof(uint16_t));
    QByteArray wrappedData = QByteArray::fromRawData(
        reinterpret_cast<const char*>(ringBuf.data.data()),
        byte_size);
    QRhiTextureSubresourceUploadDescription subres(wrappedData);
    subres.setDataStride(spec.width * spec.channels * static_cast<int>(sizeof(uint16_t)));
    QRhiTextureUploadDescription desc(QRhiTextureUploadEntry(0, 0, subres));
    batch->uploadTexture(texture, desc);

    const size_t bytes = static_cast<size_t>(byte_size);
    _displayCache.put(
        source_index,
        spec.frame_index,
        spec.width,
        spec.height,
        spec.channels,
        texture,
        bytes);

    // Present-authoritative upload always proceeds; still count against the
    // per-present budget so lookahead does not pile on after a sync upload.
    ++_present_upload_jobs_done;
    _debug_stats.last_upload_bytes += byte_size;
    _debug_stats.last_upload_count++;
    return texture;
}

void RhiRenderer::upload_texture(TextureState& state, const FrameSlotSpec& spec, CacheManager* cpu_cache, QRhiResourceUpdateBatch* batch)
{
    if (spec.width <= 0 || spec.height <= 0) {
        return;
    }

    QRhiTexture::Format texFormat = (spec.channels == 1) ? QRhiTexture::R16F : QRhiTexture::RGBA16F;
    bool sizeChanged = !state.texture || state.texture == _placeholderTex2D
        || state.last_w != spec.width || state.last_h != spec.height || state.last_channels != spec.channels;
    
    if (sizeChanged) {
        if (state.texture && state.texture != _placeholderTex2D) {
            state.texture->destroy();
            delete state.texture;
        }
        state.texture = _rhi->newTexture(texFormat, QSize(spec.width, spec.height));
        state.texture->create();
        state.last_w = spec.width;
        state.last_h = spec.height;
        state.last_channels = spec.channels;
        _debug_stats.textures_created++;
        // Do not rebuild pipeline here — bindings_dirty / update_srb_resources handles it.
    }

    if (state.last_upload_token == spec.upload_token && !sizeChanged) {
        return;
    }

    auto staging_slot = acquire_staging_slot();
    if (!staging_slot) {
        return;
    }

    StagingBuffer& ringBuf = _stagingRing[*staging_slot];

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

    const int byte_size = static_cast<int>(reqElements * sizeof(uint16_t));
    QByteArray wrappedData = QByteArray::fromRawData(
        reinterpret_cast<const char*>(ringBuf.data.data()),
        byte_size);
    QRhiTextureSubresourceUploadDescription subres(wrappedData);
    subres.setDataStride(spec.width * spec.channels * sizeof(uint16_t));

    QRhiTextureUploadDescription desc(QRhiTextureUploadEntry(0, 0, subres));
    batch->uploadTexture(state.texture, desc);

    state.last_upload_token = spec.upload_token;
    _debug_stats.last_upload_bytes += byte_size;
    _debug_stats.last_upload_count++;
}

void RhiRenderer::upload_texture_3d(OcioLut& lut, QRhiResourceUpdateBatch* batch)
{
    if (!_rhi || lut.size <= 0) {
        return;
    }

    if (lut.texture) {
        lut.texture->destroy();
        delete lut.texture;
    }

    // depth > 0 implies ThreeDimensional; sampleCount must stay 1 (not the flag value).
    lut.texture = _rhi->newTexture(QRhiTexture::RGBA32F, lut.size, lut.size, lut.size);
    lut.texture->create();

    int rowStride = lut.size * 4 * sizeof(float);
    std::vector<QRhiTextureUploadEntry> entries;
    entries.reserve(lut.size);

    for (int layer = 0; layer < lut.size; ++layer) {
        size_t sliceOffset = layer * lut.size * lut.size * 4;
        QByteArray sliceData = QByteArray::fromRawData(
            reinterpret_cast<const char*>(lut.rgba_data.data() + sliceOffset),
            lut.size * lut.size * 4 * sizeof(float));
        QRhiTextureSubresourceUploadDescription subres(sliceData);
        subres.setDataStride(rowStride);
        entries.push_back(QRhiTextureUploadEntry(layer, 0, subres));
    }

    QRhiTextureUploadDescription desc;
    desc.setEntries(entries.begin(), entries.end());
    batch->uploadTexture(lut.texture, desc);

    const int needed = !_ocio_lut_slot_dims.empty()
        ? static_cast<int>(_ocio_lut_slot_dims.size())
        : static_cast<int>(_active_ocio_luts.size());
    if (needed != _pipeline_lut_count) {
        _pipeline_lut_count = needed;
        build_pipeline(_swapChain->renderPassDescriptor(), true);
    } else {
        update_srb_resources();
    }
}

void RhiRenderer::upload_texture_2d_lut(OcioLut& lut, QRhiResourceUpdateBatch* batch)
{
    if (!_rhi || lut.width <= 0 || lut.height <= 0) {
        return;
    }

    if (lut.texture) {
        lut.texture->destroy();
        delete lut.texture;
    }

    lut.texture = _rhi->newTexture(QRhiTexture::RGBA32F, QSize(lut.width, lut.height));
    lut.texture->create();

    const int rowStride = lut.width * 4 * static_cast<int>(sizeof(float));
    QByteArray data = QByteArray::fromRawData(
        reinterpret_cast<const char*>(lut.rgba_data.data()),
        lut.width * lut.height * 4 * static_cast<int>(sizeof(float)));
    QRhiTextureSubresourceUploadDescription subres(data);
    subres.setDataStride(rowStride);
    batch->uploadTexture(
        lut.texture,
        QRhiTextureUploadDescription(QRhiTextureUploadEntry(0, 0, subres)));

    const int needed = !_ocio_lut_slot_dims.empty()
        ? static_cast<int>(_ocio_lut_slot_dims.size())
        : static_cast<int>(_active_ocio_luts.size());
    if (needed != _pipeline_lut_count) {
        _pipeline_lut_count = needed;
        build_pipeline(_swapChain->renderPassDescriptor(), true);
    } else {
        update_srb_resources();
    }
}

void RhiRenderer::_release_gpu_resources() {
    _uploadQueue.clear_with([this](void* tex) { destroy_upload_texture(tex); });
    {
        std::vector<CacheManager*> caches;
        {
            std::lock_guard<std::mutex> lock(_mutex);
            caches.reserve(_caches.size());
            for (const auto& pair : _caches) {
                if (pair.second) {
                    caches.push_back(pair.second);
                }
            }
        }
        for (CacheManager* cache : caches) {
            HwFrameDispatch::unbind(cache);
        }
    }
    clear_tile_srb_cache();
    if (_displayCache.enabled()) {
        _displayCache.clear();
    } else {
        if (_texAState.texture && _texAState.texture != _placeholderTex2D) {
            _texAState.texture->destroy();
            delete _texAState.texture;
        }
        if (_texBState.texture && _texBState.texture != _placeholderTex2D) {
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
    _display_frames_snapshot.clear();
    _display_stats_snapshot = GpuTextureCache::Stats{};

    for (auto& lut : _active_ocio_luts) {
        if (lut.texture) {
            lut.texture->destroy();
            delete lut.texture;
        }
    }
    _active_ocio_luts.clear();

    // Placeholders are destroyed in shutdown_rhi_on_thread after this.
}
