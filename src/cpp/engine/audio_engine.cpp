#include "audio_engine.h"

#include "miniaudio.h"

#include <algorithm>
#include <cmath>
#include <cstring>

namespace {

std::string device_id_to_string(const ma_device_id& id)
{
    // Opaque blob → hex for settings persistence.
    static_assert(sizeof(ma_device_id) > 0, "ma_device_id size");
    const auto* bytes = reinterpret_cast<const unsigned char*>(&id);
    static const char* hex = "0123456789abcdef";
    std::string out;
    out.resize(sizeof(ma_device_id) * 2);
    for (size_t i = 0; i < sizeof(ma_device_id); ++i) {
        out[i * 2] = hex[(bytes[i] >> 4) & 0xf];
        out[i * 2 + 1] = hex[bytes[i] & 0xf];
    }
    return out;
}

bool device_id_from_string(const std::string& s, ma_device_id* out)
{
    if (!out || s.size() != sizeof(ma_device_id) * 2) {
        return false;
    }
    auto* bytes = reinterpret_cast<unsigned char*>(out);
    auto nibble = [](char c) -> int {
        if (c >= '0' && c <= '9') return c - '0';
        if (c >= 'a' && c <= 'f') return c - 'a' + 10;
        if (c >= 'A' && c <= 'F') return c - 'A' + 10;
        return -1;
    };
    for (size_t i = 0; i < sizeof(ma_device_id); ++i) {
        const int hi = nibble(s[i * 2]);
        const int lo = nibble(s[i * 2 + 1]);
        if (hi < 0 || lo < 0) {
            return false;
        }
        bytes[i] = static_cast<unsigned char>((hi << 4) | lo);
    }
    return true;
}

} // namespace

AudioEngine::AudioEngine()
{
    _ring.assign(static_cast<size_t>(kRingFrames * NativeAudioDecoder::kOutputChannels), 0.0f);
    _refill_thread = std::thread([this]() { _refill_worker_main(); });
}

AudioEngine::~AudioEngine()
{
    {
        std::lock_guard<std::mutex> lock(_refill_mutex);
        _refill_stop = true;
        _refill_cv.notify_all();
    }
    if (_refill_thread.joinable()) {
        _refill_thread.join();
    }
    _stop_device();
    clear_media();
}

bool AudioEngine::set_media_path(const std::string& path)
{
    {
        std::lock_guard<std::mutex> lock(_mutex);
        // Same path: keep decoder/device running (program pushes are frequent).
        if (_path == path) {
            return path.empty() || (_decoder && _decoder->is_open());
        }
    }
    pause();
    std::lock_guard<std::mutex> lock(_mutex);
    _decoder.reset();
    _path.clear();
    _ring_start_sample = 0;
    _ring_valid_frames = 0;
    _cached_peaks.clear();
    _cached_peaks_path.clear();
    std::fill(_ring.begin(), _ring.end(), 0.0f);
    _last_error.clear();
    if (path.empty()) {
        return true;
    }
    auto dec = std::make_unique<NativeAudioDecoder>();
    if (!dec->open(path)) {
        _last_error = "Failed to open audio media";
        return false;
    }
    _decoder = std::move(dec);
    _path = path;
    return true;
}

void AudioEngine::clear_media()
{
    set_media_path("");
}

bool AudioEngine::has_audio() const
{
    std::lock_guard<std::mutex> lock(_mutex);
    return _decoder && _decoder->has_audio();
}

void AudioEngine::play()
{
    _scrubbing.store(false);
    _playing.store(true);
    const double t = _media_time.load();
    _fill_ring(t);
    _ensure_device();
    _request_refill(t); // keep ahead asynchronously
}

void AudioEngine::pause()
{
    // Do not clear `_scrubbing` — stop_playback during a scrub gesture must not
    // kill scrub preview; only end_scrub()/play() end it.
    _playing.store(false);
}

void AudioEngine::begin_scrub()
{
    _scrubbing.store(true);
    _ensure_device();
}

void AudioEngine::end_scrub()
{
    _scrubbing.store(false);
}

void AudioEngine::seek_media_time(double time_sec, bool scrub_preview)
{
    time_sec = std::max(0.0, time_sec);
    _media_time.store(time_sec);
    _output_sample.store(static_cast<int64_t>(time_sec * NativeAudioDecoder::kOutputSampleRate));
    {
        std::lock_guard<std::mutex> lock(_mutex);
        _ring_valid_frames = 0;
    }
    if (_playing.load()) {
        _fill_ring(time_sec);
        _request_refill(time_sec);
        return;
    }
    // Scrub preview: UI gesture actively requests audible seek.
    // Keep outputting while already scrubbing so mid-drag seeks stay audible
    // even if a call site forgets the preview flag for one frame.
    if (scrub_preview) {
        _scrubbing.store(true);
    }
    if (_scrubbing.load()) {
        _fill_ring(time_sec);
        _ensure_device();
        _request_refill(time_sec);
    }
}

void AudioEngine::sync_to_media_time(double time_sec, bool playing, int direction)
{
    time_sec = std::max(0.0, time_sec);
    _direction.store(direction >= 0 ? 1 : -1);
    // Reverse play: mute audio in v1 (do not free-run).
    if (direction < 0) {
        _playing.store(false);
        _scrubbing.store(false);
        _media_time.store(time_sec);
        return;
    }

    const bool was_playing = _playing.load();
    _media_time.store(time_sec);
    const int64_t expected =
        static_cast<int64_t>(time_sec * NativeAudioDecoder::kOutputSampleRate);

    // Presentation master: soft-resync when drift is large. Fill before snap so
    // the callback never reads outside the ring (empty-ring zeros → clicks).
    const int64_t actual = _output_sample.load();
    const double drift =
        std::fabs(static_cast<double>(actual - expected)) / NativeAudioDecoder::kOutputSampleRate;
    constexpr int64_t kAhead = NativeAudioDecoder::kOutputSampleRate / 5;
    if (drift > _drift_threshold.load()) {
        bool covers = false;
        {
            std::lock_guard<std::mutex> lock(_mutex);
            covers = _ring_covers(expected, 1024);
        }
        if (!covers) {
            _fill_ring(time_sec);
        }
        _output_sample.store(expected);
    }

    if (playing && !was_playing) {
        play();
        return;
    }
    if (!playing && was_playing) {
        pause();
        return;
    }
    _playing.store(playing);
    if (!playing) {
        return;
    }

    _ensure_device();
    // Only refill when the ring does not cover the next ~200ms.
    bool need = false;
    {
        std::lock_guard<std::mutex> lock(_mutex);
        need = !_ring_covers(expected, kAhead);
    }
    if (need) {
        _request_refill(time_sec);
    }
}

void AudioEngine::set_volume(float volume)
{
    _volume.store(std::clamp(volume, 0.0f, 1.0f));
}

void AudioEngine::set_muted(bool muted)
{
    _muted.store(muted);
}

void AudioEngine::set_scrub_audio(bool enabled)
{
    _scrub_audio.store(enabled);
    if (!enabled) {
        _scrubbing.store(false);
    }
}

void AudioEngine::set_drift_threshold(double seconds)
{
    _drift_threshold.store(std::max(0.005, seconds));
}

void AudioEngine::set_null_device(bool null_device)
{
    _null_device.store(null_device);
    if (null_device) {
        _stop_device();
    }
}

bool AudioEngine::set_output_device(const std::string& device_id)
{
    {
        std::lock_guard<std::mutex> lock(_device_mutex);
        if (_output_device_id == device_id && _device_started) {
            return true;
        }
        _output_device_id = device_id;
    }
    _stop_device();
    if (_playing.load() || _scrubbing.load()) {
        _ensure_device();
    }
    return _last_error.empty();
}

std::string AudioEngine::output_device_id() const
{
    std::lock_guard<std::mutex> lock(_device_mutex);
    return _output_device_id;
}

std::string AudioEngine::last_error() const
{
    std::lock_guard<std::mutex> lock(_device_mutex);
    return _last_error;
}

std::vector<AudioDeviceInfo> AudioEngine::list_output_devices()
{
    std::vector<AudioDeviceInfo> out;
    out.push_back(AudioDeviceInfo{"", "System Default", true});

    ma_context_config ctx_config = ma_context_config_init();
    ma_context context;
    std::memset(&context, 0, sizeof(context));
    if (ma_context_init(nullptr, 0, &ctx_config, &context) != MA_SUCCESS) {
        return out;
    }
    ma_device_info* playback = nullptr;
    ma_uint32 playback_count = 0;
    ma_device_info* capture = nullptr;
    ma_uint32 capture_count = 0;
    const ma_result enum_res = ma_context_get_devices(
        &context, &playback, &playback_count, &capture, &capture_count);
    if (enum_res != MA_SUCCESS || playback == nullptr) {
        ma_context_uninit(&context);
        return out;
    }
    for (ma_uint32 i = 0; i < playback_count; ++i) {
        AudioDeviceInfo info;
        info.id = device_id_to_string(playback[i].id);
        info.name = std::string(playback[i].name);
        if (info.name.empty()) {
            info.name = "Audio Device " + std::to_string(i);
        }
        info.is_default = playback[i].isDefault != MA_FALSE;
        out.push_back(std::move(info));
    }
    ma_context_uninit(&context);
    return out;
}

std::vector<float> AudioEngine::peaks(int peaks_per_second)
{
    std::lock_guard<std::mutex> lock(_mutex);
    if (!_decoder || !_decoder->has_audio()) {
        return {};
    }
    if (_cached_peaks_path == _path && _cached_peaks_pps == peaks_per_second && !_cached_peaks.empty()) {
        return _cached_peaks;
    }
    _cached_peaks = _decoder->build_peaks(peaks_per_second);
    _cached_peaks_path = _path;
    _cached_peaks_pps = peaks_per_second;
    return _cached_peaks;
}

bool AudioEngine::_ring_covers(int64_t sample, int64_t ahead_samples) const
{
    if (_ring_valid_frames <= 0) {
        return false;
    }
    const int64_t start = _ring_start_sample;
    const int64_t end = start + _ring_valid_frames;
    return sample >= start && (sample + ahead_samples) <= end;
}

void AudioEngine::_fill_ring(double center_time)
{
    std::string path;
    {
        std::lock_guard<std::mutex> lock(_mutex);
        if (!_decoder || !_decoder->has_audio()) {
            return;
        }
        path = _path;
    }
    NativeAudioDecoder local;
    if (!local.open(path) || !local.has_audio()) {
        return;
    }
    const double start_time = std::max(0.0, center_time - 0.02);
    if (!local.seek(start_time)) {
        return;
    }
    std::vector<float> buf(static_cast<size_t>(kRingFrames * NativeAudioDecoder::kOutputChannels));
    int filled = 0;
    while (filled < kRingFrames) {
        const int got = local.decode_frames(
            buf.data() + filled * NativeAudioDecoder::kOutputChannels,
            kRingFrames - filled);
        if (got <= 0) {
            break;
        }
        filled += got;
    }
    {
        std::lock_guard<std::mutex> lock(_mutex);
        if (_path != path) {
            return;
        }
        _ring.swap(buf);
        if (_ring.size() < static_cast<size_t>(kRingFrames * NativeAudioDecoder::kOutputChannels)) {
            _ring.resize(static_cast<size_t>(kRingFrames * NativeAudioDecoder::kOutputChannels), 0.0f);
        }
        _ring_start_sample = static_cast<int64_t>(start_time * NativeAudioDecoder::kOutputSampleRate);
        _ring_valid_frames = filled;
    }
}

void AudioEngine::_request_refill(double center_time)
{
    std::lock_guard<std::mutex> lock(_refill_mutex);
    _refill_center = center_time;
    _refill_pending = true;
    _refill_cv.notify_one();
}

void AudioEngine::_refill_worker_main()
{
    for (;;) {
        double center = 0.0;
        {
            std::unique_lock<std::mutex> lock(_refill_mutex);
            _refill_cv.wait(lock, [this]() { return _refill_stop || _refill_pending; });
            if (_refill_stop) {
                return;
            }
            center = _refill_center;
            _refill_pending = false;
        }
        _fill_ring(center);
    }
}

void AudioEngine::_data_callback(ma_device* device, void* output, const void* /*input*/, unsigned int frame_count)
{
    auto* self = static_cast<AudioEngine*>(device->pUserData);
    if (self) {
        self->_data_callback_impl(static_cast<float*>(output), frame_count);
    }
}

void AudioEngine::_data_callback_impl(float* output, unsigned int frame_count)
{
    const unsigned int ch = NativeAudioDecoder::kOutputChannels;
    std::memset(output, 0, static_cast<size_t>(frame_count * ch) * sizeof(float));

    // Output while transport playing, or during sticky scrub (ended by end_scrub).
    const bool should_play = _playing.load() || _scrubbing.load();
    if (!should_play || _muted.load() || _direction.load() < 0) {
        return;
    }

    const float gain = _volume.load();
    int64_t cursor = _output_sample.load();

    std::lock_guard<std::mutex> lock(_mutex);
    for (unsigned int i = 0; i < frame_count; ++i) {
        const int64_t rel = cursor - _ring_start_sample;
        float l = 0.0f;
        float r = 0.0f;
        if (rel >= 0 && rel < _ring_valid_frames) {
            const size_t idx = static_cast<size_t>(rel * ch);
            l = _ring[idx];
            r = _ring[idx + 1];
        }
        output[i * ch] = l * gain;
        output[i * ch + 1] = r * gain;
        ++cursor;
    }
    _output_sample.store(cursor);
}

void AudioEngine::_ensure_device()
{
    if (_null_device.load()) {
        return;
    }

    std::string device_id;
    {
        std::lock_guard<std::mutex> lock(_device_mutex);
        if (_device_started) {
            return;
        }
        device_id = _output_device_id;
        _last_error.clear();
    }

    auto* device = new ma_device();
    ma_device_config config = ma_device_config_init(ma_device_type_playback);
    config.playback.format = ma_format_f32;
    config.playback.channels = NativeAudioDecoder::kOutputChannels;
    config.sampleRate = NativeAudioDecoder::kOutputSampleRate;
    config.dataCallback = _data_callback;
    config.pUserData = this;

    ma_device_id explicit_id;
    if (!device_id.empty() && device_id_from_string(device_id, &explicit_id)) {
        config.playback.pDeviceID = &explicit_id;
    } else {
        config.playback.pDeviceID = nullptr; // system default
    }

    // Never hold _mutex here: callback may run during start().
    const ma_result init_res = ma_device_init(nullptr, &config, device);
    if (init_res != MA_SUCCESS) {
        delete device;
        std::lock_guard<std::mutex> lock(_device_mutex);
        _last_error = "ma_device_init failed (" + std::to_string(static_cast<int>(init_res)) + ")";
        return;
    }
    const ma_result start_res = ma_device_start(device);
    if (start_res != MA_SUCCESS) {
        ma_device_uninit(device);
        delete device;
        std::lock_guard<std::mutex> lock(_device_mutex);
        _last_error = "ma_device_start failed (" + std::to_string(static_cast<int>(start_res)) + ")";
        return;
    }

    std::lock_guard<std::mutex> lock(_device_mutex);
    if (_device_started) {
        // Lost a race; tear down the extra device.
        ma_device_uninit(device);
        delete device;
        return;
    }
    _device = device;
    _device_started = true;
    _last_error.clear();
}

void AudioEngine::_stop_device()
{
    ma_device* device = nullptr;
    {
        std::lock_guard<std::mutex> lock(_device_mutex);
        device = _device;
        _device = nullptr;
        _device_started = false;
    }
    if (device) {
        ma_device_uninit(device);
        delete device;
    }
}
