"""Selective PyInstaller binary filtering for Framecycler bundles."""

from __future__ import annotations

import re
from typing import Iterable

# Drop unused Qt modules / tooling that Analysis may pull transitively.
_DENY_SUBSTR = (
    "QtWebEngine",
    "QtWebView",
    "Qt3D",
    "QtBluetooth",
    "QtNfc",
    "QtSensors",
    "QtPositioning",
    "QtLocation",
    "QtCharts",
    "QtDataVisualization",
    "QtVirtualKeyboard",
    "QtQuick3D",
    "QtPdf",
    "QtDesigner",
    "QtHelp",
    "QtTest",
    "QtQml",
    "QtQuick",
    "designer",
    "linguist",
    "lupdate",
    "lrelease",
    "qml",
    "qmllint",
)

# Keep engine, OIIO/FFmpeg/OpenCV stacks, and core Qt GUI/RHI bits.
_ALLOW_SUBSTR = (
    "framecycler_engine",
    "OpenImageIO",
    "OpenEXR",
    "Imath",
    "OpenColorIO",
    "avcodec",
    "avformat",
    "avutil",
    "swscale",
    "swresample",
    "opencv",
    "Qt6Core",
    "Qt6Gui",
    "Qt6Widgets",
    "Qt6ShaderTools",
    "Qt6Network",
    "Qt6DBus",
    "Qt6Svg",
    "Qt6OpenGL",
    "QtCore",
    "QtGui",
    "QtWidgets",
    "QtShaderTools",
    "QtNetwork",
    "QtDBus",
    "QtSvg",
    "QtOpenGL",
    "platforms",
    "imageformats",
    "styles",
    "tls",
    "networkinformation",
    "generic",
    "iconengines",
    "qsb",
    "libpng",
    "libjpeg",
    "libtiff",
    "libwebp",
    "zlib",
    "freetype",
    "harfbuzz",
    "icu",
    "pcre",
    "glib",
    "double-conversion",
)

_DENY_RE = re.compile("|".join(re.escape(s) for s in _DENY_SUBSTR), re.IGNORECASE)


def _name(entry) -> str:
    if isinstance(entry, (tuple, list)) and entry:
        return str(entry[0])
    return str(entry)


def should_keep_binary(entry) -> bool:
    """Return True if a PyInstaller binary TOC entry should be shipped."""
    path = _name(entry)
    base = path.replace("\\", "/")
    if _DENY_RE.search(base):
        return False
    # Always keep anything that looks like a required native dep or Qt core.
    lower = base.lower()
    if any(token.lower() in lower for token in _ALLOW_SUBSTR):
        return True
    # Drop unknown heavyweight libs by default; Analysis still keeps Python ext.
    if lower.endswith((".dll", ".so", ".dylib")) or ".framework/" in lower:
        # Conservative: keep remaining libs — deny list already removed bloat.
        return True
    return True


def filter_binaries(binaries: Iterable) -> list:
    kept = [b for b in binaries if should_keep_binary(b)]
    return kept


# Modules PyInstaller should not pull into the archive.
PYINSTALLER_EXCLUDES = [
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebEngineQuick",
    "PySide6.Qt3DCore",
    "PySide6.Qt3DRender",
    "PySide6.QtBluetooth",
    "PySide6.QtNfc",
    "PySide6.QtSensors",
    "PySide6.QtPositioning",
    "PySide6.QtLocation",
    "PySide6.QtCharts",
    "PySide6.QtDataVisualization",
    "PySide6.QtQuick",
    "PySide6.QtQuick3D",
    "PySide6.QtQml",
    "PySide6.QtDesigner",
    "tkinter",
    "matplotlib",
    "scipy",
]
