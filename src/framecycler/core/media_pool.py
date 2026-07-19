"""Ref-counted media pool mapping filesystem paths to MediaSource runtimes."""

from __future__ import annotations

import os
from typing import Callable, Dict, Optional, Tuple

from ..decoders.base import BaseDecoder
from ..decoders.dpx_decoder import DPXDecoder
from ..decoders.exr_decoder import EXRDecoder
from ..decoders.image_io import SQUARE_PIXEL_ASPECT
from ..decoders.qt_decoder import QuickTimeDecoder
from .cache import CacheEngine
from .media_source import MediaSource
from .settings import Settings

FrameReadyCallback = Callable[[str, int], None]
DecoderFactory = Callable[[str], BaseDecoder]
DecoderResolver = Callable[[str], Optional[DecoderFactory]]


class MediaPool:
    """Owns decoder + CacheEngine instances keyed by absolute media path."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._entries: Dict[str, Tuple[MediaSource, int]] = {}
        self._frame_ready_callback: Optional[FrameReadyCallback] = None
        self._decoder_resolver: Optional[DecoderResolver] = None

    def set_frame_ready_callback(self, callback: Optional[FrameReadyCallback]) -> None:
        self._frame_ready_callback = callback

    def set_decoder_resolver(self, resolver: Optional[DecoderResolver]) -> None:
        self._decoder_resolver = resolver

    def get(self, path: str) -> Optional[MediaSource]:
        key = self._key(path)
        entry = self._entries.get(key)
        return entry[0] if entry else None

    def acquire(self, path: str) -> MediaSource:
        key = self._key(path)
        entry = self._entries.get(key)
        if entry is not None:
            source, refs = entry
            self._entries[key] = (source, refs + 1)
            return source

        source = self._load_source(key)
        source_key = key

        def _on_ready(frame_index: int, p: str = source_key) -> None:
            if self._frame_ready_callback is not None:
                self._frame_ready_callback(p, frame_index)

        source.cache.add_frame_ready_callback(_on_ready)
        source.cache.start()
        self._entries[key] = (source, 1)
        return source

    def release(self, path: str) -> None:
        key = self._key(path)
        entry = self._entries.get(key)
        if entry is None:
            return
        source, refs = entry
        refs -= 1
        if refs <= 0:
            source.cache.close()
            del self._entries[key]
        else:
            self._entries[key] = (source, refs)

    def clear(self) -> None:
        for source, _ in list(self._entries.values()):
            source.cache.close()
        self._entries.clear()

    def paths(self) -> list[str]:
        return list(self._entries.keys())

    def sources(self) -> list[MediaSource]:
        return [source for source, _ in self._entries.values()]

    @staticmethod
    def _key(path: str) -> str:
        return os.path.abspath(path)

    def _load_source(self, path: str) -> MediaSource:
        ext = os.path.splitext(path)[1].lower()
        decoder = None
        if self._decoder_resolver is not None:
            factory = self._decoder_resolver(ext)
            if factory is not None:
                decoder = factory(path)
        if decoder is None:
            if ext == ".exr":
                decoder = EXRDecoder(path)
            elif ext == ".dpx":
                decoder = DPXDecoder(path)
            else:
                decoder = QuickTimeDecoder(path)

        cache = CacheEngine(decoder, self.settings, resolution_scale=1.0)
        meta = decoder.get_metadata()
        frame_count = int(meta.get("frame_count", 0))
        return MediaSource(
            path=path,
            decoder=decoder,
            cache=cache,
            frame_count=frame_count,
            fps=float(meta.get("fps", self.settings.default_fps)),
            decoder_start_frame=int(meta.get("start_frame", 0)),
            width=int(meta.get("width", 0)),
            height=int(meta.get("height", 0)),
            pixel_aspect_ratio=float(meta.get("pixel_aspect_ratio", SQUARE_PIXEL_ASPECT)),
            metadata=meta,
            offline=False,
        )

    def make_offline_placeholder(self, path: str, metadata: Optional[dict] = None) -> MediaSource:
        """Create a non-decodable placeholder MediaSource for offline clips."""
        metadata = dict(metadata or {})
        return MediaSource(
            path=path or "<offline>",
            decoder=None,  # type: ignore[arg-type]
            cache=None,  # type: ignore[arg-type]
            frame_count=int(metadata.get("frame_count", 0) or 0),
            fps=float(metadata.get("fps", self.settings.default_fps) or self.settings.default_fps),
            decoder_start_frame=int(metadata.get("start_frame", 0) or 0),
            width=int(metadata.get("width", 0) or 0),
            height=int(metadata.get("height", 0) or 0),
            pixel_aspect_ratio=float(metadata.get("pixel_aspect_ratio", SQUARE_PIXEL_ASPECT) or 1.0),
            metadata=metadata,
            offline=True,
        )
