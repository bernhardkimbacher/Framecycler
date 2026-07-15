cmake_minimum_required(VERSION 3.21)
project(framecycler_engine)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

if(APPLE)
    list(APPEND CMAKE_PREFIX_PATH "/opt/homebrew" "/opt/homebrew/opt/openimageio" "/usr/local")
endif()

if(QT_SDK_PATH)
    list(PREPEND CMAKE_PREFIX_PATH "${QT_SDK_PATH}")
endif()

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

find_package(Qt6 REQUIRED COMPONENTS Gui Widgets ShaderTools)
find_package(Qt6 COMPONENTS GuiPrivate ShaderToolsPrivate)

find_package(OpenImageIO REQUIRED)

pybind11_add_module(framecycler_engine
    src/cpp/bindings/python_bindings.cpp
    src/cpp/engine/cache_manager.cpp
    src/cpp/engine/rhi_renderer.cpp
    src/cpp/engine/gpu_texture_cache.cpp
    src/cpp/engine/native_decoder.cpp
)

target_include_directories(framecycler_engine PRIVATE src/cpp/engine)

target_link_libraries(framecycler_engine PRIVATE
    Qt6::Gui
    Qt6::Widgets
    Qt6::ShaderTools
    Qt6::GuiPrivate
    Qt6::ShaderToolsPrivate
    OpenImageIO::OpenImageIO
)

find_package(Vulkan)
if(Vulkan_FOUND)
    target_link_libraries(framecycler_engine PRIVATE Vulkan::Vulkan)
endif()

target_compile_definitions(framecycler_engine PRIVATE QT_NO_KEYWORDS QT_NO_PRIVATE_MODULE_WARNING)

if(MSVC)
    target_compile_options(framecycler_engine PRIVATE /wd4996)
else()
    target_compile_options(framecycler_engine PRIVATE -Wno-deprecated-declarations)
endif()

if(PYSIDE6_QT_LIB)
    set_target_properties(framecycler_engine PROPERTIES
        BUILD_RPATH "${PYSIDE6_QT_LIB}"
        INSTALL_RPATH "${PYSIDE6_QT_LIB}"
    )
endif()

