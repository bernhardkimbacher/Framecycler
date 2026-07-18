import threading
from typing import Any, Callable, Dict, List, Set

import numpy as np

from ..decoders.base import BaseDecoder
from .settings import Settings
from .timecode import Timecode

# Try importing the compiled C++ engine extension module
try:
    from .. import framecycler_engine
except ImportError:
    import framecycler_engine


class CacheEngine:
    """Python façade over C++ CacheManager + PrefetchEngine.

    Prefetch scheduling and native OIIO decode run in C++. QuickTime / non-native
    decode still executes in Python via a GIL-scoped callback scheduled by C++.

    Prefetch does not start until ``start()`` so callers can register frame-ready
    callbacks first (MediaPool.acquire).
    """

    def __init__(self, decoder: BaseDecoder, settings: Settings, resolution_scale: float = 1.0):
        self.decoder = decoder
        self.settings = settings
        self.resolution_scale = Settings.clamp_resolution_scale(resolution_scale)

        self.native_cache = framecycler_engine.CacheManager(self.settings.decode_cache_limit_gb)
        self.lock = threading.Lock()
        self._frame_ready_callbacks: List[Callable[[int], None]] = []
        self._started = False

        meta = self.decoder.get_metadata()
        start = meta.get("start_frame", 0)
        end = meta.get("end_frame", meta["frame_count"] - 1)

        self.current_playhead = start
        self.play_direction = 1
        self.playback_range = (start, end)

        self._prefetch = framecycler_engine.PrefetchEngine(
            self.native_cache, max(1, int(self.settings.reader_threads))
        )
        self._prefetch.set_frame_ready_callback(self._notify_frame_ready)
        self._prefetch.set_python_decode_callback(self._python_decode_frame)
        self._sync_prefetch_options()
        self._sync_path_table()
        # Stay disabled until start() so no decode races ahead of callbacks.
        self._prefetch.set_enabled(False)

    def start(self) -> None:
        """Begin prefetch after frame-ready callbacks are registered."""
        with self.lock:
            if self._started:
                return
            self._started = True
            start, end = self.playback_range
            playhead = self.current_playhead
            direction = self.play_direction
            enabled = self.settings.decode_cache_limit_gb > 0.0
        self._prefetch.set_enabled(enabled)
        self._prefetch.set_playback_range(start, end)
        self._prefetch.set_playhead(playhead, direction)

    def add_frame_ready_callback(self, callback: Callable[[int], None]) -> None:
        self._frame_ready_callbacks.append(callback)

    @staticmethod
    def _to_cache_dtype(img: np.ndarray) -> np.ndarray:
        if img.dtype == np.float16:
            return img
        return np.ascontiguousarray(img.astype(np.float16))

    @staticmethod
    def _prepare_cache_image(img: np.ndarray) -> tuple[np.ndarray, int]:
        """Normalize dtype/shape for CacheManager. RGB is expanded to RGBA on decode threads."""
        img = CacheEngine._to_cache_dtype(img)
        if img.ndim == 2:
            img = img[:, :, np.newaxis]
        channels = int(img.shape[2])
        if channels == 3:
            alpha = np.ones((img.shape[0], img.shape[1], 1), dtype=np.float16)
            img = np.ascontiguousarray(np.concatenate([img, alpha], axis=2))
            channels = 4
        return img, channels

    def _uses_native_path_decode(self) -> bool:
        uses_native = getattr(self.decoder, "uses_native_path_decode", None)
        return bool(uses_native is not None and uses_native())

    def _sync_path_table(self) -> None:
        if not self._uses_native_path_decode():
            self._prefetch.set_path_table({}, [])
            return
        frame_map = getattr(self.decoder, "frame_map", None) or {}
        paths = {int(k): str(v) for k, v in frame_map.items()}
        sorted_frames = sorted(paths.keys())
        self._prefetch.set_path_table(paths, sorted_frames)

    def _sync_prefetch_options(self) -> None:
        meta = getattr(self.decoder, "metadata", None) or self.decoder.get_metadata() or {}
        active_layer = getattr(self.decoder, "active_layer", "") or ""
        fallback_mode = getattr(self.settings, "missing_frame_mode", "Nearest Frame")
        self._prefetch.set_options(
            self.resolution_scale,
            active_layer,
            fallback_mode,
            int(meta.get("width", 0) or 0),
            int(meta.get("height", 0) or 0),
            self._uses_native_path_decode(),
        )

    def _python_decode_frame(self, frame_index: int) -> bool:
        """Called from C++ PrefetchEngine workers under the GIL for non-native sources."""
        try:
            frame_data = self.decoder.read_frame(
                frame_index, resolution_scale=self.resolution_scale
            )
            img, channels = self._prepare_cache_image(frame_data["data"])
            self.native_cache.write_frame(frame_index, img.shape[1], img.shape[0], channels, img)
            return True
        except Exception as exc:
            print(f"CacheEngine: python decode failed for frame {frame_index}: {exc}")
            return False

    def _notify_frame_ready(self, frame_index: int) -> None:
        for callback in list(self._frame_ready_callbacks):
            try:
                callback(frame_index)
            except Exception as exc:
                print(f"CacheEngine: frame-ready callback failed for frame {frame_index}: {exc}")

    def set_playback_range(self, start: int, end: int):
        with self.lock:
            self.playback_range = (start, end)
        self._prefetch.set_playback_range(start, end)

    def set_playhead(self, frame_index: int, direction: int = 1):
        with self.lock:
            self.current_playhead = frame_index
            self.play_direction = direction
            started = self._started
        if started:
            self._prefetch.set_playhead(frame_index, direction)
        else:
            # First seek also starts prefetch (covers tests / paths that skip start()).
            self.start()

    def has_frame(self, frame_index: int) -> bool:
        return bool(self.native_cache.has_frame(frame_index))

    def get_frame(self, frame_index: int) -> Dict[str, Any] | None:
        if self.has_frame(frame_index):
            data_view = self.native_cache.get_frame_data(frame_index)
            if data_view is not None:
                cache_channels = data_view.shape[2] if data_view.ndim > 2 else 1
                meta = self.decoder.get_metadata()
                return {
                    "width": data_view.shape[1],
                    "height": data_view.shape[0],
                    "channels": cache_channels,
                    "frame_index": frame_index,
                    "timecode": Timecode.frame_to_timecode(frame_index, meta["fps"], 0),
                }

        if not self._started:
            self.start()
        self._prefetch.schedule(frame_index, 0)
        return None

    def get_cached_frames(self) -> Set[int]:
        return set(self.native_cache.get_cached_frames())

    def set_resolution_scale(self, scale: float) -> None:
        self.resolution_scale = Settings.clamp_resolution_scale(scale)
        self._sync_prefetch_options()

    def update_settings(self):
        with self.lock:
            self.native_cache.set_ram_limit(self.settings.decode_cache_limit_gb)
            self._prefetch.set_enabled(self.settings.decode_cache_limit_gb > 0.0)
            self._prefetch.set_max_workers(max(1, int(self.settings.reader_threads)))
            self._sync_prefetch_options()
            self._sync_path_table()

    def trigger_prefetch(self):
        with self.lock:
            playhead = self.current_playhead
            direction = self.play_direction
        if not self._started:
            self.start()
        else:
            self._prefetch.set_playhead(playhead, direction)

    def clear(self):
        self._prefetch.clear()

    def close(self):
        # PrefetchEngine.stop clears Python callbacks under the GIL, then joins
        # workers with the GIL released.
        self._prefetch.stop()
        self.native_cache.clear()
