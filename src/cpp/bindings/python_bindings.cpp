#include <pybind11/pybind11.h>
#include <pybind11/numpy.h>
#include <pybind11/functional.h>
#include <pybind11/stl.h>
#include "cache_manager.h"
#include "prefetch_engine.h"
#include "native_decoder.h"
#include "native_movie_decoder.h"
#include "native_audio_decoder.h"
#include "half_convert.h"
#include "rhi_renderer.h"
#include "display_upload_queue.h"
#include "transport_clock.h"
#include "scope_analyzer.h"

#include <cstring>
#include <iostream>
#include <memory>
#include <unordered_map>
#include <vector>

namespace py = pybind11;

namespace {

// py::object must be destroyed while the GIL is held. PrefetchEngine worker
// threads can drop the last std::function copy of a Python callback off-GIL.
struct GilPyObject {
    py::object obj;
    GilPyObject() = default;
    explicit GilPyObject(py::object o) : obj(std::move(o)) {}
    GilPyObject(const GilPyObject&) = default;
    GilPyObject& operator=(const GilPyObject&) = default;
    GilPyObject(GilPyObject&&) noexcept = default;
    GilPyObject& operator=(GilPyObject&&) noexcept = default;
    ~GilPyObject() {
        if (!obj) {
            return;
        }
        py::gil_scoped_acquire gil;
        py::object tmp = std::move(obj);
        // tmp destroyed at end of scope under GIL.
    }
    py::object& get() { return obj; }
};

} // namespace

PYBIND11_MODULE(framecycler_engine, m) {
    m.doc() = "Framecycler High-Performance C++ Review Playback Core Engine";

    m.def("set_decode_threads", &NativeDecoder::set_decode_threads, py::arg("n"),
          "Configure global OIIO / OpenEXR decode thread pools");
    m.def("half_convert_backend", &fc::half_convert_backend,
          "Active movie float16 convert backend (neon_f16|sse_f16c|sse2|scalar)");
    m.def("half_convert_self_test", &fc::half_convert_self_test,
          py::call_guard<py::gil_scoped_release>(),
          "SIMD vs scalar float16 convert parity self-test");

    m.def(
        "downsample_frame",
        [](CacheManager& cache, int frame_index, int max_width) -> py::object {
            int oh = 0, ow = 0;
            std::vector<float> rgb;
            {
                py::gil_scoped_release release;
                rgb = ScopeAnalyzer::downsample_frame(cache, frame_index, max_width, oh, ow);
            }
            if (rgb.empty() || oh <= 0 || ow <= 0) {
                return py::none();
            }
            auto* owned = new std::vector<float>(std::move(rgb));
            py::capsule owner(owned, [](void* p) {
                delete static_cast<std::vector<float>*>(p);
            });
            return py::array_t<float>(
                {oh, ow, 3},
                {
                    static_cast<py::ssize_t>(ow * 3 * sizeof(float)),
                    static_cast<py::ssize_t>(3 * sizeof(float)),
                    static_cast<py::ssize_t>(sizeof(float))
                },
                owned->data(),
                owner);
        },
        py::arg("cache"), py::arg("frame_index"), py::arg("max_width") = 512,
        "Stride-downsample a cached float16 frame to float32 HxWx3 RGB under the cache lock");

    m.def(
        "accumulate_scopes",
        [](py::array_t<float, py::array::c_style | py::array::forcecast> rgb,
           const std::vector<std::string>& types) -> py::list {
            if (rgb.ndim() != 3 || rgb.shape(2) < 3) {
                throw std::runtime_error("accumulate_scopes expects HxWx3 float32 RGB");
            }
            const int h = static_cast<int>(rgb.shape(0));
            const int w = static_cast<int>(rgb.shape(1));
            const float* ptr = rgb.data();
            // Own a contiguous copy so we can release the GIL safely.
            std::vector<float> contiguous;
            {
                py::buffer_info info = rgb.request();
                contiguous.resize(static_cast<size_t>(h) * w * 3);
                const float* src = static_cast<float*>(info.ptr);
                // Copy only RGB even if source has more channels.
                if (rgb.shape(2) == 3) {
                    std::memcpy(contiguous.data(), src, contiguous.size() * sizeof(float));
                } else {
                    for (int y = 0; y < h; ++y) {
                        for (int x = 0; x < w; ++x) {
                            const size_t si = (static_cast<size_t>(y) * w + x) * rgb.shape(2);
                            const size_t di = (static_cast<size_t>(y) * w + x) * 3;
                            contiguous[di + 0] = src[si + 0];
                            contiguous[di + 1] = src[si + 1];
                            contiguous[di + 2] = src[si + 2];
                        }
                    }
                }
            }
            std::vector<std::vector<float>> results;
            results.reserve(types.size());
            {
                py::gil_scoped_release release;
                for (const auto& name : types) {
                    auto st = ScopeAnalyzer::scope_type_from_name(name);
                    results.push_back(ScopeAnalyzer::accumulate(contiguous.data(), h, w, st));
                }
            }
            py::list out;
            for (size_t i = 0; i < results.size(); ++i) {
                auto st = ScopeAnalyzer::scope_type_from_name(types[i]);
                auto* owned = new std::vector<float>(std::move(results[i]));
                py::capsule owner(owned, [](void* p) {
                    delete static_cast<std::vector<float>*>(p);
                });
                py::array arr;
                if (st == ScopeAnalyzer::ScopeType::Waveform) {
                    arr = py::array_t<float>(
                        {ScopeAnalyzer::kWaveformBins, w},
                        owned->data(),
                        owner);
                } else if (st == ScopeAnalyzer::ScopeType::Parade) {
                    arr = py::array_t<float>(
                        {ScopeAnalyzer::kWaveformBins, w * 3},
                        owned->data(),
                        owner);
                } else if (st == ScopeAnalyzer::ScopeType::Vectorscope) {
                    arr = py::array_t<float>(
                        {ScopeAnalyzer::kVectorSize, ScopeAnalyzer::kVectorSize},
                        owned->data(),
                        owner);
                } else if (st == ScopeAnalyzer::ScopeType::Histogram) {
                    arr = py::array_t<float>(
                        {3, ScopeAnalyzer::kHistBins},
                        owned->data(),
                        owner);
                } else {
                    arr = py::array_t<float>(
                        {ScopeAnalyzer::kCieGrid, ScopeAnalyzer::kCieGrid},
                        owned->data(),
                        owner);
                }
                out.append(arr);
            }
            return out;
        },
        py::arg("rgb"), py::arg("types"),
        "Accumulate waveform/parade/vectorscope/histogram/cie from float32 HxWx3 RGB");

    py::enum_<UploadQueuePolicy>(m, "UploadQueuePolicy")
        .value("EveryFrame", UploadQueuePolicy::EveryFrame)
        .value("Realtime", UploadQueuePolicy::Realtime);

    py::enum_<PrefetchDecodeMode>(m, "PrefetchDecodeMode")
        .value("NativePath", PrefetchDecodeMode::NativePath)
        .value("Movie", PrefetchDecodeMode::Movie)
        .value("PythonFallback", PrefetchDecodeMode::PythonFallback);

    py::class_<NativeMovieDecoder, std::shared_ptr<NativeMovieDecoder>>(m, "NativeMovieDecoder")
        .def(py::init<>())
        .def("open", &NativeMovieDecoder::open, py::arg("path"),
             py::call_guard<py::gil_scoped_release>())
        .def("close", &NativeMovieDecoder::close, py::call_guard<py::gil_scoped_release>())
        .def("is_open", &NativeMovieDecoder::is_open)
        .def_property_readonly("width", &NativeMovieDecoder::width)
        .def_property_readonly("height", &NativeMovieDecoder::height)
        .def_property_readonly("fps", &NativeMovieDecoder::fps)
        .def_property_readonly("frame_count", &NativeMovieDecoder::frame_count)
        .def_property_readonly("has_alpha", &NativeMovieDecoder::has_alpha)
        .def_property_readonly("sample_aspect_ratio", &NativeMovieDecoder::sample_aspect_ratio)
        .def_property_readonly("timecode_start", &NativeMovieDecoder::timecode_start)
        .def_property_readonly("start_frame", &NativeMovieDecoder::start_frame)
        .def_property_readonly("end_frame", &NativeMovieDecoder::end_frame)
        .def_property_readonly("path", &NativeMovieDecoder::path)
        .def_property_readonly("hw_type", &NativeMovieDecoder::hw_type)
        .def_property_readonly("bits_per_raw_sample", &NativeMovieDecoder::bits_per_raw_sample)
        .def_property_readonly("pix_fmt_name", &NativeMovieDecoder::pix_fmt_name)
        .def_property_readonly(
            "hw_zerocopy_eligible",
            &NativeMovieDecoder::hw_zerocopy_eligible,
            "True when VT/D3D11VA/VAAPI surfaces can skip CPU upload "
            "(disabled by FRAMECYCLER_MOVIE_CPU_UPLOAD; Windows needs shared QRhi D3D11 device)")
        .def("file_metadata", &NativeMovieDecoder::file_metadata)
        .def(
            "decode_frame",
            [](NativeMovieDecoder& self, int absolute_frame_index, float resolution_scale) -> py::object {
                NativeDecoder::DecodeResult decoded;
                {
                    py::gil_scoped_release release;
                    decoded = self.decode_frame(absolute_frame_index, resolution_scale);
                }
                if (!decoded.success || decoded.pixel_data.empty()) {
                    return py::none();
                }
                auto* data = new std::vector<uint16_t>(std::move(decoded.pixel_data));
                py::capsule owner(data, [](void* p) {
                    delete static_cast<std::vector<uint16_t>*>(p);
                });
                return py::array(
                    py::dtype("float16"),
                    { decoded.height, decoded.width, decoded.channels },
                    {
                        static_cast<py::ssize_t>(decoded.width * decoded.channels * sizeof(uint16_t)),
                        static_cast<py::ssize_t>(decoded.channels * sizeof(uint16_t)),
                        static_cast<py::ssize_t>(sizeof(uint16_t))
                    },
                    data->data(),
                    owner);
            },
            py::arg("absolute_frame_index"),
            py::arg("resolution_scale") = 1.0f)
        .def(
            "probe",
            [](const NativeMovieDecoder& self) {
                py::dict d;
                d["width"] = self.width();
                d["height"] = self.height();
                d["fps"] = self.fps();
                d["frame_count"] = self.frame_count();
                d["has_alpha"] = self.has_alpha();
                d["sample_aspect_ratio"] = self.sample_aspect_ratio();
                d["pixel_aspect_ratio"] = self.sample_aspect_ratio();
                d["timecode_start"] = self.timecode_start();
                d["start_frame"] = self.start_frame();
                d["end_frame"] = self.end_frame();
                d["path"] = self.path();
                d["file_metadata"] = self.file_metadata();
                d["hw_type"] = self.hw_type();
                d["bits_per_raw_sample"] = self.bits_per_raw_sample();
                d["pix_fmt"] = self.pix_fmt_name();
                py::list channels;
                channels.append("R");
                channels.append("G");
                channels.append("B");
                if (self.has_alpha()) {
                    channels.append("A");
                }
                d["channels"] = channels;
                return d;
            });

    py::class_<NativeAudioDecoder, std::shared_ptr<NativeAudioDecoder>>(m, "NativeAudioDecoder")
        .def(py::init<>())
        .def("open", &NativeAudioDecoder::open, py::arg("path"),
             py::call_guard<py::gil_scoped_release>())
        .def("close", &NativeAudioDecoder::close, py::call_guard<py::gil_scoped_release>())
        .def("is_open", &NativeAudioDecoder::is_open)
        .def("has_audio", &NativeAudioDecoder::has_audio)
        .def_property_readonly("path", &NativeAudioDecoder::path)
        .def_property_readonly("duration_seconds", &NativeAudioDecoder::duration_seconds)
        .def_property_readonly("sample_rate", &NativeAudioDecoder::sample_rate)
        .def_property_readonly("channels", &NativeAudioDecoder::channels)
        .def("seek", &NativeAudioDecoder::seek, py::arg("time_sec"),
             py::call_guard<py::gil_scoped_release>())
        .def(
            "decode_frames",
            [](NativeAudioDecoder& self, int max_frames) -> py::object {
                if (max_frames <= 0) {
                    return py::none();
                }
                std::vector<float> buf(
                    static_cast<size_t>(max_frames * NativeAudioDecoder::kOutputChannels));
                int got = 0;
                {
                    py::gil_scoped_release release;
                    got = self.decode_frames(buf.data(), max_frames);
                }
                if (got <= 0) {
                    return py::none();
                }
                buf.resize(static_cast<size_t>(got * NativeAudioDecoder::kOutputChannels));
                py::array_t<float> arr({got, NativeAudioDecoder::kOutputChannels});
                std::memcpy(arr.mutable_data(), buf.data(), buf.size() * sizeof(float));
                return arr;
            },
            py::arg("max_frames") = 4096)
        .def(
            "build_peaks",
            [](NativeAudioDecoder& self, int peaks_per_second) {
                std::vector<float> peaks;
                {
                    py::gil_scoped_release release;
                    peaks = self.build_peaks(peaks_per_second);
                }
                py::array_t<float> arr(static_cast<py::ssize_t>(peaks.size()));
                if (!peaks.empty()) {
                    std::memcpy(arr.mutable_data(), peaks.data(), peaks.size() * sizeof(float));
                }
                return arr;
            },
            py::arg("peaks_per_second") = 300);

    m.def(
        "list_audio_output_devices",
        []() {
            std::vector<AudioDeviceInfo> devices;
            {
                py::gil_scoped_release release;
                devices = AudioEngine::list_output_devices();
            }
            py::list out;
            for (const auto& d : devices) {
                py::dict item;
                item["id"] = d.id;
                item["name"] = d.name;
                item["is_default"] = d.is_default;
                out.append(item);
            }
            return out;
        });

    py::class_<DisplayUploadQueue>(m, "DisplayUploadQueue")
        .def(py::init<>())
        .def("set_policy", &DisplayUploadQueue::set_policy)
        .def("policy", &DisplayUploadQueue::policy)
        .def(
            "enqueue",
            [](DisplayUploadQueue& self, int source_index, int decoder_frame, int upload_token, bool already_resident) {
                return self.enqueue(
                    UploadJobRequest{source_index, decoder_frame, upload_token},
                    already_resident);
            },
            py::arg("source_index"),
            py::arg("decoder_frame"),
            py::arg("upload_token") = 0,
            py::arg("already_resident") = false)
        .def("has_job", &DisplayUploadQueue::has_job)
        .def("clear", &DisplayUploadQueue::clear)
        .def("job_count", &DisplayUploadQueue::job_count)
        .def("stats", [](const DisplayUploadQueue& self) {
            auto s = self.stats();
            py::dict d;
            d["pending"] = s.pending;
            d["inflight"] = s.inflight;
            d["ready"] = s.ready;
            d["completed"] = s.completed;
            d["refused"] = s.refused;
            d["coalesced"] = s.coalesced;
            return d;
        });

    py::class_<CacheManager, std::shared_ptr<CacheManager>>(m, "CacheManager")
        .def(py::init<double>(), py::arg("ram_limit_gb") = 8.0)
        .def("set_playhead", &CacheManager::set_playhead)
        .def("has_frame", &CacheManager::has_frame)
        .def("write_frame", [](CacheManager& self, int frame_index, int width, int height, int channels, py::array array) {
            py::buffer_info info = array.request();
            if (info.ndim != 3) {
                throw std::runtime_error("write_frame expects a (H, W, C) array");
            }
            if (info.itemsize != 2) {
                throw std::runtime_error("write_frame expects float16 pixel data");
            }
            const uint16_t* ptr = static_cast<const uint16_t*>(info.ptr);
            size_t size = info.size;
            {
                py::gil_scoped_release release;
                self.write_frame(frame_index, width, height, channels, ptr, size);
            }
        })
        .def("get_frame_data", [](CacheManager& self, int frame_index) -> py::object {
            // Owned float16 copy taken under a single shared lock via with_active_frame.
            auto* owned = new std::vector<uint16_t>();
            int width = 0;
            int height = 0;
            int channels = 0;
            bool ok = false;
            {
                py::gil_scoped_release release;
                ok = self.with_active_frame(
                    frame_index,
                    [&](const uint16_t* data, int w, int h, int c) {
                        width = w;
                        height = h;
                        channels = c;
                        const size_t n =
                            static_cast<size_t>(w) * static_cast<size_t>(h) * static_cast<size_t>(c);
                        owned->assign(data, data + n);
                    });
            }
            if (!ok || owned->empty() || width <= 0 || height <= 0 || channels <= 0) {
                delete owned;
                return py::none();
            }
            py::capsule owner(owned, [](void* p) {
                delete static_cast<std::vector<uint16_t>*>(p);
            });
            return py::array(
                py::dtype("float16"),
                { height, width, channels },
                {
                    static_cast<py::ssize_t>(width * channels * sizeof(uint16_t)),
                    static_cast<py::ssize_t>(channels * sizeof(uint16_t)),
                    static_cast<py::ssize_t>(sizeof(uint16_t))
                },
                owned->data(),
                owner);
        })
        .def("get_cached_frames", &CacheManager::get_cached_frames)
        .def("clear", &CacheManager::clear)
        .def("set_ram_limit", &CacheManager::set_ram_limit)
        .def("allocated_bytes", &CacheManager::allocated_bytes)
        .def("max_bytes", &CacheManager::max_bytes)
        .def("bytes_per_frame", &CacheManager::bytes_per_frame)
        .def("decode_and_cache_frame", &CacheManager::decode_and_cache_frame,
             py::arg("frame_index"), py::arg("file_path"), py::arg("resolution_scale"),
             py::arg("layer") = "", py::arg("fallback_mode") = "Nearest Frame",
             py::arg("placeholder_width") = 0, py::arg("placeholder_height") = 0,
             py::call_guard<py::gil_scoped_release>())
        .def("try_claim_decode", &CacheManager::try_claim_decode)
        .def("release_decode_claim", &CacheManager::release_decode_claim)
        .def("is_decode_claimed", &CacheManager::is_decode_claimed)
        .def(
            "acquire_write_slot",
            [](CacheManager& self, int frame_index, int width, int height, int channels) -> py::object {
                uint16_t* ptr = nullptr;
                {
                    py::gil_scoped_release release;
                    ptr = self.acquire_write_slot(frame_index, width, height, channels);
                }
                if (!ptr) {
                    return py::none();
                }
                // Non-owning view into CacheManager storage. Caller must keep the
                // decode claim until commit_write_slot; do not use after commit/evict.
                py::capsule owner(ptr, [](void*) {});
                return py::array(
                    py::dtype("float16"),
                    { height, width, channels },
                    {
                        static_cast<py::ssize_t>(width * channels * sizeof(uint16_t)),
                        static_cast<py::ssize_t>(channels * sizeof(uint16_t)),
                        static_cast<py::ssize_t>(sizeof(uint16_t))
                    },
                    ptr,
                    owner);
            },
            py::arg("frame_index"), py::arg("width"), py::arg("height"), py::arg("channels"))
        .def("commit_write_slot", &CacheManager::commit_write_slot,
             py::arg("frame_index"), py::arg("success"));

    py::class_<PrefetchEngine, std::shared_ptr<PrefetchEngine>>(m, "PrefetchEngine")
        .def(py::init<std::shared_ptr<CacheManager>, int>(),
             py::arg("cache"), py::arg("max_workers") = 4,
             py::keep_alive<1, 2>())
        .def(
            "set_path_table",
            [](PrefetchEngine& self, const std::unordered_map<int, std::string>& paths,
               const std::vector<int>& sorted_frames) {
                self.set_path_table(paths, sorted_frames);
            },
            py::arg("paths"), py::arg("sorted_frames"))
        .def(
            "set_options",
            &PrefetchEngine::set_options,
            py::arg("resolution_scale"),
            py::arg("layer") = "",
            py::arg("fallback_mode") = "Nearest Frame",
            py::arg("placeholder_width") = 0,
            py::arg("placeholder_height") = 0,
            py::arg("decode_mode") = PrefetchDecodeMode::NativePath)
        .def(
            "set_movie_decoder",
            [](PrefetchEngine& self, std::shared_ptr<NativeMovieDecoder> decoder) {
                self.set_movie_decoder(std::move(decoder));
            },
            py::arg("decoder") = py::none())
        .def("set_enabled", &PrefetchEngine::set_enabled)
        .def("set_lookahead", &PrefetchEngine::set_lookahead)
        .def("set_max_workers", &PrefetchEngine::set_max_workers)
        .def("set_playback_range", &PrefetchEngine::set_playback_range)
        .def("set_playhead", &PrefetchEngine::set_playhead,
             py::arg("frame_index"), py::arg("direction") = 1)
        .def("schedule", &PrefetchEngine::schedule,
             py::arg("frame_index"), py::arg("priority") = 0)
        .def(
            "set_frame_ready_callback",
            [](PrefetchEngine& self, py::object cb) {
                if (cb.is_none()) {
                    self.set_frame_ready_callback(nullptr);
                    return;
                }
                auto holder = std::make_shared<GilPyObject>(std::move(cb));
                self.set_frame_ready_callback([holder](int frame_index) {
                    py::gil_scoped_acquire gil;
                    try {
                        holder->get()(frame_index);
                    } catch (py::error_already_set& e) {
                        std::cerr << "PrefetchEngine: frame-ready Python callback failed: "
                                  << e.what() << std::endl;
                        e.discard_as_unraisable(__func__);
                    }
                });
            },
            py::arg("callback"))
        .def(
            "set_python_decode_callback",
            [](PrefetchEngine& self, py::object cb) {
                if (cb.is_none()) {
                    self.set_python_decode_callback(nullptr);
                    return;
                }
                auto holder = std::make_shared<GilPyObject>(std::move(cb));
                self.set_python_decode_callback([holder](int frame_index) -> bool {
                    py::gil_scoped_acquire gil;
                    try {
                        py::object result = holder->get()(frame_index);
                        if (result.is_none()) {
                            return false;
                        }
                        return py::bool_(result);
                    } catch (py::error_already_set& e) {
                        std::cerr << "PrefetchEngine: python-decode callback failed: "
                                  << e.what() << std::endl;
                        e.discard_as_unraisable(__func__);
                        return false;
                    }
                });
            },
            py::arg("callback"))
        .def("clear", &PrefetchEngine::clear, py::call_guard<py::gil_scoped_release>())
        // Join workers first (callbacks may still run and need the GIL), then
        // tear down Python callables under the GIL. Clearing before join races:
        // a worker's local std::function copy can destroy py::object off-GIL.
        .def("stop", [](PrefetchEngine& self) {
            {
                py::gil_scoped_release release;
                self.stop();
            }
            self.set_frame_ready_callback(nullptr);
            self.set_python_decode_callback(nullptr);
            self.set_movie_decoder(nullptr);
        });

    py::class_<FrameSlotSpec>(m, "FrameSlotSpec")
        .def(py::init<>())
        .def_readwrite("source_index", &FrameSlotSpec::source_index)
        .def_readwrite("frame_index", &FrameSlotSpec::frame_index)
        .def_readwrite("width", &FrameSlotSpec::width)
        .def_readwrite("height", &FrameSlotSpec::height)
        .def_readwrite("channels", &FrameSlotSpec::channels)
        .def_readwrite("upload_token", &FrameSlotSpec::upload_token);

    py::class_<TileSpec>(m, "TileSpec")
        .def(py::init<>())
        .def_readwrite("source_index", &TileSpec::source_index)
        .def_readwrite("scale_x", &TileSpec::scale_x)
        .def_readwrite("scale_y", &TileSpec::scale_y)
        .def_readwrite("offset_x", &TileSpec::offset_x)
        .def_readwrite("offset_y", &TileSpec::offset_y);

    py::class_<RenderParams>(m, "RenderParams")
        .def(py::init<>())
        .def_readwrite("compare_mode", &RenderParams::compare_mode)
        .def_readwrite("sequence_index", &RenderParams::sequence_index)
        .def_readwrite("wipe_pos", &RenderParams::wipe_pos)
        .def_readwrite("channel_mask", &RenderParams::channel_mask)
        .def_readwrite("false_color_mode", &RenderParams::false_color_mode)
        .def_readwrite("zebra_lo", &RenderParams::zebra_lo)
        .def_readwrite("zebra_hi", &RenderParams::zebra_hi)
        .def_readwrite("scale_x", &RenderParams::scale_x)
        .def_readwrite("scale_y", &RenderParams::scale_y)
        .def_readwrite("pan_x", &RenderParams::pan_x)
        .def_readwrite("pan_y", &RenderParams::pan_y)
        .def_readwrite("slots", &RenderParams::slots)
        .def_readwrite("tiles", &RenderParams::tiles);

    py::enum_<TransportLoopMode>(m, "TransportLoopMode")
        .value("Once", TransportLoopMode::Once)
        .value("Loop", TransportLoopMode::Loop)
        .value("Bounce", TransportLoopMode::Bounce);

    py::enum_<TransportTimingMode>(m, "TransportTimingMode")
        .value("EveryFrame", TransportTimingMode::EveryFrame)
        .value("Realtime", TransportTimingMode::Realtime);

    py::class_<TransportSlotMapping>(m, "TransportSlotMapping")
        .def(py::init<>())
        .def_readwrite("source_index", &TransportSlotMapping::source_index)
        .def_readwrite("segment_global_start", &TransportSlotMapping::segment_global_start)
        .def_readwrite("segment_global_end", &TransportSlotMapping::segment_global_end)
        .def_readwrite("decoder_start_frame", &TransportSlotMapping::decoder_start_frame)
        .def_readwrite("decoder_frames", &TransportSlotMapping::decoder_frames)
        .def_readwrite("playback_in", &TransportSlotMapping::playback_in)
        .def_readwrite("playback_out", &TransportSlotMapping::playback_out);

    py::class_<TransportProgram>(m, "TransportProgram")
        .def(py::init<>())
        .def_readwrite("playing", &TransportProgram::playing)
        .def_readwrite("direction", &TransportProgram::direction)
        .def_readwrite("fps", &TransportProgram::fps)
        .def_readwrite("in_point", &TransportProgram::in_point)
        .def_readwrite("out_point", &TransportProgram::out_point)
        .def_readwrite("loop_mode", &TransportProgram::loop_mode)
        .def_readwrite("timing_mode", &TransportProgram::timing_mode)
        .def_readwrite("current_frame", &TransportProgram::current_frame)
        .def_readwrite("segment_global_start", &TransportProgram::segment_global_start)
        .def_readwrite("segment_global_end", &TransportProgram::segment_global_end)
        .def_readwrite("hold_at_segment_bounds", &TransportProgram::hold_at_segment_bounds)
        .def_readwrite("slots", &TransportProgram::slots);

    py::class_<TransportAdvanceResult>(m, "TransportAdvanceResult")
        .def(py::init<>())
        .def_readonly("frame", &TransportAdvanceResult::frame)
        .def_readonly("direction", &TransportAdvanceResult::direction)
        .def_readonly("moved", &TransportAdvanceResult::moved)
        .def_readonly("stop", &TransportAdvanceResult::stop)
        .def_readonly("segment_boundary", &TransportAdvanceResult::segment_boundary)
        .def_readonly("steps_taken", &TransportAdvanceResult::steps_taken);

    // Pure clock helpers for parity tests (no renderer required).
    m.def("transport_realtime_steps", &TransportClock::realtime_steps,
          py::arg("elapsed_seconds"), py::arg("fps"));
    m.def(
        "transport_advance_playback",
        [](int current_frame, int direction, int steps, int in_point, int out_point,
           const std::string& loop_mode) {
            return TransportClock::advance_playback(
                current_frame,
                direction,
                steps,
                in_point,
                out_point,
                TransportClock::parse_loop_mode(loop_mode));
        },
        py::arg("current_frame"),
        py::arg("direction"),
        py::arg("steps"),
        py::arg("in_point"),
        py::arg("out_point"),
        py::arg("loop_mode") = "loop");
    m.def(
        "transport_decoder_frame_for_source",
        [](const TransportProgram& program, int source_index, int global_frame) {
            TransportClock clock;
            clock.set_program(program);
            return clock.decoder_frame_for_source(source_index, global_frame);
        },
        py::arg("program"),
        py::arg("source_index"),
        py::arg("global_frame"));

    py::class_<TransportClock>(m, "TransportClock")
        .def(py::init<>())
        .def("set_program", &TransportClock::set_program)
        .def("play", [](TransportClock& self) { self.play(); })
        .def(
            "play_at",
            [](TransportClock& self, double t_sec) {
                const auto tp = TransportClock::TimePoint(
                    std::chrono::duration_cast<TransportClock::Clock::duration>(
                        std::chrono::duration<double>(t_sec)));
                self.play(tp);
            },
            py::arg("t_sec"))
        .def("pause", &TransportClock::pause)
        .def("seek", [](TransportClock& self, int frame) { self.seek(frame); },
             py::arg("global_frame"))
        .def("is_playing", &TransportClock::is_playing)
        .def("current_frame", &TransportClock::current_frame)
        .def("direction", &TransportClock::direction)
        .def(
            "tick_now",
            [](TransportClock& self, py::object can_advance) {
                TransportClock::CanAdvanceFn pred;
                if (!can_advance.is_none()) {
                    pred = [can_advance](int global_frame) -> bool {
                        return py::bool_(can_advance(global_frame));
                    };
                }
                return self.tick(TransportClock::Clock::now(), pred);
            },
            py::arg("can_advance") = py::none())
        .def(
            "tick_at",
            [](TransportClock& self, double t_sec, py::object can_advance) {
                TransportClock::CanAdvanceFn pred;
                if (!can_advance.is_none()) {
                    pred = [can_advance](int global_frame) -> bool {
                        return py::bool_(can_advance(global_frame));
                    };
                }
                const auto tp = TransportClock::TimePoint(
                    std::chrono::duration_cast<TransportClock::Clock::duration>(
                        std::chrono::duration<double>(t_sec)));
                return self.tick(tp, pred);
            },
            py::arg("t_sec"),
            py::arg("can_advance") = py::none())
        .def(
            "decoder_frame_for_source",
            &TransportClock::decoder_frame_for_source,
            py::arg("source_index"),
            py::arg("global_frame"));

    py::class_<RhiRenderer>(m, "RhiRenderer")
        .def(py::init<>())
        .def("initialize", &RhiRenderer::initialize)
        .def("shutdown", &RhiRenderer::shutdown)
        .def("is_fallback_null_backend", &RhiRenderer::is_fallback_null_backend)
        .def("set_force_null_backend", &RhiRenderer::set_force_null_backend)
        .def("update_render_params", &RhiRenderer::update_render_params)
        .def("set_grading_uniform", &RhiRenderer::set_grading_uniform)
        .def("set_grading_uniform_vec3", &RhiRenderer::set_grading_uniform_vec3)
        .def("clear_grading_uniforms", &RhiRenderer::clear_grading_uniforms)
        .def("register_cache", &RhiRenderer::register_cache, py::keep_alive<1, 3>())
        .def("set_shader_sources", &RhiRenderer::set_shader_sources)
        .def("upload_ocio_lut_3d", &RhiRenderer::upload_ocio_lut_3d)
        .def(
            "upload_ocio_lut_2d",
            &RhiRenderer::upload_ocio_lut_2d,
            py::arg("index"),
            py::arg("width"),
            py::arg("height"),
            py::arg("channels"),
            py::arg("data"))
        .def("set_ocio_lut_slot_dims", &RhiRenderer::set_ocio_lut_slot_dims)
        .def("clear_ocio_luts", &RhiRenderer::clear_ocio_luts)
        .def("cached_pipeline_key", &RhiRenderer::cached_pipeline_key)
        .def("set_exposed", &RhiRenderer::set_exposed)
        .def("set_pending_size", &RhiRenderer::set_pending_size)
        .def("request_redraw", &RhiRenderer::request_redraw)
        .def("sync_and_render", &RhiRenderer::sync_and_render, py::call_guard<py::gil_scoped_release>())
        .def("set_display_cache_limit_gb", &RhiRenderer::set_display_cache_limit_gb)
        .def("clear_display_cache", &RhiRenderer::clear_display_cache)
        .def("set_source_playhead", &RhiRenderer::set_source_playhead)
        .def("invalidate_display_cache_source", &RhiRenderer::invalidate_display_cache_source)
        .def("set_transport_program", &RhiRenderer::set_transport_program)
        .def("transport_play", &RhiRenderer::transport_play)
        .def("transport_pause", &RhiRenderer::transport_pause)
        .def("transport_seek", &RhiRenderer::transport_seek,
             py::arg("global_frame"), py::arg("scrub_preview") = false)
        .def("get_transport_frame", &RhiRenderer::get_transport_frame)
        .def("get_transport_direction", &RhiRenderer::get_transport_direction)
        .def("is_transport_playing", &RhiRenderer::is_transport_playing)
        .def("set_audio_media_path", &RhiRenderer::set_audio_media_path,
             py::arg("path"), py::arg("media_origin_frame") = 0,
             py::call_guard<py::gil_scoped_release>())
        .def("set_audio_volume", &RhiRenderer::set_audio_volume, py::arg("volume"))
        .def("set_audio_muted", &RhiRenderer::set_audio_muted, py::arg("muted"))
        .def("set_audio_scrub", &RhiRenderer::set_audio_scrub, py::arg("enabled"))
        .def("begin_audio_scrub", &RhiRenderer::begin_audio_scrub)
        .def("end_audio_scrub", &RhiRenderer::end_audio_scrub)
        .def("set_audio_output_device", &RhiRenderer::set_audio_output_device, py::arg("device_id"),
             py::call_guard<py::gil_scoped_release>())
        .def("audio_output_device", &RhiRenderer::audio_output_device)
        .def("audio_last_error", &RhiRenderer::audio_last_error)
        .def("audio_has_audio", &RhiRenderer::audio_has_audio)
        .def(
            "list_audio_output_devices",
            [](RhiRenderer& /*self*/) {
                std::vector<AudioDeviceInfo> devices;
                {
                    py::gil_scoped_release release;
                    devices = RhiRenderer::list_audio_output_devices();
                }
                py::list out;
                for (const auto& d : devices) {
                    py::dict item;
                    item["id"] = d.id;
                    item["name"] = d.name;
                    item["is_default"] = d.is_default;
                    out.append(item);
                }
                return out;
            })
        .def(
            "audio_peaks",
            [](RhiRenderer& self, int peaks_per_second) {
                std::vector<float> peaks;
                {
                    py::gil_scoped_release release;
                    peaks = self.audio_peaks(peaks_per_second);
                }
                py::array_t<float> arr(static_cast<py::ssize_t>(peaks.size()));
                if (!peaks.empty()) {
                    std::memcpy(arr.mutable_data(), peaks.data(), peaks.size() * sizeof(float));
                }
                return arr;
            },
            py::arg("peaks_per_second") = 300)
        .def("ack_transport_frame_notify", &RhiRenderer::ack_transport_frame_notify)
        .def(
            "poll_transport_frame_notify",
            [](RhiRenderer& self) -> py::object {
                int frame = -1;
                int direction = 1;
                if (!self.poll_transport_frame_notify(frame, direction)) {
                    return py::none();
                }
                return py::make_tuple(frame, direction);
            })
        .def(
            "poll_transport_boundary_notify",
            [](RhiRenderer& self) -> py::object {
                int frame = -1;
                int direction = 1;
                if (!self.poll_transport_boundary_notify(frame, direction)) {
                    return py::none();
                }
                return py::make_tuple(frame, direction);
            })
        .def(
            "set_frame_changed_callback",
            [](RhiRenderer& self, py::object cb) {
                if (cb.is_none()) {
                    self.set_frame_changed_callback(nullptr);
                    return;
                }
                auto holder = std::make_shared<GilPyObject>(std::move(cb));
                self.set_frame_changed_callback([holder](int frame, int direction) {
                    py::gil_scoped_acquire gil;
                    try {
                        holder->get()(frame, direction);
                    } catch (py::error_already_set& e) {
                        std::cerr << "RhiRenderer: frame-changed Python callback failed: "
                                  << e.what() << std::endl;
                        e.restore();
                        PyErr_Clear();
                    }
                });
            })
        .def(
            "set_segment_boundary_callback",
            [](RhiRenderer& self, py::object cb) {
                if (cb.is_none()) {
                    self.set_segment_boundary_callback(nullptr);
                    return;
                }
                auto holder = std::make_shared<GilPyObject>(std::move(cb));
                self.set_segment_boundary_callback([holder](int frame, int direction) {
                    py::gil_scoped_acquire gil;
                    try {
                        holder->get()(frame, direction);
                    } catch (py::error_already_set& e) {
                        std::cerr << "RhiRenderer: segment-boundary Python callback failed: "
                                  << e.what() << std::endl;
                        e.restore();
                        PyErr_Clear();
                    }
                });
            })
        .def("get_display_cache_stats", [](const RhiRenderer& self) {
            auto s = self.get_display_cache_stats();
            py::dict d;
            d["hits"] = s.hits;
            d["misses"] = s.misses;
            d["evictions"] = s.evictions;
            d["resident_bytes"] = static_cast<int>(s.resident_bytes);
            d["resident_frames"] = s.resident_frames;
            return d;
        })
        .def("get_display_cached_frames", &RhiRenderer::get_display_cached_frames,
             py::arg("source_index"))
        .def("set_upload_queue_policy", &RhiRenderer::set_upload_queue_policy)
        .def("upload_queue_policy", &RhiRenderer::upload_queue_policy)
        .def("get_upload_queue_stats", [](const RhiRenderer& self) {
            auto s = self.get_upload_queue_stats();
            py::dict d;
            d["pending"] = s.pending;
            d["inflight"] = s.inflight;
            d["ready"] = s.ready;
            d["completed"] = s.completed;
            d["refused"] = s.refused;
            d["coalesced"] = s.coalesced;
            return d;
        })
        .def("get_debug_stats", [](const RhiRenderer& self) {
            auto s = self.get_debug_stats();
            py::dict d;
            d["begin_frame_ok"] = s.begin_frame_ok;
            d["begin_frame_fail"] = s.begin_frame_fail;
            d["frames_drawn"] = s.frames_drawn;
            d["frames_cleared_only"] = s.frames_cleared_only;
            d["cache_hits"] = s.cache_hits;
            d["cache_misses"] = s.cache_misses;
            d["rhi_ready"] = s.rhi_ready;
            d["shaders_valid"] = s.shaders_valid;
            d["pipeline_ready"] = s.pipeline_ready;
            d["tex_a_ready"] = s.tex_a_ready;
            d["last_tex_w"] = s.last_tex_w;
            d["last_tex_h"] = s.last_tex_h;
            d["swap_w"] = s.swap_w;
            d["swap_h"] = s.swap_h;
            d["last_render_ms"] = s.last_render_ms;
            d["last_upload_ms"] = s.last_upload_ms;
            d["last_draw_ms"] = s.last_draw_ms;
            d["last_upload_bytes"] = s.last_upload_bytes;
            d["last_upload_count"] = s.last_upload_count;
            d["last_upload_jobs"] = s.last_upload_jobs;
            d["upload_ms_total"] = s.upload_ms_total;
            d["last_end_frame_ms"] = s.last_end_frame_ms;
            d["end_frame_ms_max"] = s.end_frame_ms_max;
            d["gpu_cache_hits"] = s.gpu_cache_hits;
            d["gpu_cache_misses"] = s.gpu_cache_misses;
            d["pipeline_rebuilds"] = s.pipeline_rebuilds;
            d["srb_updates"] = s.srb_updates;
            d["staging_waits"] = s.staging_waits;
            d["textures_created"] = s.textures_created;
            d["textures_pooled_reuses"] = s.textures_pooled_reuses;
            d["lut_textures_pooled_reuses"] = s.lut_textures_pooled_reuses;
            d["pipeline_lut_count"] = s.pipeline_lut_count;
            return d;
        })
        .def("pipeline_lut_count", &RhiRenderer::pipeline_lut_count);
}
