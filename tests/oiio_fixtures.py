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


def write_scattered_channel_exr(path: Path, width: int = 32, height: int = 16) -> None:
    """Non-contiguous RGB: Z, R, A, G, B — exercises gather fallback."""
    require_oiio()
    spec = oiio.ImageSpec(width, height, 5, oiio.FLOAT)
    spec.channelnames = ("Z", "R", "A", "G", "B")
    pixels = np.zeros((height, width, 5), dtype=np.float32)
    pixels[..., 0] = 0.1  # Z
    pixels[..., 1] = 0.2  # R
    pixels[..., 2] = 0.9  # A
    pixels[..., 3] = 0.4  # G
    pixels[..., 4] = 0.6  # B
    out = oiio.ImageOutput.create(str(path))
    if out is None or not out.open(str(path), spec) or not out.write_image(pixels):
        raise RuntimeError(f"Failed to write scattered EXR: {oiio.geterror()}")
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


def write_tiled_float_exr(
    path: Path,
    width: int = 128,
    height: int = 96,
    value: float = 0.5,
    tile_size: int = 64,
) -> None:
    """Constant-fill tiled OpenEXR for proxy tile-read tests."""
    require_oiio()
    spec = oiio.ImageSpec(width, height, 3, oiio.FLOAT)
    spec.channelnames = ("R", "G", "B")
    spec.tile_width = tile_size
    spec.tile_height = tile_size
    rgb = np.full((height, width, 3), value, dtype=np.float32)
    out = oiio.ImageOutput.create(str(path))
    if out is None or not out.open(str(path), spec) or not out.write_image(rgb):
        raise RuntimeError(f"Failed to write tiled EXR: {oiio.geterror()}")
    out.close()


def write_mipmapped_float_exr(
    path: Path,
    width: int = 64,
    height: int = 32,
    base_value: float = 0.5,
) -> None:
    """Constant-fill EXR with MIP levels via ImageBufAlgo.make_texture."""
    require_oiio()
    if width < 2 or height < 2:
        raise ValueError("mipmapped EXR needs width/height >= 2")

    spec = oiio.ImageSpec(width, height, 3, oiio.FLOAT)
    spec.channelnames = ("R", "G", "B")
    buf = oiio.ImageBuf(spec)
    if not oiio.ImageBufAlgo.fill(buf, (base_value, base_value, base_value)):
        raise RuntimeError(f"Failed to fill mip source: {oiio.geterror()}")
    config = oiio.ImageSpec()
    if not oiio.ImageBufAlgo.make_texture(oiio.MakeTxTexture, buf, str(path), config):
        raise RuntimeError(f"Failed to write mipmapped EXR: {oiio.geterror()}")
