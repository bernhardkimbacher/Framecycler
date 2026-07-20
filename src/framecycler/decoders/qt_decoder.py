import os
from typing import Any, Dict

import numpy as np

from .base import BaseDecoder
from ..core.timecode import Timecode

try:
    from .. import framecycler_engine
except ImportError:
    import framecycler_engine


class QuickTimeDecoder(BaseDecoder):
    """Thin Python façade over C++ NativeMovieDecoder (FFmpeg)."""

    def __init__(self, file_path: str):
        if not os.path.isfile(file_path):
            raise FileNotFoundError(f"File not found: {file_path}")

        self.file_path = os.path.abspath(file_path)
        self._native = framecycler_engine.NativeMovieDecoder()
        if not self._native.open(self.file_path):
            raise ValueError(f"Failed to open movie: {file_path}")

        probe = self._native.probe()
        self.width = int(probe["width"])
        self.height = int(probe["height"])
        self.fps = float(probe["fps"])
        self.frame_count = int(probe["frame_count"])
        self.channels = list(probe["channels"])
        self.timecode_start = str(probe["timecode_start"])
        self.start_frame = int(probe["start_frame"])
        self.end_frame = int(probe["end_frame"])

        self.metadata = {
            "width": self.width,
            "height": self.height,
            "pixel_aspect_ratio": float(probe.get("pixel_aspect_ratio", 1.0) or 1.0),
            "fps": self.fps,
            "frame_count": self.frame_count,
            "start_frame": self.start_frame,
            "end_frame": self.end_frame,
            "timecode_start": self.timecode_start,
            "channels": self.channels,
            "file_metadata": dict(probe.get("file_metadata") or {}),
            "has_audio": False,
        }
        self._audio_peaks: list[float] = []
        try:
            audio = framecycler_engine.NativeAudioDecoder()
            if audio.open(self.file_path) and audio.has_audio():
                self.metadata["has_audio"] = True
            audio.close()
        except Exception:
            pass

    def ensure_audio_peaks(self, peaks_per_second: int = 300) -> list[float]:
        if self._audio_peaks:
            return self._audio_peaks
        if not self.metadata.get("has_audio"):
            return []
        try:
            audio = framecycler_engine.NativeAudioDecoder()
            if not audio.open(self.file_path) or not audio.has_audio():
                audio.close()
                return []
            peaks = audio.build_peaks(peaks_per_second)
            audio.close()
            self._audio_peaks = [float(x) for x in peaks.tolist()] if hasattr(peaks, "tolist") else list(peaks)
            return self._audio_peaks
        except Exception:
            return []

    def get_native_movie_decoder(self):
        return self._native

    def uses_native_path_decode(self) -> bool:
        return False

    def uses_native_movie_decode(self) -> bool:
        return True

    def get_metadata(self) -> Dict[str, Any]:
        return self.metadata

    def read_frame(self, frame_index: int, resolution_scale: float = 1.0) -> Dict[str, Any]:
        if frame_index < self.start_frame or frame_index > self.end_frame:
            raise IndexError(
                f"Frame index {frame_index} out of bounds ({self.start_frame}-{self.end_frame})"
            )
        if self._native is None:
            raise RuntimeError("QuickTimeDecoder is closed")

        img = self._native.decode_frame(frame_index, resolution_scale)
        if img is None:
            raise ValueError(f"Failed to decode frame {frame_index}")

        img = np.ascontiguousarray(img.astype(np.float16, copy=False))

        return {
            "data": img,
            "channels": self.channels,
            "frame_index": frame_index,
            "timecode": Timecode.frame_to_timecode(frame_index, self.fps, 0),
        }

    def close(self):
        if self._native is not None:
            self._native.close()
            self._native = None
