"""Pixel probe sampling: widget→image mapping, neighborhood extract, OCIO display sample."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class ProbeSample:
    image_x: int
    image_y: int
    width: int
    height: int
    frame: int
    source_rgba: tuple[float, float, float, float]
    display_rgb: tuple[float, float, float]
    neighborhood: np.ndarray  # float32 HxWx3 source space
    neighborhood_display: np.ndarray  # float32 HxWx3 display-referred (viewer match)
    pixel_aspect_ratio: float = 1.0


def magnifier_cell_size(pixel_aspect_ratio: float = 1.0, *, base_px: int = 12) -> tuple[int, int]:
    """Return (cell_w, cell_h) in magnifier pixels for one source texel.

    Matches viewer PAR: anamorphic (PAR>1) texels are wider than tall.
    """
    par = float(pixel_aspect_ratio) if pixel_aspect_ratio and pixel_aspect_ratio > 0.0 else 1.0
    cell_h = max(1, int(base_px))
    cell_w = max(1, int(round(base_px * par)))
    return cell_w, cell_h


def magnifier_texel_counts(
    view_w: int,
    view_h: int,
    pixel_aspect_ratio: float = 1.0,
    *,
    base_px: int = 12,
    min_cells: int = 5,
    max_radius: int = 64,
) -> tuple[int, int]:
    """Odd (cols, rows) of source texels so PAR-stretched cells fill a square view.

    Anamorphic (PAR=2) uses fewer columns than rows so the magnified pixmap
    stays roughly square while each texel cell is 2:1.
    """
    cell_w, cell_h = magnifier_cell_size(pixel_aspect_ratio, base_px=base_px)
    side = max(1, min(int(view_w), int(view_h)))
    cols = side // cell_w
    rows = side // cell_h

    def _odd_clamp(n: int) -> int:
        lo = min_cells if min_cells % 2 == 1 else min_cells + 1
        hi = 2 * max_radius + 1
        n = max(lo, min(hi, int(n)))
        if n % 2 == 0:
            n -= 1
        return max(lo, n)

    return _odd_clamp(cols), _odd_clamp(rows)


def widget_to_image_xy(
    widget_x: float,
    widget_y: float,
    widget_w: float,
    widget_h: float,
    scale_x: float,
    scale_y: float,
    zoom: float,
    pan_x_px: float,
    pan_y_px: float,
    image_w: int,
    image_h: int,
) -> tuple[int, int] | None:
    """Map widget coordinates to integer image pixel indices, or None if outside.

    Inverts the same NDC transform used by the viewport vertex shader:
    ``gl_Position = position * (fit_scale * zoom) + pan_ndc`` with widget Y-down
    and the shader's ``vUV.y = 1 - texCoord.y``.
    """
    if image_w <= 0 or image_h <= 0 or widget_w <= 0 or widget_h <= 0:
        return None
    sx = scale_x * zoom
    sy = scale_y * zoom
    if sx <= 0.0 or sy <= 0.0:
        return None
    # Match Viewport.update(): pan_y NDC is negated for Y-up clip space.
    pan_x = (pan_x_px / widget_w) * 2.0
    pan_y = -(pan_y_px / widget_h) * 2.0
    ndc_x = (widget_x / widget_w) * 2.0 - 1.0
    ndc_y = 1.0 - (widget_y / widget_h) * 2.0
    local_x = (ndc_x - pan_x) / sx
    local_y = (ndc_y - pan_y) / sy
    if local_x < -1.0 or local_x > 1.0 or local_y < -1.0 or local_y > 1.0:
        return None
    u = (local_x + 1.0) * 0.5
    v = (1.0 - local_y) * 0.5  # vertex UV flip
    x = int(np.floor(u * image_w))
    y = int(np.floor(v * image_h))
    x = max(0, min(image_w - 1, x))
    y = max(0, min(image_h - 1, y))
    return x, y


def extract_neighborhood(
    frame: np.ndarray,
    x: int,
    y: int,
    radius: int = 5,
    *,
    radius_x: int | None = None,
    radius_y: int | None = None,
) -> np.ndarray:
    """Return a patch centered on (x,y), edge-clamped. float32 HxWxC.

    ``radius`` sets both axes; ``radius_x`` / ``radius_y`` override per-axis
    (used so PAR-stretched cells still fill a square magnifier).
    """
    if frame.ndim != 3:
        raise ValueError("frame must be HxWxC")
    rx = int(radius if radius_x is None else radius_x)
    ry = int(radius if radius_y is None else radius_y)
    rx = max(0, rx)
    ry = max(0, ry)
    h, w, c = frame.shape
    out = np.zeros((2 * ry + 1, 2 * rx + 1, c), dtype=np.float32)
    for dy in range(-ry, ry + 1):
        for dx in range(-rx, rx + 1):
            sx = max(0, min(w - 1, x + dx))
            sy = max(0, min(h - 1, y + dy))
            out[dy + ry, dx + rx] = np.asarray(frame[sy, sx], dtype=np.float32)
    return out


def encode_levels(value: float) -> tuple[float, int, int]:
    """Return (float, 8-bit, 10-bit) encodings for a linear/display channel sample."""
    v = float(value)
    u8 = int(max(0, min(255, round(v * 255.0))))
    u10 = int(max(0, min(1023, round(v * 1023.0))))
    return v, u8, u10


def _ocio_cpu_processor(ocio_manager):
    """CPU processor matching viewer order: pre → ASC CDL → post."""
    if ocio_manager is None or getattr(ocio_manager, "config", None) is None:
        return None
    getter = getattr(ocio_manager, "get_cpu_processor", None)
    if callable(getter):
        try:
            return getter()
        except Exception:
            return None
    # Fallback for older managers: build CPU group if available.
    try:
        build = getattr(ocio_manager, "_build_cpu_transform_group", None)
        if callable(build):
            group = build()
        else:
            group = ocio_manager._build_transform_group()
        return ocio_manager.config.getProcessor(group).getDefaultCPUProcessor()
    except Exception:
        return None


def apply_ocio_rgb(ocio_manager, rgb: tuple[float, float, float]) -> tuple[float, float, float]:
    """CPU OCIO approx of the viewer graph (includes ASC CDL when active)."""
    cpu = _ocio_cpu_processor(ocio_manager)
    if cpu is None:
        return rgb
    try:
        pixel = np.array(rgb, dtype=np.float32)
        cpu.applyRGB(pixel)
        return float(pixel[0]), float(pixel[1]), float(pixel[2])
    except Exception:
        return rgb


def ocio_cpu_processor(ocio_manager):
    """Public wrapper: build a CPU processor snapshot for off-thread use."""
    return _ocio_cpu_processor(ocio_manager)


def apply_ocio_cpu_array(cpu_processor, rgb: np.ndarray) -> np.ndarray:
    """Apply a pre-built OCIO CPU processor to an HxWx3 (or Nx3) float array."""
    src = np.asarray(rgb, dtype=np.float32)
    if cpu_processor is None:
        return np.array(src, copy=True)
    try:
        if src.ndim == 2 and src.shape[1] == 3:
            out = np.array(src, copy=True, order="C")
            cpu_processor.applyRGB(out)
            return out
        if src.ndim == 3 and src.shape[2] >= 3:
            flat = np.array(src[..., :3].reshape(-1, 3), copy=True, order="C")
            cpu_processor.applyRGB(flat)
            return flat.reshape(src.shape[0], src.shape[1], 3)
        return np.array(src, copy=True)
    except Exception:
        return np.array(src, copy=True)


def apply_ocio_rgb_array(ocio_manager, rgb: np.ndarray) -> np.ndarray:
    """Apply viewer OCIO to an HxWx3 (or Nx3) float array; returns a new float32 array."""
    return apply_ocio_cpu_array(_ocio_cpu_processor(ocio_manager), rgb)


def sample_probe(
    frame: np.ndarray,
    x: int,
    y: int,
    *,
    timeline_frame: int,
    ocio_manager=None,
    radius: int = 5,
    radius_x: int | None = None,
    radius_y: int | None = None,
    pixel_aspect_ratio: float = 1.0,
) -> ProbeSample:
    h, w = frame.shape[0], frame.shape[1]
    x = max(0, min(w - 1, int(x)))
    y = max(0, min(h - 1, int(y)))
    pix = np.asarray(frame[y, x], dtype=np.float32)
    r = float(pix[0]) if pix.shape[0] > 0 else 0.0
    g = float(pix[1]) if pix.shape[0] > 1 else r
    b = float(pix[2]) if pix.shape[0] > 2 else r
    a = float(pix[3]) if pix.shape[0] > 3 else 1.0
    rx = int(radius if radius_x is None else radius_x)
    ry = int(radius if radius_y is None else radius_y)
    neighborhood = extract_neighborhood(frame, x, y, radius_x=rx, radius_y=ry)
    neighborhood_rgb = np.asarray(neighborhood[..., :3], dtype=np.float32)
    neighborhood_display = apply_ocio_rgb_array(ocio_manager, neighborhood_rgb)
    display = (
        float(neighborhood_display[ry, rx, 0]),
        float(neighborhood_display[ry, rx, 1]),
        float(neighborhood_display[ry, rx, 2]),
    )
    par = float(pixel_aspect_ratio) if pixel_aspect_ratio and pixel_aspect_ratio > 0.0 else 1.0
    return ProbeSample(
        image_x=x,
        image_y=y,
        width=w,
        height=h,
        frame=int(timeline_frame),
        source_rgba=(r, g, b, a),
        display_rgb=display,
        neighborhood=neighborhood_rgb,
        neighborhood_display=neighborhood_display,
        pixel_aspect_ratio=par,
    )
