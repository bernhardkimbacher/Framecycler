"""Shared OIIO test fixture writers."""

from __future__ import annotations

from pathlib import Path

import numpy as np

try:
    import OpenImageIO as oiio
except ImportError:
    oiio = None


def require_oiio():
    if oiio is None:
        raise ImportError("OpenImageIO is required for decoder tests")


def write_float_exr(path: Path, width: int = 32, height: int = 16, value: float = 0.5, pixel_aspect: float = 1.0) -> None:
    require_oiio()
    spec = oiio.ImageSpec(width, height, 3, oiio.FLOAT)
    spec.channelnames = ("R", "G", "B")
    if pixel_aspect != 1.0:
        spec.attribute("pixelAspectRatio", pixel_aspect)
    rgb = np.full((height, width, 3), value, dtype=np.float32)
    out = oiio.ImageOutput.create(str(path))
    if out is None or not out.open(str(path), spec) or not out.write_image(rgb):
        raise RuntimeError(f"Failed to write EXR: {oiio.geterror()}")
    out.close()


def write_layered_exr(path: Path, width: int = 32, height: int = 16) -> None:
    require_oiio()
    spec = oiio.ImageSpec(width, height, 6, oiio.FLOAT)
    spec.channelnames = ("beauty.R", "beauty.G", "beauty.B", "depth.R", "depth.G", "depth.B")
    beauty = np.full((height, width, 3), 0.25, dtype=np.float32)
    depth = np.full((height, width, 3), 0.75, dtype=np.float32)
    pixels = np.concatenate([beauty, depth], axis=2)
    out = oiio.ImageOutput.create(str(path))
    if out is None or not out.open(str(path), spec) or not out.write_image(pixels):
        raise RuntimeError(f"Failed to write layered EXR: {oiio.geterror()}")
    out.close()


def write_uint16_dpx(path: Path, width: int = 32, height: int = 16) -> None:
    require_oiio()
    spec = oiio.ImageSpec(width, height, 3, oiio.UINT16)
    spec.channelnames = ("R", "G", "B")
    rgb = np.full((height, width, 3), 32768, dtype=np.uint16)
    out = oiio.ImageOutput.create(str(path))
    if out is None or not out.open(str(path), spec) or not out.write_image(rgb):
        raise RuntimeError(f"Failed to write DPX: {oiio.geterror()}")
    out.close()
