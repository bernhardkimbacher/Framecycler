cmake_minimum_required(VERSION 3.21)
project(rhi_spike_b LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

if(NOT DEFINED QT_SDK_PATH)
    message(FATAL_ERROR "QT_SDK_PATH is required (aqtinstall Qt 6.x matching PySide6)")
endif()
if(NOT DEFINED PYSIDE6_QT_LIB)
    message(FATAL_ERROR "PYSIDE6_QT_LIB is required (PySide6/Qt/lib for runtime rpath)")
endif()

list(PREPEND CMAKE_PREFIX_PATH "${QT_SDK_PATH}")

find_package(Python COMPONENTS Interpreter Development.Module REQUIRED)

execute_process(
    COMMAND "${Python_EXECUTABLE}" -c "import pybind11; print(pybind11.get_cmake_dir())"
    OUTPUT_VARIABLE PYBIND11_CMAKE_DIR
    OUTPUT_STRIP_TRAILING_WHITESPACE
    RESULT_VARIABLE PYBIND11_SEARCH_RESULT
)
if(PYBIND11_SEARCH_RESULT EQUAL 0 AND EXISTS "${PYBIND11_CMAKE_DIR}")
    list(APPEND CMAKE_PREFIX_PATH "${PYBIND11_CMAKE_DIR}")
endif()
find_package(pybind11 REQUIRED)

set(QT_NO_PRIVATE_MODULE_WARNING ON)

find_package(Qt6 6.11 REQUIRED COMPONENTS Gui Widgets ShaderTools)
find_package(Qt6 REQUIRED COMPONENTS GuiPrivate ShaderToolsPrivate)

pybind11_add_module(rhi_spike_b rhi_spike_bindings.cpp)

target_link_libraries(rhi_spike_b PRIVATE
    Qt6::Gui
    Qt6::Widgets
    Qt6::ShaderTools
    Qt6::GuiPrivate
    Qt6::ShaderToolsPrivate
)

target_compile_definitions(rhi_spike_b PRIVATE QT_NO_KEYWORDS QT_NO_PRIVATE_MODULE_WARNING)

set_target_properties(rhi_spike_b PROPERTIES
    BUILD_RPATH "${PYSIDE6_QT_LIB}"
    INSTALL_RPATH "${PYSIDE6_QT_LIB}"
)

if(APPLE)
    target_link_options(rhi_spike_b PRIVATE
        "-Wl,-rpath,${PYSIDE6_QT_LIB}"
    )
elseif(UNIX)
    target_link_options(rhi_spike_b PRIVATE
        "-Wl,-rpath,${PYSIDE6_QT_LIB}"
    )
endif()
