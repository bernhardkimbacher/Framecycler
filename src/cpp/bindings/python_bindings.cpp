#include <pybind11/pybind11.h>
#include <pybind11/numpy.h>
#include <pybind11/stl.h>
#include "cache_manager.h"
#include "rhi_renderer.h"

namespace py = pybind11;

PYBIND11_MODULE(framecycler_engine, m) {
    m.doc() = "Framecycler High-Performance C++ Review Playback Core Engine";

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
        .def("get_frame_data", [](py::object self, int frame_index) -> py::object {
            auto& self_cpp = self.cast<CacheManager&>();
            int width = 0, height = 0, channels = 0;
            const uint16_t* ptr = nullptr;
            {
                py::gil_scoped_release release;
                ptr = self_cpp.get_frame_data(frame_index, width, height, channels);
            }
            if (!ptr) {
                return py::none();
            }
            return py::array(
                py::dtype("float16"),
                { height, width, channels },
                {
                    static_cast<py::ssize_t>(width * channels * sizeof(uint16_t)),
                    static_cast<py::ssize_t>(channels * sizeof(uint16_t)),
                    static_cast<py::ssize_t>(sizeof(uint16_t))
                },
                const_cast<uint16_t*>(ptr),
                self
            );
        })
        .def("get_cached_frames", &CacheManager::get_cached_frames)
        .def("clear", &CacheManager::clear)
        .def("set_ram_limit", &CacheManager::set_ram_limit)
        .def("decode_and_cache_frame", &CacheManager::decode_and_cache_frame,
             py::arg("frame_index"), py::arg("file_path"), py::arg("resolution_scale"),
             py::arg("layer") = "", py::arg("fallback_mode") = "Flat Gray",
             py::arg("placeholder_width") = 0, py::arg("placeholder_height") = 0,
             py::call_guard<py::gil_scoped_release>());

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
        .def_readwrite("scale_x", &RenderParams::scale_x)
        .def_readwrite("scale_y", &RenderParams::scale_y)
        .def_readwrite("pan_x", &RenderParams::pan_x)
        .def_readwrite("pan_y", &RenderParams::pan_y)
        .def_readwrite("slots", &RenderParams::slots)
        .def_readwrite("tiles", &RenderParams::tiles);

    py::class_<RhiRenderer>(m, "RhiRenderer")
        .def(py::init<>())
        .def("initialize", &RhiRenderer::initialize)
        .def("shutdown", &RhiRenderer::shutdown)
        .def("is_fallback_null_backend", &RhiRenderer::is_fallback_null_backend)
        .def("update_render_params", &RhiRenderer::update_render_params)
        .def("set_grading_uniform", &RhiRenderer::set_grading_uniform)
        .def("set_grading_uniform_vec3", &RhiRenderer::set_grading_uniform_vec3)
        .def("clear_grading_uniforms", &RhiRenderer::clear_grading_uniforms)
        .def("register_cache", &RhiRenderer::register_cache, py::keep_alive<1, 3>())
        .def("set_shader_sources", &RhiRenderer::set_shader_sources)
        .def("upload_ocio_lut_3d", &RhiRenderer::upload_ocio_lut_3d)
        .def("clear_ocio_luts", &RhiRenderer::clear_ocio_luts)
        .def("set_exposed", &RhiRenderer::set_exposed)
        .def("set_pending_size", &RhiRenderer::set_pending_size)
        .def("request_redraw", &RhiRenderer::request_redraw)
        .def("sync_and_render", &RhiRenderer::sync_and_render, py::call_guard<py::gil_scoped_release>())
        .def("set_display_cache_limit_gb", &RhiRenderer::set_display_cache_limit_gb)
        .def("clear_display_cache", &RhiRenderer::clear_display_cache)
        .def("set_source_playhead", &RhiRenderer::set_source_playhead)
        .def("invalidate_display_cache_source", &RhiRenderer::invalidate_display_cache_source)
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
            d["gpu_cache_hits"] = s.gpu_cache_hits;
            d["gpu_cache_misses"] = s.gpu_cache_misses;
            return d;
        });
}
