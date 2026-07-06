import os
import av
import numpy as np
import threading
from typing import Dict, Any
from .base import BaseDecoder
from . import image_io
from ..core.timecode import Timecode

class QuickTimeDecoder(BaseDecoder):
    def __init__(self, file_path: str):
        if not os.path.isfile(file_path):
            raise FileNotFoundError(f"File not found: {file_path}")
            
        self.file_path = os.path.abspath(file_path)
        self.container = av.open(self.file_path)
        
        # Get video stream
        video_streams = [s for s in self.container.streams if s.type == 'video']
        if not video_streams:
            raise ValueError(f"No video streams found in: {file_path}")
            
        self.stream = video_streams[0]
        self.stream.thread_type = "AUTO"  # Enable multi-threaded decoding in FFmpeg
        
        # Read stream attributes
        self.width = self.stream.width
        self.height = self.stream.height
        
        # Calculate frame rate
        fps = float(self.stream.average_rate or self.stream.base_rate or 24.0)
        if fps <= 0:
            fps = 24.0
        self.fps = fps
        
        # Frame count
        self.frame_count = self.stream.frames
        if self.frame_count <= 0:
            # Estimate from duration
            duration = float(self.container.duration or 0) / av.time_base
            self.frame_count = int(round(duration * self.fps))
            if self.frame_count <= 0:
                self.frame_count = 1
                
        # Channels list
        self.channels = ["R", "G", "B"]
        if self.stream.pix_fmt in ['rgba', 'bgra', 'yuva420p', 'yuva422p', 'yuva444p']:
            self.channels.append("A")
            
        # State tracking for sequential optimization
        self._current_frame_index = -1
        self._frame_generator = None
        self.lock = threading.Lock()
        
        self.timecode_start = self._extract_start_timecode()
        self.start_frame = Timecode.timecode_to_frame(self.timecode_start, self.fps)
        self.end_frame = self.start_frame + self.frame_count - 1
        
        # Extract stream and container metadata
        merged_meta = {}
        if hasattr(self.container, "metadata") and self.container.metadata:
            merged_meta.update(self.container.metadata)
        if hasattr(self.stream, "metadata") and self.stream.metadata:
            merged_meta.update(self.stream.metadata)

        pixel_aspect_ratio = 1.0
        sample_aspect = getattr(self.stream, "sample_aspect_ratio", None)
        if sample_aspect is not None and sample_aspect.denominator:
            pixel_aspect_ratio = float(sample_aspect.numerator) / float(sample_aspect.denominator)
            if pixel_aspect_ratio <= 0.0:
                pixel_aspect_ratio = 1.0

        self.metadata = {
            "width": self.width,
            "height": self.height,
            "pixel_aspect_ratio": pixel_aspect_ratio,
            "fps": self.fps,
            "frame_count": self.frame_count,
            "start_frame": self.start_frame,
            "end_frame": self.end_frame,
            "timecode_start": self.timecode_start,
            "channels": self.channels,
            "file_metadata": merged_meta
        }

        # For single-frame files (images), pre-decode the frame immediately and close the container
        self._single_frame_data = None
        if self.frame_count == 1:
            try:
                generator = self.container.decode(video=0)
                frame = next(generator)
                self._single_frame_data = self._process_frame(frame, self.start_frame)
                self.container.close()
            except Exception as e:
                print(f"QuickTimeDecoder: failed to pre-decode single frame: {e}")

    def _extract_start_timecode(self) -> str:
        # Check metadata for start timecode
        timecode = self.stream.metadata.get("timecode") or self.container.metadata.get("timecode")
        if timecode:
            return timecode
        return "01:00:00:00"

    def get_metadata(self) -> Dict[str, Any]:
        return self.metadata

    def _reset_generator(self):
        self.container.seek(0)
        self._frame_generator = self.container.decode(video=0)
        self._current_frame_index = -1

    def read_frame(self, frame_index: int, resolution_scale: float = 1.0) -> Dict[str, Any]:
        if frame_index < self.start_frame or frame_index > self.end_frame:
            raise IndexError(f"Frame index {frame_index} out of bounds ({self.start_frame}-{self.end_frame})")
            
        if self._single_frame_data is not None:
            data = self._single_frame_data["data"]
            if resolution_scale < 1.0:
                if data.dtype != np.float16:
                    data = data.astype(np.float16)
                data = image_io.downsample_pixels(data, resolution_scale)
            return {
                "data": data,
                "channels": self.channels,
                "frame_index": frame_index,
                "timecode": self._single_frame_data["timecode"]
            }
            
        with self.lock:
            # Map absolute frame_index to internal 0-based index
            internal_index = frame_index - self.start_frame
            
            # Optimization: if requesting the next frame sequentially, just read it from the active generator
            if self._frame_generator is not None and internal_index == self._current_frame_index + 1:
                try:
                    frame = next(self._frame_generator)
                    self._current_frame_index = internal_index
                    return self._process_frame(frame, frame_index, resolution_scale)
                except (StopIteration, av.FFmpegError):
                    pass
                    
            # Non-sequential seek or generator failed: perform a container seek to keyframe
            # Convert frame index to stream timestamp (pts)
            time_base = self.stream.time_base
            target_sec = internal_index / self.fps
            target_pts = int(round(target_sec / time_base))
            
            # Seek stream
            self.container.seek(target_pts, stream=self.stream)
            self._frame_generator = self.container.decode(video=0)
            
            # Decode and discard frames until we reach the target frame
            last_frame = None
            while True:
                try:
                    frame = next(self._frame_generator)
                    # Compute approximate frame index from frame pts
                    pts_sec = float(frame.pts * time_base)
                    approx_internal = int(round(pts_sec * self.fps))
                    
                    if approx_internal == internal_index:
                        self._current_frame_index = internal_index
                        return self._process_frame(frame, frame_index, resolution_scale)
                    elif approx_internal > internal_index:
                        # We seeked past or skipped it, fall back to last decoded frame if close, or just return this frame
                        self._current_frame_index = approx_internal
                        return self._process_frame(frame, self.start_frame + approx_internal, resolution_scale)
                    
                    last_frame = frame
                except (StopIteration, av.FFmpegError):
                    # End of stream or decoder error
                    if last_frame is not None:
                        return self._process_frame(last_frame, frame_index, resolution_scale)
                    raise ValueError(f"Failed to seek or read frame {frame_index}")

    def _process_frame(self, frame, frame_index: int, resolution_scale: float = 1.0) -> Dict[str, Any]:
        # Convert PyAV frame to RGB/RGBA NumPy array
        pix_format = 'rgba' if "A" in self.channels else 'rgb24'
        img_frame = frame.to_ndarray(format=pix_format)
        
        img = (img_frame.astype(np.float32) / 255.0).astype(np.float16)
        if resolution_scale < 1.0:
            img = image_io.downsample_pixels(img, resolution_scale)
        
        tc = Timecode.frame_to_timecode(frame_index, self.fps, 0)
        
        return {
            "data": img,
            "channels": self.channels,
            "frame_index": frame_index,
            "timecode": tc
        }

    def close(self):
        if self._single_frame_data is not None:
            return
        with self.lock:
            try:
                self._frame_generator = None
                self.container.close()
            except Exception:
                pass
