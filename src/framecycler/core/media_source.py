from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, List, Optional

if TYPE_CHECKING:
    from ..decoders.base import BaseDecoder
    from .cache import CacheEngine


@dataclass
class MediaSource:
    """Runtime decode/cache resource for a single media path (no timeline state)."""

    path: str
    decoder: Optional["BaseDecoder"]
    cache: Optional["CacheEngine"]
    frame_count: int
    fps: float
    decoder_start_frame: int = 0
    width: int = 0
    height: int = 0
    pixel_aspect_ratio: float = 1.0
    resolution_scale: float = 1.0
    metadata: dict = field(default_factory=dict)
    offline: bool = False

    @property
    def display_name(self) -> str:
        return os.path.basename(self.path) if self.path else "<offline>"


def _decoder_frame_numbers(source: MediaSource) -> List[int] | None:
    if source.decoder is None:
        return None
    frame_numbers = getattr(source.decoder, "frame_numbers", None)
    if frame_numbers:
        return list(frame_numbers)
    return None


def local_index_to_decoder_frame(source: MediaSource, local_index: int) -> int:
    """Map a 0-based index within a source to the decoder frame number."""
    if source.frame_count <= 0:
        return source.decoder_start_frame
    local_index = max(0, min(source.frame_count - 1, local_index))
    frame_numbers = _decoder_frame_numbers(source)
    if frame_numbers and local_index < len(frame_numbers):
        return frame_numbers[local_index]
    return source.decoder_start_frame + local_index


def decoder_frame_to_local_index(source: MediaSource, decoder_frame: int) -> int:
    frame_numbers = _decoder_frame_numbers(source)
    if frame_numbers:
        try:
            return frame_numbers.index(decoder_frame)
        except ValueError:
            closest = min(range(len(frame_numbers)), key=lambda i: abs(frame_numbers[i] - decoder_frame))
            return closest
    return max(0, min(source.frame_count - 1, decoder_frame - source.decoder_start_frame))
