import os
import json

from .system_memory import clamp_cache_limits, get_platform_cache_limits
from .playback_timing import (
    PLAYBACK_TIMING_EVERY_FRAME,
    normalize_playback_timing,
)


class Settings:
    def __init__(self, config_dir=None):
        self.config_dir = config_dir or os.path.expanduser("~/.framecycler")
        self.config_path = os.path.join(self.config_dir, "settings.json")

        limits = get_platform_cache_limits()
        default_decode = min(8.0, limits.decode_max_gb)
        default_display = min(2.0, limits.display_max_gb)
        if limits.coupled:
            default_decode, default_display = clamp_cache_limits(default_decode, default_display, limits)

        # Default settings values
        self.reader_threads = max(1, min(32, os.cpu_count() or 4))
        self.decode_cache_limit_gb = default_decode
        self.display_cache_limit_gb = default_display
        self.default_fps = 24.0
        self.ocio_config_path = ""
        self.loop_mode = "loop"  # loop, bounce, once
        # every_frame | realtime — default prefers contiguous display-cache fill
        self.playback_timing = PLAYBACK_TIMING_EVERY_FRAME
        self.timecode_mode = False  # True = Timecode mode, False = Frame mode
        self.recent_files = []
        self.resolution_scale = 1.0
        self.missing_frame_mode = "Nearest Frame"
        # Package id -> enabled override; absent means use manifest enabled_by_default
        self.package_enabled: dict[str, bool] = {}
        # Vertical main splitter: [viewer_area, timeline_pane]
        self.timeline_splitter_sizes: list[int] = [700, 96]
        # QMainWindow.saveState / saveGeometry as base64 (dock layout + window geom)
        self.main_window_state: str = ""
        self.main_window_geometry: str = ""

        self.load()

    @property
    def ram_cache_limit_gb(self) -> float:
        """Deprecated alias for decode_cache_limit_gb."""
        return self.decode_cache_limit_gb

    @ram_cache_limit_gb.setter
    def ram_cache_limit_gb(self, value: float) -> None:
        self.decode_cache_limit_gb = value

    @staticmethod
    def clamp_resolution_scale(scale: float) -> float:
        return max(0.01, min(1.0, float(scale)))

    def clamp_cache_limits_to_platform(self) -> None:
        limits = get_platform_cache_limits()
        decode, display = clamp_cache_limits(
            self.decode_cache_limit_gb,
            self.display_cache_limit_gb,
            limits,
        )
        self.decode_cache_limit_gb = decode
        self.display_cache_limit_gb = display

    def load(self):
        if not os.path.exists(self.config_path):
            self.clamp_cache_limits_to_platform()
            return
        try:
            with open(self.config_path, "r") as f:
                data = json.load(f)
                self.reader_threads = data.get("reader_threads", self.reader_threads)
                self.decode_cache_limit_gb = data.get(
                    "decode_cache_limit_gb",
                    data.get("ram_cache_limit_gb", self.decode_cache_limit_gb),
                )
                self.display_cache_limit_gb = data.get(
                    "display_cache_limit_gb",
                    self.display_cache_limit_gb,
                )
                self.default_fps = data.get("default_fps", self.default_fps)
                self.ocio_config_path = data.get("ocio_config_path", self.ocio_config_path)
                self.loop_mode = data.get("loop_mode", self.loop_mode)
                self.playback_timing = normalize_playback_timing(
                    data.get("playback_timing", self.playback_timing)
                )
                self.timecode_mode = data.get("timecode_mode", self.timecode_mode)
                self.recent_files = data.get("recent_files", self.recent_files)
                self.missing_frame_mode = data.get("missing_frame_mode", "Nearest Frame")
                raw_packages = data.get("package_enabled", self.package_enabled)
                if isinstance(raw_packages, dict):
                    self.package_enabled = {
                        str(key): bool(value) for key, value in raw_packages.items()
                    }
                else:
                    self.package_enabled = {}
                raw_sizes = data.get("timeline_splitter_sizes", self.timeline_splitter_sizes)
                if (
                    isinstance(raw_sizes, list)
                    and len(raw_sizes) == 2
                    and all(isinstance(v, (int, float)) for v in raw_sizes)
                ):
                    self.timeline_splitter_sizes = [max(80, int(raw_sizes[0])), max(64, int(raw_sizes[1]))]
                raw_state = data.get("main_window_state", self.main_window_state)
                self.main_window_state = str(raw_state) if raw_state else ""
                raw_geom = data.get("main_window_geometry", self.main_window_geometry)
                self.main_window_geometry = str(raw_geom) if raw_geom else ""
        except Exception as e:
            print(f"Error loading settings: {e}")
        self.clamp_cache_limits_to_platform()

    def save(self):
        try:
            os.makedirs(self.config_dir, exist_ok=True)
            data = {
                "reader_threads": self.reader_threads,
                "decode_cache_limit_gb": self.decode_cache_limit_gb,
                "display_cache_limit_gb": self.display_cache_limit_gb,
                "default_fps": self.default_fps,
                "ocio_config_path": self.ocio_config_path,
                "loop_mode": self.loop_mode,
                "playback_timing": normalize_playback_timing(self.playback_timing),
                "timecode_mode": self.timecode_mode,
                "recent_files": self.recent_files,
                "missing_frame_mode": self.missing_frame_mode,
                "package_enabled": self.package_enabled,
                "timeline_splitter_sizes": self.timeline_splitter_sizes,
                "main_window_state": self.main_window_state,
                "main_window_geometry": self.main_window_geometry,
            }
            with open(self.config_path, "w") as f:
                json.dump(data, f, indent=4)
        except Exception as e:
            print(f"Error saving settings: {e}")
