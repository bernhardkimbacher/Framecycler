cmake_minimum_required(VERSION 3.15)
project(framecycler_engine)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# Find Python
find_package(Python COMPONENTS Interpreter Development.Module REQUIRED)

# Find pybind11 via Python module first if CMake find_package is missing
execute_process(
    COMMAND "${Python_EXECUTABLE}" -c "import pybind11; print(pybind11.get_cmake_dir())"
    OUTPUT_VARIABLE PYBIND11_CMAKE_DIR
    OUTPUT_STRIP_TRAILING_WHITESPACE
    RESULT_VARIABLE PYBIND11_SEARCH_RESULT
)

if (PYBIND11_SEARCH_RESULT EQUAL 0 AND EXISTS "${PYBIND11_CMAKE_DIR}")
    list(APPEND CMAKE_PREFIX_PATH "${PYBIND11_CMAKE_DIR}")
endif()

find_package(pybind11 REQUIRED)

# Define pybind11 module target
pybind11_add_module(framecycler_engine
    src/cpp/bindings/python_bindings.cpp
    src/cpp/engine/cache_manager.cpp
    src/cpp/engine/renderer.cpp
)

# Target Include Directories
target_include_directories(framecycler_engine PRIVATE src/cpp/engine)

# Link platform libraries
if(WIN32)
    target_link_libraries(framecycler_engine PRIVATE opengl32)
else()
    find_package(OpenGL REQUIRED)
    target_link_libraries(framecycler_engine PRIVATE OpenGL::GL)
endif()
