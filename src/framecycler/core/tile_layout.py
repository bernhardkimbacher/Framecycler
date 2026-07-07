from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Sequence, Tuple


@dataclass(frozen=True)
class TileLayout:
    source_index: int
    scale_x: float
    scale_y: float
    offset_x: float
    offset_y: float


def _pixel_rect_to_ndc(
    x: float,
    y: float,
    w: float,
    h: float,
    widget_w: float,
    widget_h: float,
) -> Tuple[float, float, float, float]:
    left = (x / widget_w) * 2.0 - 1.0
    right = ((x + w) / widget_w) * 2.0 - 1.0
    top = 1.0 - (y / widget_h) * 2.0
    bottom = 1.0 - ((y + h) / widget_h) * 2.0
    scale_x = (right - left) * 0.5
    scale_y = (top - bottom) * 0.5
    offset_x = (right + left) * 0.5
    offset_y = (top + bottom) * 0.5
    return scale_x, scale_y, offset_x, offset_y


def compute_tile_layouts(
    source_sizes: Sequence[Tuple[int, int]],
    pixel_aspects: Sequence[float],
    widget_w: int,
    widget_h: int,
) -> List[TileLayout]:
    """Compute aspect-preserving tile transforms for N sources in a grid."""
    count = len(source_sizes)
    if count <= 0 or widget_w <= 0 or widget_h <= 0:
        return []

    if count == 1:
        width, height = source_sizes[0]
        par = pixel_aspects[0] if pixel_aspects else 1.0
        if width <= 0 or height <= 0:
            return []
        return [_fit_rect_to_widget(width, height, par, widget_w, widget_h, 0)]

    cols = math.ceil(math.sqrt(count))
    rows = math.ceil(count / cols)
    cell_w = widget_w / cols
    cell_h = widget_h / rows
    layouts: List[TileLayout] = []

    for index, (width, height) in enumerate(source_sizes):
        if width <= 0 or height <= 0:
            continue
        par = pixel_aspects[index] if index < len(pixel_aspects) else 1.0
        col = index % cols
        row = index // cols
        cell_x = col * cell_w
        cell_y = row * cell_h
        aspect = (width * par) / height
        cell_aspect = cell_w / cell_h
        if cell_aspect > aspect:
            draw_h = cell_h
            draw_w = draw_h * aspect
        else:
            draw_w = cell_w
            draw_h = draw_w / aspect
        draw_x = cell_x + (cell_w - draw_w) * 0.5
        draw_y = cell_y + (cell_h - draw_h) * 0.5
        scale_x, scale_y, offset_x, offset_y = _pixel_rect_to_ndc(
            draw_x,
            draw_y,
            draw_w,
            draw_h,
            widget_w,
            widget_h,
        )
        layouts.append(
            TileLayout(
                source_index=index,
                scale_x=scale_x,
                scale_y=scale_y,
                offset_x=offset_x,
                offset_y=offset_y,
            )
        )
    return layouts


def _fit_rect_to_widget(
    width: int,
    height: int,
    par: float,
    widget_w: int,
    widget_h: int,
    source_index: int,
) -> TileLayout:
    aspect = (width * par) / height
    widget_aspect = widget_w / widget_h
    if widget_aspect > aspect:
        draw_h = widget_h
        draw_w = draw_h * aspect
    else:
        draw_w = widget_w
        draw_h = draw_w / aspect
    draw_x = (widget_w - draw_w) * 0.5
    draw_y = (widget_h - draw_h) * 0.5
    scale_x, scale_y, offset_x, offset_y = _pixel_rect_to_ndc(
        draw_x,
        draw_y,
        draw_w,
        draw_h,
        widget_w,
        widget_h,
    )
    return TileLayout(
        source_index=source_index,
        scale_x=scale_x,
        scale_y=scale_y,
        offset_x=offset_x,
        offset_y=offset_y,
    )
