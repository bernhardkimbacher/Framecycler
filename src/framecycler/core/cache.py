import heapq
import threading
import queue
import numpy as np
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Dict, Any, List, Set, Tuple

from PySide6.QtCore import QByteArray

from ..decoders.base import BaseDecoder
from .settings import Settings
from .timecode import Timecode

# Try importing the compiled C++ engine extension module
try:
    from .. import framecycler_engine
except ImportError:
    import framecycler_engine


class CacheEngine:
    def __init__(self, decoder: BaseDecoder, settings: Settings, resolution_scale: float = 1.0):
        self.decoder = decoder
        self.settings = settings
        self.resolution_scale = Settings.clamp_resolution_scale(resolution_scale)

        # Instantiate compiled C++ CacheManager (stores half-float pixels in pre-allocated RAM)
        self.native_cache = framecycler_engine.CacheManager(self.settings.decode_cache_limit_gb)
        self.lock = threading.Lock()

        # Priority decode queue: (priority, sequence, frame_index) — lower priority value = sooner
        self._decode_heap: List[Tuple[int, int, int]] = []
        self._heap_seq = 0
        self._frame_ready_callbacks: List[Callable[[int], None]] = []

        # Asynchronous pre-fetch queue & threads
        self.request_queue = queue.Queue()
        self.active_requests: Set[int] = set()
        self.executor = ThreadPoolExecutor(max_workers=self.settings.reader_threads)

        # Playback states
        meta = self.decoder.get_metadata()
        meta_fps = meta.get("fps", 24.0)
        start = meta.get("start_frame", 0)
        end = meta.get("end_frame", meta["frame_count"] - 1)

        self.current_playhead = start
        self.play_direction = 1
        self.playback_range = (start, end)

        self.running = True
        self.manager_thread = threading.Thread(target=self._manager_loop, daemon=True)
        self.manager_thread.start()

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

    def set_playback_range(self, start: int, end: int):
        with self.lock:
            self.playback_range = (start, end)
            self.native_cache.set_playhead(self.current_playhead, self.play_direction, start, end)
        self.trigger_prefetch()

    def set_playhead(self, frame_index: int, direction: int = 1):
        with self.lock:
            self.current_playhead = frame_index
            self.play_direction = direction
            self.native_cache.set_playhead(frame_index, direction, self.playback_range[0], self.playback_range[1])
        self.trigger_prefetch()

    def get_frame(self, frame_index: int) -> Dict[str, Any] | None:
        if self.native_cache.has_frame(frame_index):
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

        self._schedule_frame(frame_index, priority=0)
        return None

    def get_cached_frames(self) -> Set[int]:
        return set(self.native_cache.get_cached_frames())

    def set_resolution_scale(self, scale: float) -> None:
        self.resolution_scale = Settings.clamp_resolution_scale(scale)

    def update_settings(self):
        with self.lock:
            self.native_cache.set_ram_limit(self.settings.decode_cache_limit_gb)

            new_threads = self.settings.reader_threads
            if self.executor._max_workers != new_threads:
                self.executor.shutdown(wait=False)
                self.executor = ThreadPoolExecutor(max_workers=new_threads)

    def trigger_prefetch(self):
        self.request_queue.put(None)

    def _schedule_frame(self, frame_index: int, priority: int) -> None:
        with self.lock:
            if self.native_cache.has_frame(frame_index):
                return
            if frame_index in self.active_requests:
                return
            self.active_requests.add(frame_index)
            heapq.heappush(self._decode_heap, (priority, self._heap_seq, frame_index))
            self._heap_seq += 1
        self.trigger_prefetch()

    def _notify_frame_ready(self, frame_index: int) -> None:
        for callback in list(self._frame_ready_callbacks):
            try:
                callback(frame_index)
            except Exception as exc:
                print(f"CacheEngine: frame-ready callback failed for frame {frame_index}: {exc}")

    def _manager_loop(self):
        while self.running:
            try:
                self.request_queue.get(timeout=0.1)
            except queue.Empty:
                pass
            if not self.running:
                break
            self._fill_cache_requests()
            self._process_decode_queue()

    def _fill_cache_requests(self):
        if self.settings.decode_cache_limit_gb <= 0.0:
            return

        with self.lock:
            playhead = self.current_playhead
            direction = self.play_direction
            start_range, end_range = self.playback_range
            frame_count = self.decoder.get_metadata()["frame_count"]

        if frame_count <= 0:
            return

        curr = playhead
        for distance in range(1, 101):
            curr += direction
            if curr > end_range:
                curr = start_range
            elif curr < start_range:
                curr = end_range

            self._schedule_frame(curr, priority=distance)

    def _process_decode_queue(self):
        to_submit: List[int] = []
        with self.lock:
            max_batch = self.settings.reader_threads
            while self._decode_heap and len(to_submit) < max_batch:
                _priority, _seq, frame_idx = heapq.heappop(self._decode_heap)
                if self.native_cache.has_frame(frame_idx):
                    self.active_requests.discard(frame_idx)
                    continue
                if frame_idx not in self.active_requests:
                    self.active_requests.add(frame_idx)
                to_submit.append(frame_idx)

        for frame_idx in to_submit:
            self.executor.submit(self._read_and_cache_worker, frame_idx)

    def _read_and_cache_worker(self, frame_index: int):
        if not self.running:
            return
        try:
            if self.native_cache.has_frame(frame_index):
                with self.lock:
                    self.active_requests.discard(frame_index)
                return

            get_path = getattr(self.decoder, "get_file_path", None)
            path = get_path(frame_index) if get_path is not None else None
            if path is not None:
                self.native_cache.decode_and_cache_frame(frame_index, path, self.resolution_scale)
            else:
                frame_data = self.decoder.read_frame(
                    frame_index, resolution_scale=self.resolution_scale
                )
                img, channels = self._prepare_cache_image(frame_data["data"])
                self.native_cache.write_frame(frame_index, img.shape[1], img.shape[0], channels, img)

            with self.lock:
                self.active_requests.discard(frame_index)
            self._notify_frame_ready(frame_index)
        except Exception as exc:
            print(f"CacheEngine: decode failed for frame {frame_index}: {exc}")
            with self.lock:
                self.active_requests.discard(frame_index)

    def clear(self):
        self.native_cache.clear()
        with self.lock:
            self.active_requests.clear()
            self._decode_heap.clear()

    def close(self):
        self.running = False
        self.executor.shutdown(wait=False)
        self.clear()
