# FindFFmpeg.cmake — locates libavformat, libavcodec, libavutil, libswscale.
#
# Sets:
#   FFmpeg_FOUND
#   FFmpeg_INCLUDE_DIRS
#   FFmpeg_LIBRARIES
#   FFmpeg::FFmpeg (imported interface target)

find_package(PkgConfig QUIET)
if(PkgConfig_FOUND)
    pkg_check_modules(PC_AVFORMAT QUIET libavformat)
    pkg_check_modules(PC_AVCODEC QUIET libavcodec)
    pkg_check_modules(PC_AVUTIL QUIET libavutil)
    pkg_check_modules(PC_SWSCALE QUIET libswscale)
endif()

set(_FFMPEG_HINTS
    /opt/homebrew/opt/ffmpeg
    /usr/local/opt/ffmpeg
    /opt/homebrew
    /usr/local
)

find_path(FFmpeg_AVFORMAT_INCLUDE_DIR
    NAMES libavformat/avformat.h
    HINTS
        ${PC_AVFORMAT_INCLUDE_DIRS}
        ${_FFMPEG_HINTS}
        ENV FFMPEG_ROOT
    PATH_SUFFIXES include
)

find_library(FFmpeg_AVFORMAT_LIBRARY
    NAMES avformat
    HINTS
        ${PC_AVFORMAT_LIBRARY_DIRS}
        ${_FFMPEG_HINTS}
        ENV FFMPEG_ROOT
    PATH_SUFFIXES lib
)

find_library(FFmpeg_AVCODEC_LIBRARY
    NAMES avcodec
    HINTS
        ${PC_AVCODEC_LIBRARY_DIRS}
        ${_FFMPEG_HINTS}
        ENV FFMPEG_ROOT
    PATH_SUFFIXES lib
)

find_library(FFmpeg_AVUTIL_LIBRARY
    NAMES avutil
    HINTS
        ${PC_AVUTIL_LIBRARY_DIRS}
        ${_FFMPEG_HINTS}
        ENV FFMPEG_ROOT
    PATH_SUFFIXES lib
)

find_library(FFmpeg_SWSCALE_LIBRARY
    NAMES swscale
    HINTS
        ${PC_SWSCALE_LIBRARY_DIRS}
        ${_FFMPEG_HINTS}
        ENV FFMPEG_ROOT
    PATH_SUFFIXES lib
)

include(FindPackageHandleStandardArgs)
find_package_handle_standard_args(FFmpeg
    REQUIRED_VARS
        FFmpeg_AVFORMAT_INCLUDE_DIR
        FFmpeg_AVFORMAT_LIBRARY
        FFmpeg_AVCODEC_LIBRARY
        FFmpeg_AVUTIL_LIBRARY
        FFmpeg_SWSCALE_LIBRARY
)

if(FFmpeg_FOUND)
    set(FFmpeg_INCLUDE_DIRS ${FFmpeg_AVFORMAT_INCLUDE_DIR})
    set(FFmpeg_LIBRARIES
        ${FFmpeg_AVFORMAT_LIBRARY}
        ${FFmpeg_AVCODEC_LIBRARY}
        ${FFmpeg_AVUTIL_LIBRARY}
        ${FFmpeg_SWSCALE_LIBRARY}
    )

    if(NOT TARGET FFmpeg::FFmpeg)
        add_library(FFmpeg::FFmpeg INTERFACE IMPORTED)
        set_target_properties(FFmpeg::FFmpeg PROPERTIES
            INTERFACE_INCLUDE_DIRECTORIES "${FFmpeg_INCLUDE_DIRS}"
            INTERFACE_LINK_LIBRARIES "${FFmpeg_LIBRARIES}"
        )
    endif()
endif()

mark_as_advanced(
    FFmpeg_AVFORMAT_INCLUDE_DIR
    FFmpeg_AVFORMAT_LIBRARY
    FFmpeg_AVCODEC_LIBRARY
    FFmpeg_AVUTIL_LIBRARY
    FFmpeg_SWSCALE_LIBRARY
)
