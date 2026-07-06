import os
import json

class Settings:
    def __init__(self, config_dir=None):
        self.config_dir = config_dir or os.path.expanduser("~/.framecycler")
        self.config_path = os.path.join(self.config_dir, "settings.json")
        
        # Default settings values
        self.reader_threads = max(1, min(32, os.cpu_count() or 4))
        self.ram_cache_limit_gb = 8.0
        self.default_fps = 24.0
        self.ocio_config_path = ""
        self.loop_mode = "loop"  # loop, bounce, once
        self.timecode_mode = False  # True = Timecode mode, False = Frame mode
        self.recent_files = []
        self.resolution_scale = 1.0
        
        self.load()

    @staticmethod
    def clamp_resolution_scale(scale: float) -> float:
        return max(0.01, min(1.0, float(scale)))

    def load(self):
        if not os.path.exists(self.config_path):
            return
        try:
            with open(self.config_path, "r") as f:
                data = json.load(f)
                self.reader_threads = data.get("reader_threads", self.reader_threads)
                self.ram_cache_limit_gb = data.get("ram_cache_limit_gb", self.ram_cache_limit_gb)
                self.default_fps = data.get("default_fps", self.default_fps)
                self.ocio_config_path = data.get("ocio_config_path", self.ocio_config_path)
                self.loop_mode = data.get("loop_mode", self.loop_mode)
                self.timecode_mode = data.get("timecode_mode", self.timecode_mode)
                self.recent_files = data.get("recent_files", self.recent_files)
                self.resolution_scale = self.clamp_resolution_scale(
                    data.get("resolution_scale", self.resolution_scale)
                )
        except Exception as e:
            print(f"Error loading settings: {e}")

    def save(self):
        try:
            os.makedirs(self.config_dir, exist_ok=True)
            data = {
                "reader_threads": self.reader_threads,
                "ram_cache_limit_gb": self.ram_cache_limit_gb,
                "default_fps": self.default_fps,
                "ocio_config_path": self.ocio_config_path,
                "loop_mode": self.loop_mode,
                "timecode_mode": self.timecode_mode,
                "recent_files": self.recent_files,
                "resolution_scale": self.resolution_scale,
            }
            with open(self.config_path, "w") as f:
                json.dump(data, f, indent=4)
        except Exception as e:
            print(f"Error saving settings: {e}")
