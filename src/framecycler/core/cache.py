import threading
import queue
import numpy as np
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, Any, Set
from ..decoders.base import BaseDecoder
from .settings import Settings
from .timecode import Timecode

# Try importing the compiled C++ engine extension module
try:
    from .. import framecycler_engine
except ImportError:
    import framecycler_engine

class CacheEngine:
    def __init__(self, decoder: BaseDecoder, settings: Settings):
        self.decoder = decoder
        self.settings = settings
        
        # Instantiate compiled C++ CacheManager (stores uncompressed floats in pre-allocated RAM)
        self.native_cache = framecycler_engine.CacheManager(self.settings.ram_cache_limit_gb)
        self.lock = threading.Lock()
        
        # Asynchronous pre-fetch queue & threads
        self.request_queue = queue.Queue()
        self.active_requests: Set[int] = set()
        self.executor = ThreadPoolExecutor(max_workers=self.settings.reader_threads)
        
        # Playback states
        meta = self.decoder.get_metadata()
        start = meta.get("start_frame", 0)
        end = meta.get("end_frame", meta["frame_count"] - 1)
        
        self.current_playhead = start
        self.play_direction = 1
        self.playback_range = (start, end)
        
        self.running = True
        self.manager_thread = threading.Thread(target=self._manager_loop, daemon=True)
        self.manager_thread.start()

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

    def get_frame(self, frame_index: int) -> Dict[str, Any]:
        """
        Fetches frame. Returns zero-copy NumPy view of C++ buffer immediately on cache-hit.
        """
        # 1. Check C++ cache manager
        if self.native_cache.has_frame(frame_index):
            data_view = self.native_cache.get_frame_data(frame_index)
            if data_view is not None:
                meta = self.decoder.get_metadata()
                return {
                    "data": data_view,
                    "channels": meta["channels"],
                    "frame_index": frame_index,
                    "timecode": Timecode.frame_to_timecode(frame_index, meta["fps"], 0),
                }
                
        # 2. Cache miss: Read frame synchronously, store in C++ cache and return
        try:
            frame_data = self.decoder.read_frame(frame_index)
            img = frame_data["data"]
            h, w = img.shape[:2]
            channels = img.shape[2] if len(img.shape) > 2 else 1
            
            # Write to C++ native allocator
            self.native_cache.write_frame(frame_index, w, h, channels, img)
            
            # Fetch the zero-copy array back to ensure we share memory
            shared_view = self.native_cache.get_frame_data(frame_index)
            
            return {
                "data": shared_view if shared_view is not None else img,
                "channels": frame_data["channels"],
                "frame_index": frame_index,
                "timecode": frame_data["timecode"]
            }
        except Exception as e:
            print(f"CacheEngine: Synchronous read failure on frame {frame_index}: {e}")
            return None

    def get_cached_frames(self) -> Set[int]:
        return set(self.native_cache.get_cached_frames())

    def update_settings(self):
        with self.lock:
            # Update RAM Cache ceiling in C++
            self.native_cache.set_ram_limit(self.settings.ram_cache_limit_gb)
            
            # Rescale reader threads
            new_threads = self.settings.reader_threads
            if self.executor._max_workers != new_threads:
                self.executor.shutdown(wait=False)
                self.executor = ThreadPoolExecutor(max_workers=new_threads)

    def trigger_prefetch(self):
        self.request_queue.put(None)

    def _manager_loop(self):
        while self.running:
            try:
                self.request_queue.get(timeout=0.1)
            except queue.Empty:
                pass
            if not self.running:
                break
            self._fill_cache_requests()

    def _fill_cache_requests(self):
        with self.lock:
            playhead = self.current_playhead
            direction = self.play_direction
            start_range, end_range = self.playback_range
            frame_count = self.decoder.get_metadata()["frame_count"]
            
        if frame_count <= 0:
            return
            
        prefetch_sequence = []
        max_prefetch_ahead = 100
        curr = playhead
        
        for _ in range(max_prefetch_ahead):
            curr += direction
            if curr > end_range:
                curr = start_range
            elif curr < start_range:
                curr = end_range
                
            if not self.native_cache.has_frame(curr) and curr not in self.active_requests:
                prefetch_sequence.append(curr)
                
        for frame_idx in prefetch_sequence:
            with self.lock:
                self.active_requests.add(frame_idx)
            self.executor.submit(self._read_and_cache_worker, frame_idx)

    def _read_and_cache_worker(self, frame_index: int):
        if not self.running:
            return
        try:
            if self.native_cache.has_frame(frame_index):
                with self.lock:
                    self.active_requests.discard(frame_index)
                return
                
            frame_data = self.decoder.read_frame(frame_index)
            img = frame_data["data"]
            h, w = img.shape[:2]
            channels = img.shape[2] if len(img.shape) > 2 else 1
            
            # Save directly into pre-allocated C++ memory
            self.native_cache.write_frame(frame_index, w, h, channels, img)
            
            with self.lock:
                self.active_requests.discard(frame_index)
        except Exception:
            with self.lock:
                self.active_requests.discard(frame_index)

    def clear(self):
        self.native_cache.clear()
        with self.lock:
            self.active_requests.clear()

    def close(self):
        self.running = False
        self.executor.shutdown(wait=False)
        self.clear()
