"""Downsample + CPU scope accumulators (waveform, parade, vector, hist, CIE)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

import numpy as np

from .cie_data import REC709_TO_XYZ

try:
    from ... import framecycler_engine as _engine
except Exception:  # pragma: no cover
    try:
        import framecycler_engine as _engine
    except Exception:
        _engine = None


class ScopeType(str, Enum):
    WAVEFORM = "waveform"
    PARADE = "parade"
    VECTORSCOPE = "vectorscope"
    HISTOGRAM = "histogram"
    CIE = "cie"


WAVEFORM_BINS = 1024  # 10-bit 0–1023 columns
VECTOR_SIZE = 256
HIST_BINS = 256
CIE_GRID = 256
DEFAULT_MAX_WIDTH = 512
PLAY_MAX_WIDTH = 320


@dataclass(frozen=True)
class ScopeFrame:
    """Display-referred RGB float32 HxWx3 in [0,1+] (may exceed 1)."""

    rgb: np.ndarray  # float32 HxWx3
    frame_index: int = -1


def has_native_scopes() -> bool:
    return _engine is not None and hasattr(_engine, "downsample_frame") and hasattr(
        _engine, "accumulate_scopes"
    )


def downsample_rgb(rgb: np.ndarray, max_width: int = DEFAULT_MAX_WIDTH) -> np.ndarray:
    """Downsample HxWx{3,4} to at most *max_width* columns (nearest / stride)."""
    if rgb is None or rgb.ndim != 3 or rgb.shape[2] < 3:
        raise ValueError("rgb must be HxWx3+")
    h, w, _ = rgb.shape
    arr = np.asarray(rgb[..., :3], dtype=np.float32)
    if w <= max_width:
        return arr
    step = max(1, int(np.ceil(w / float(max_width))))
    return arr[:, ::step, :].copy()


def downsample_from_cache(native_cache, frame_index: int, max_width: int = DEFAULT_MAX_WIDTH):
    """Downsample a cached frame to float32 HxWx3. Prefers C++ under-lock path."""
    if has_native_scopes():
        out = _engine.downsample_frame(native_cache, int(frame_index), int(max_width))
        if out is not None:
            return np.asarray(out, dtype=np.float32)
    if native_cache is None or not hasattr(native_cache, "get_frame_data"):
        return None
    arr = native_cache.get_frame_data(int(frame_index))
    if arr is None:
        return None
    return downsample_rgb(np.asarray(arr), max_width=max_width)


def luma_rec709(rgb: np.ndarray) -> np.ndarray:
    r = rgb[..., 0]
    g = rgb[..., 1]
    b = rgb[..., 2]
    return (0.2126 * r + 0.7152 * g + 0.0722 * b).astype(np.float32, copy=False)


def accumulate_waveform(
    rgb: np.ndarray,
    *,
    bins: int = WAVEFORM_BINS,
    channel: str = "luma",
) -> np.ndarray:
    """Return float32 [bins, width] column-intensity map (low = bottom)."""
    h, w, _ = rgb.shape
    if channel == "r":
        sig = rgb[..., 0]
    elif channel == "g":
        sig = rgb[..., 1]
    elif channel == "b":
        sig = rgb[..., 2]
    else:
        sig = luma_rec709(rgb)
    y = np.clip(sig * (bins - 1), 0, bins - 1).astype(np.int32)
    out = np.zeros((bins, w), dtype=np.float32)
    xs = np.broadcast_to(np.arange(w, dtype=np.int32), (h, w))
    np.add.at(out, (y.ravel(), xs.ravel()), 1.0)
    return out


def accumulate_parade(rgb: np.ndarray, *, bins: int = WAVEFORM_BINS) -> np.ndarray:
    """Return float32 [bins, 3*width] R|G|B parade side-by-side."""
    r = accumulate_waveform(rgb, bins=bins, channel="r")
    g = accumulate_waveform(rgb, bins=bins, channel="g")
    b = accumulate_waveform(rgb, bins=bins, channel="b")
    return np.concatenate([r, g, b], axis=1)


def accumulate_histogram(rgb: np.ndarray, *, bins: int = HIST_BINS) -> np.ndarray:
    """Return float32 [3, bins] counts for R,G,B (0..1 mapped)."""
    out = np.zeros((3, bins), dtype=np.float32)
    for c in range(3):
        v = np.clip(rgb[..., c] * (bins - 1), 0, bins - 1).astype(np.int32)
        counts = np.bincount(v.ravel(), minlength=bins).astype(np.float32)
        out[c, :bins] = counts[:bins]
    return out


def accumulate_vectorscope(
    rgb: np.ndarray,
    *,
    size: int = VECTOR_SIZE,
) -> np.ndarray:
    """Return float32 [size, size] Cb/Cr density (Rec.709 YCbCr, centered)."""
    r = rgb[..., 0]
    g = rgb[..., 1]
    b = rgb[..., 2]
    cb = -0.114572 * r - 0.385428 * g + 0.5 * b
    cr = 0.5 * r - 0.454153 * g - 0.045847 * b
    half = (size - 1) * 0.5
    xi = np.clip(np.rint(cb * size + half), 0, size - 1).astype(np.int32)
    yi = np.clip(np.rint(-cr * size + half), 0, size - 1).astype(np.int32)
    out = np.zeros((size, size), dtype=np.float32)
    np.add.at(out, (yi.ravel(), xi.ravel()), 1.0)
    return out


def rgb_to_xy(rgb: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Linear-ish RGB → CIE xy (Rec.709 matrix). Clamps negatives lightly."""
    r = np.maximum(rgb[..., 0], 0.0)
    g = np.maximum(rgb[..., 1], 0.0)
    b = np.maximum(rgb[..., 2], 0.0)
    m = REC709_TO_XYZ
    x = m[0][0] * r + m[0][1] * g + m[0][2] * b
    y = m[1][0] * r + m[1][1] * g + m[1][2] * b
    z = m[2][0] * r + m[2][1] * g + m[2][2] * b
    s = x + y + z
    ok = s > 1e-8
    xx = np.zeros_like(s, dtype=np.float32)
    yy = np.zeros_like(s, dtype=np.float32)
    xx[ok] = (x[ok] / s[ok]).astype(np.float32)
    yy[ok] = (y[ok] / s[ok]).astype(np.float32)
    return xx, yy


def accumulate_cie(rgb: np.ndarray, *, grid: int = CIE_GRID) -> np.ndarray:
    """Return float32 [grid, grid] density in CIE xy (x→right, y→up)."""
    xx, yy = rgb_to_xy(rgb)
    xi = np.clip(np.rint(xx / 0.8 * (grid - 1)), 0, grid - 1).astype(np.int32)
    yi = np.clip(np.rint((1.0 - yy / 0.9) * (grid - 1)), 0, grid - 1).astype(np.int32)
    out = np.zeros((grid, grid), dtype=np.float32)
    mask = (xx + yy) > 1e-6
    if np.any(mask):
        np.add.at(out, (yi[mask], xi[mask]), 1.0)
    return out


def analyze(
    rgb: np.ndarray,
    scope_type: ScopeType,
) -> np.ndarray:
    """Run accumulator for *scope_type* on display RGB (Python fallback)."""
    if scope_type == ScopeType.WAVEFORM:
        return accumulate_waveform(rgb)
    if scope_type == ScopeType.PARADE:
        return accumulate_parade(rgb)
    if scope_type == ScopeType.VECTORSCOPE:
        return accumulate_vectorscope(rgb)
    if scope_type == ScopeType.HISTOGRAM:
        return accumulate_histogram(rgb)
    if scope_type == ScopeType.CIE:
        return accumulate_cie(rgb)
    raise ValueError(f"unknown scope type: {scope_type}")


def analyze_many(rgb: np.ndarray, scope_types: tuple[ScopeType, ...]) -> list[np.ndarray]:
    """Accumulate multiple scope types; uses C++ when available."""
    if has_native_scopes() and rgb is not None and rgb.size:
        arr = np.ascontiguousarray(rgb[..., :3], dtype=np.float32)
        names = [st.value for st in scope_types]
        try:
            return list(_engine.accumulate_scopes(arr, names))
        except Exception:
            pass
    return [analyze(rgb, st) for st in scope_types]


def apply_display_transform(
    small_rgb: np.ndarray,
    *,
    ocio_manager: Optional[object] = None,
    cpu_processor: Optional[object] = None,
) -> np.ndarray:
    from ..probe_sampling import apply_ocio_cpu_array, apply_ocio_rgb_array

    small = np.nan_to_num(small_rgb, nan=0.0, posinf=0.0, neginf=0.0)
    if cpu_processor is not None:
        out = apply_ocio_cpu_array(cpu_processor, small)
    else:
        out = apply_ocio_rgb_array(ocio_manager, small)
    out = np.nan_to_num(out, nan=0.0, posinf=0.0, neginf=0.0)
    return np.clip(out, 0.0, None).astype(np.float32, copy=False)


def prepare_scope_rgb(
    source_rgb: np.ndarray,
    *,
    max_width: int = DEFAULT_MAX_WIDTH,
    ocio_manager: Optional[object] = None,
    cpu_processor: Optional[object] = None,
) -> np.ndarray:
    """Downsample then optional OCIO display transform (CPU)."""
    small = downsample_rgb(source_rgb, max_width=max_width)
    return apply_display_transform(
        small, ocio_manager=ocio_manager, cpu_processor=cpu_processor
    )


def compute_scope_accumulators(
    source_rgb: np.ndarray,
    scope_types: tuple[ScopeType, ...],
    *,
    max_width: int = DEFAULT_MAX_WIDTH,
    cpu_processor: Optional[object] = None,
) -> list[np.ndarray]:
    """Downsample + OCIO + analyze for each scope type (array input path)."""
    rgb = prepare_scope_rgb(
        source_rgb,
        max_width=max_width,
        cpu_processor=cpu_processor,
    )
    return analyze_many(rgb, scope_types)


def compute_scopes_from_cache(
    native_cache,
    frame_index: int,
    scope_types: tuple[ScopeType, ...],
    *,
    max_width: int = DEFAULT_MAX_WIDTH,
    cpu_processor: Optional[object] = None,
) -> list[np.ndarray] | None:
    """Fast path: C++ downsample under lock → CPU OCIO → C++/Python accumulate."""
    small = downsample_from_cache(native_cache, frame_index, max_width=max_width)
    if small is None:
        return None
    rgb = apply_display_transform(small, cpu_processor=cpu_processor)
    return analyze_many(rgb, scope_types)
