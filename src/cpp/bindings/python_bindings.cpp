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
        .def("write_frame", [](CacheManager& self, int frame_index, int width, int height, int channels, py::array_t<float> array) {
            py::buffer_info info = array.request();
            const float* ptr = static_cast<const float*>(info.ptr);
            self.write_frame(frame_index, width, height, channels, ptr, info.size);
        })
        .def("get_frame_data", [](CacheManager& self, int frame_index) -> py::object {
            int width = 0, height = 0, channels = 0;
            const float* ptr = self.get_frame_data(frame_index, width, height, channels);
            if (!ptr) {
                return py::none();
            }
            // Return NumPy array view sharing memory directly with C++ cache buffer
            return py::array_t<float>(
                { height, width, channels },
                { width * channels * sizeof(float), channels * sizeof(float), sizeof(float) },
                ptr,
                py::cast(&self) // Keep cache owner alive
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
                          py::array_t<float> array_a, int width_a, int height_a, int channels_a,
                          py::object array_b, int width_b, int height_b, int channels_b,
                          int compare_mode, float wipe_pos, int channel_mask,
                          float scale_x, float scale_y, float pan_x, float pan_y) {
            
            py::buffer_info info_a = array_a.request();
            const float* ptr_a = static_cast<const float*>(info_a.ptr);
            
            const float* ptr_b = nullptr;
            if (!array_b.is_none()) {
                py::array_t<float> arr_b = py::cast<py::array_t<float>>(array_b);
                py::buffer_info info_b = arr_b.request();
                ptr_b = static_cast<const float*>(info_b.ptr);
            }
            
            self.render(
                width_a, height_a, channels_a, ptr_a,
                width_b, height_b, channels_b, ptr_b,
                compare_mode, wipe_pos, channel_mask,
                scale_x, scale_y, pan_x, pan_y
            );
        });
}
