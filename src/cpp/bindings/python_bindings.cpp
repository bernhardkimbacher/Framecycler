#include <pybind11/pybind11.h>
#include <pybind11/numpy.h>
#include <pybind11/stl.h>
#include "cache_manager.h"
#include "renderer.h"

namespace py = pybind11;

PYBIND11_MODULE(framecycler_engine, m) {
    m.doc() = "Framecycler High-Performance C++ Review Playback Core Engine";

    py::class_<CacheManager>(m, "CacheManager")
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
            self.write_frame(frame_index, width, height, channels, ptr, info.size);
        })
        .def("get_frame_data", [](py::object self, int frame_index) -> py::object {
            auto& self_cpp = self.cast<CacheManager&>();
            int width = 0, height = 0, channels = 0;
            const uint16_t* ptr = self_cpp.get_frame_data(frame_index, width, height, channels);
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
        .def("set_ram_limit", &CacheManager::set_ram_limit);

    py::class_<GLRenderer>(m, "GLRenderer")
        .def(py::init<>())
        .def("initialize", &GLRenderer::initialize)
        .def("set_ocio_shader", &GLRenderer::set_ocio_shader)
        .def("upload_ocio_lut_3d", [](GLRenderer& self, int index, int size, py::array_t<float> array) {
            py::buffer_info info = array.request();
            const float* ptr = static_cast<const float*>(info.ptr);
            self.upload_ocio_lut_3d(index, size, ptr);
        })
        .def("cleanup", &GLRenderer::cleanup)
        .def("render", [](GLRenderer& self,
                          py::array array_a, int width_a, int height_a, int channels_a, uint64_t upload_token_a,
                          py::object array_b, int width_b, int height_b, int channels_b, uint64_t upload_token_b,
                          int compare_mode, float wipe_pos, int channel_mask,
                          float scale_x, float scale_y, float pan_x, float pan_y) {

            py::buffer_info info_a = array_a.request();
            const void* ptr_a = info_a.ptr;

            const void* ptr_b = nullptr;
            if (!array_b.is_none()) {
                py::array arr_b = py::cast<py::array>(array_b);
                py::buffer_info info_b = arr_b.request();
                ptr_b = info_b.ptr;
            }

            self.render(
                width_a, height_a, channels_a, ptr_a, upload_token_a,
                width_b, height_b, channels_b, ptr_b, upload_token_b,
                compare_mode, wipe_pos, channel_mask,
                scale_x, scale_y, pan_x, pan_y
            );
        });
}
