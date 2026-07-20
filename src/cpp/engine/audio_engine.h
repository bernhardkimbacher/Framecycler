#pragma once

#include "native_audio_decoder.h"

#include <atomic>
#include <condition_variable>
#include <cstdint>
#include <memory>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

struct ma_device;
struct ma_context;

struct AudioDeviceInfo {
    std::string id;    // opaque stable key; empty = system default
    std::string name;
    bool is_default = false;
};

/// Presentation-slave audio output: media time from TransportClock drives PCM.
class AudioEngine {
public:
    AudioEngine();
    ~AudioEngine();

    AudioEngine(const AudioEngine&) = delete;
    AudioEngine& operator=(const AudioEngine&) = delete;

    /// Bind a media path (opens NativeAudioDecoder). Empty path clears.
    bool set_media_path(const std::string& path);
    void clear_media();
    bool has_audio() const;

    void play();
    void pause();
    /// Seek media time. When ``scrub_preview`` is true, arm sticky scrub output
    /// until end_scrub(). Preference alone does not free-run audio.
    void seek_media_time(double time_sec, bool scrub_preview = false);
    /// Begin scrub-preview output (timeline/viewport mouse-down).
    void begin_scrub();
    /// Stop scrub-preview audio (mouse release). Does not clear the preference.
    void end_scrub();

    /// Called from transport tick: authority is presentation media time.
    void sync_to_media_time(double time_sec, bool playing, int direction);

    void set_volume(float volume); // 0..1
    void set_muted(bool muted);
    /// Preference: seek-while-paused may briefly preview audio.
    void set_scrub_audio(bool enabled);

    float volume() const { return _volume.load(); }
    bool muted() const { return _muted.load(); }
    bool scrub_audio() const { return _scrub_audio.load(); }

    /// Soft-resync threshold in seconds (default 0.04).
    void set_drift_threshold(double seconds);

    /// Peak envelope for timeline (cached per path).
    std::vector<float> peaks(int peaks_per_second = 300);

    /// When true, never open a real device (silence / tests).
    void set_null_device(bool null_device);

    /// Empty id selects the system default device.
    bool set_output_device(const std::string& device_id);
    std::string output_device_id() const;
    static std::vector<AudioDeviceInfo> list_output_devices();

    /// Last device open error (empty if ok).
    std::string last_error() const;

private:
    static void _data_callback(ma_device* device, void* output, const void* input, unsigned int frame_count);
    void _data_callback_impl(float* output, unsigned int frame_count);
    void _ensure_device();
    void _stop_device();
    void _refill_worker_main();
    void _request_refill(double center_time);
    void _fill_ring(double center_time);
    bool _ring_covers(int64_t sample, int64_t ahead_samples) const;

    mutable std::mutex _mutex;       // decoder + ring
    mutable std::mutex _device_mutex; // device lifetime (never hold during callback init under ring lock)
    std::unique_ptr<NativeAudioDecoder> _decoder;
    std::string _path;

    // Ring of interleaved stereo floats.
    static constexpr int kRingSeconds = 4;
    static constexpr int kRingFrames = NativeAudioDecoder::kOutputSampleRate * kRingSeconds;
    std::vector<float> _ring; // size kRingFrames * 2
    int64_t _ring_start_sample = 0; // absolute sample index of ring[0]
    int _ring_valid_frames = 0;

    std::atomic<double> _media_time{0.0};
    std::atomic<bool> _playing{false};
    std::atomic<int> _direction{1};
    std::atomic<float> _volume{1.0f};
    std::atomic<bool> _muted{false};
    std::atomic<bool> _scrub_audio{false}; // preference (UI); does not gate callback
    std::atomic<bool> _scrubbing{false};   // sticky preview until end_scrub/pause
    std::atomic<double> _drift_threshold{0.08};
    std::atomic<bool> _null_device{false};

    ma_device* _device = nullptr;
    bool _device_started = false;
    std::string _output_device_id; // empty = default
    std::string _last_error;

    std::thread _refill_thread;
    std::mutex _refill_mutex;
    std::condition_variable _refill_cv;
    bool _refill_stop = false;
    bool _refill_pending = false;
    double _refill_center = 0.0;

    std::vector<float> _cached_peaks;
    std::string _cached_peaks_path;
    int _cached_peaks_pps = 0;

    // Presentation-snapped output sample cursor.
    std::atomic<int64_t> _output_sample{0};
};
