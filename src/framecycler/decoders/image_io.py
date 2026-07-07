"""OpenImageIO wrapper for still-image read paths (EXR, DPX, etc.)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

import numpy as np

try:
    import OpenImageIO as oiio
except ImportError as exc:
    raise ImportError(
        "OpenImageIO is required for EXR/DPX decoding. Install with: pip install OpenImageIO"
    ) from exc


SQUARE_PIXEL_ASPECT = 1.0
ANAMORPHIC_PIXEL_ASPECT = 2.0


@dataclass
class ImageMetadata:
    width: int
    height: int
    channel_names: List[str] = field(default_factory=list)
    layers: List[str] = field(default_factory=list)
    transfer_characteristic: int = 0
    colorimetric_specification: int = 0
    pixel_aspect_ratio: float = SQUARE_PIXEL_ASPECT


def _read_spec(path: str) -> "oiio.ImageSpec":
    inp = oiio.ImageInput.open(path)
    if inp is None:
        raise ValueError(f"Failed to open image: {path} ({oiio.geterror()})")
    try:
        return inp.spec()
    finally:
        inp.close()


def list_layers(path: str) -> List[str]:
    channel_names = list(_read_spec(path).channelnames)
    layers = set()
    for name in channel_names:
        if "." in name:
            layers.add(name.split(".", 1)[0])
        elif name in {"R", "G", "B", "A", "Y", "Z"}:
            layers.add("beauty")
        else:
            layers.add(name)
    if not layers and channel_names:
        layers.add("beauty")
    return sorted(layers)


def _layer_channel_indices(channel_names: Sequence[str], layer: Optional[str]) -> List[int]:
    names = list(channel_names)
    if not names:
        raise ValueError("Image has no channels")

    if layer:
        component_map = {}
        for idx, name in enumerate(names):
            if name == layer:
                component_map.setdefault("R", idx)
            elif name.startswith(f"{layer}."):
                component = name.split(".", 1)[1]
                component_map[component] = idx
        for components in (("R", "G", "B"), ("Y",)):
            if all(comp in component_map for comp in components):
                indices = [component_map[comp] for comp in components]
                if components == ("R", "G", "B") and "A" in component_map:
                    indices.append(component_map["A"])
                return indices

    flat = {name: idx for idx, name in enumerate(names)}
    if all(ch in flat for ch in ("R", "G", "B")):
        indices = [flat["R"], flat["G"], flat["B"]]
        if "A" in flat:
            indices.append(flat["A"])
        return indices

    if len(names) >= 3:
        return list(range(min(3, len(names))))

    return [0]


def _display_channels(channel_names: Sequence[str]) -> List[str]:
    names = list(channel_names)
    if all(ch in names for ch in ("R", "G", "B")):
        channels = ["R", "G", "B"]
        if "A" in names:
            channels.append("A")
        return channels
    return sorted(set(names))


def _read_pixel_aspect_ratio(spec: "oiio.ImageSpec") -> float:
    par = 0.0
    if hasattr(spec, "pixelaspect"):
        par = float(spec.pixelaspect)
    if par <= 0.0:
        par = float(spec.get_float_attribute("pixelAspectRatio", 0.0))
    if par <= 0.0:
        par = float(spec.get_float_attribute("PixelAspectRatio", 0.0))
    if par <= 0.0 or not np.isfinite(par):
        return SQUARE_PIXEL_ASPECT
    return par


def _read_dpx_header_bytes(path: str) -> Tuple[int, int]:
    transfer_char = 0
    colorimetric = 0
    try:
        with open(path, "rb") as handle:
            handle.seek(801)
            data = handle.read(2)
            if len(data) == 2:
                transfer_char = data[0]
                colorimetric = data[1]
    except OSError:
        pass
    return transfer_char, colorimetric


def read_metadata(path: str) -> ImageMetadata:
    spec = _read_spec(path)
    channel_names = list(spec.channelnames)
    transfer_char, colorimetric = _read_dpx_header_bytes(path)
    return ImageMetadata(
        width=spec.width,
        height=spec.height,
        channel_names=channel_names,
        layers=list_layers(path),
        transfer_characteristic=transfer_char,
        colorimetric_specification=colorimetric,
        pixel_aspect_ratio=_read_pixel_aspect_ratio(spec),
    )


def downsample_pixels(arr: np.ndarray, scale: float) -> np.ndarray:
    """Resize a float16 (H, W, C) array using OIIO box filter."""
    scale = max(0.01, min(1.0, float(scale)))
    if scale >= 1.0:
        return np.ascontiguousarray(arr)

    if arr.ndim == 2:
        arr = arr[..., np.newaxis]

    height, width, channels = arr.shape
    new_w = max(1, round(width * scale))
    new_h = max(1, round(height * scale))
    if new_w == width and new_h == height:
        return np.ascontiguousarray(arr)

    channel_names = ["R", "G", "B", "A"][:channels]
    if channels == 1:
        channel_names = ["Y"]

    src_spec = oiio.ImageSpec(width, height, channels, oiio.HALF)
    src_spec.channelnames = channel_names
    src_buf = oiio.ImageBuf(src_spec)
    roi = oiio.ROI(0, width, 0, height, 0, 1, 0, channels)
    if not src_buf.set_pixels(roi, arr):
        raise ValueError(f"Failed to set source pixels: {src_buf.geterror()}")

    dst_spec = oiio.ImageSpec(new_w, new_h, channels, oiio.HALF)
    dst_spec.channelnames = channel_names
    dst_buf = oiio.ImageBuf(dst_spec)
    if not oiio.ImageBufAlgo.resize(dst_buf, src_buf, filtername="box"):
        raise ValueError(f"Failed to resize image: {dst_buf.geterror()}")

    out = np.asarray(dst_buf.get_pixels(oiio.HALF), dtype=np.float16)
    if out.ndim == 2:
        out = out[..., np.newaxis]
    return np.ascontiguousarray(out)


def _resize_image_buf(buf: "oiio.ImageBuf", scale: float) -> "oiio.ImageBuf":
    spec = buf.spec()
    new_w = max(1, round(spec.width * scale))
    new_h = max(1, round(spec.height * scale))
    if new_w == spec.width and new_h == spec.height:
        return buf

    dst = oiio.ImageBuf(oiio.ImageSpec(new_w, new_h, spec.nchannels, oiio.HALF))
    if not oiio.ImageBufAlgo.resize(dst, buf, filtername="box"):
        raise ValueError(f"Failed to resize image {buf.geterror()}: {dst.geterror()}")
    return dst


def read_pixels(path: str, layer: Optional[str] = None, resolution_scale: float = 1.0) -> np.ndarray:
    buf = oiio.ImageBuf(path)
    if buf.has_error:
        raise ValueError(f"Failed to read image {path}: {buf.geterror()}")

    spec = buf.spec()
    channel_names = list(spec.channelnames)
    indices = _layer_channel_indices(channel_names, layer)

    if resolution_scale < 1.0:
        buf = _resize_image_buf(buf, resolution_scale)

    arr = np.asarray(buf.get_pixels(oiio.HALF), dtype=np.float16)
    if arr.ndim == 2:
        arr = arr[..., np.newaxis]
    arr = arr[..., indices]
    return np.ascontiguousarray(arr)


def display_channels_for_metadata(meta: ImageMetadata) -> List[str]:
    return _display_channels(meta.channel_names)
