#include <pybind11/pybind11.h>
#include <pybind11/numpy.h>
#include <pybind11/stl.h>
#include "cache_manager.h"

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
}
