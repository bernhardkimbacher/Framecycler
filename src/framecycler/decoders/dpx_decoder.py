import os
import re
import numpy as np
from typing import Dict, Any, List, Tuple
from .base import BaseDecoder, pattern_frame_regex, placeholder_rgba, _frame_token_match
from . import image_io
from ..core.timecode import Timecode

class DPXDecoder(BaseDecoder):
    def __init__(self, file_path_pattern: str):
        """
        file_path_pattern: Can be a single DPX file or a sequence pattern like 'shot.####.dpx' or 'shot.%04d.dpx'.
        We resolve this pattern into a list of sorted (frame_number, file_path) tuples.
        """
        self.resolved_frames = self._resolve_pattern(file_path_pattern)
        if not self.resolved_frames:
            raise FileNotFoundError(f"No DPX files found matching: {file_path_pattern}")
            
        self.frame_map = {frame_num: path for frame_num, path in self.resolved_frames}
        self.existing_frame_numbers = sorted(list(self.frame_map.keys()))
        self.start_frame = self.existing_frame_numbers[0]
        self.end_frame = self.existing_frame_numbers[-1]
        self.frame_numbers = list(range(self.start_frame, self.end_frame + 1))
        self.file_paths = [self.frame_map[fn] for fn in self.existing_frame_numbers]
        
        self.metadata = self._read_sequence_metadata()

    def _resolve_pattern(self, pattern: str) -> List[Tuple[int, str]]:
        if not pattern:
            return []
            
        if os.path.isfile(pattern):
            seq = self._find_sequence_from_single_file(pattern)
            if seq:
                return seq
            # Fallback to single file
            base = os.path.basename(pattern)
            name_part, ext = os.path.splitext(base)
            match = _frame_token_match(name_part) or re.search(r"(\d+)(?:\D*)$", name_part)
            frame_num = int(match.group(1)) if match else 0
            return [(frame_num, os.path.abspath(pattern))]
            
        dir_name = os.path.dirname(pattern) or "."
        base_name = os.path.basename(pattern)
        
        if not os.path.isdir(dir_name):
            return []
            
        regex_compiled = re.compile(pattern_frame_regex(base_name))
        
        matched_files = []
        for file in os.listdir(dir_name):
            match = regex_compiled.match(file)
            if match:
                matched_files.append((int(match.group(1)), os.path.join(dir_name, file)))
                
        matched_files.sort(key=lambda x: x[0])
        return [(frame_num, os.path.abspath(path)) for frame_num, path in matched_files]

    def _read_sequence_metadata(self) -> Dict[str, Any]:
        first_file = self.file_paths[0]
        img_meta = image_io.read_metadata(first_file)
        channels = image_io.display_channels_for_metadata(img_meta)

        return {
            "width": img_meta.width,
            "height": img_meta.height,
            "pixel_aspect_ratio": img_meta.pixel_aspect_ratio,
            "fps": 24.0,
            "frame_count": len(self.frame_numbers),
            "start_frame": self.start_frame,
            "end_frame": self.end_frame,
            "timecode_start": Timecode.frame_to_timecode(0, 24.0, self.start_frame),
            "channels": channels,
            "transfer_characteristic": img_meta.transfer_characteristic,
            "colorimetric_specification": img_meta.colorimetric_specification,
        }

    def get_metadata(self) -> Dict[str, Any]:
        return self.metadata

    def read_frame(
        self,
        frame_index: int,
        resolution_scale: float = 1.0,
        missing_frame_mode: str = "Nearest Frame",
    ) -> Dict[str, Any]:
        if frame_index < self.start_frame or frame_index > self.end_frame:
            raise IndexError(f"Frame index {frame_index} out of bounds ({self.start_frame}-{self.end_frame})")

        file_path = self.frame_map.get(frame_index)
        img = None
        if not file_path:
            mode = missing_frame_mode or "Nearest Frame"
            if mode == "Nearest Frame" and self.existing_frame_numbers:
                closest_frame = min(self.existing_frame_numbers, key=lambda x: abs(x - frame_index))
                file_path = self.frame_map[closest_frame]
            else:
                w = int(self.metadata.get("width", 64) or 64)
                h = int(self.metadata.get("height", 64) or 64)
                if resolution_scale > 0 and resolution_scale < 1.0:
                    w = max(1, int(w * resolution_scale))
                    h = max(1, int(h * resolution_scale))
                img = placeholder_rgba(w, h, mode)

        if img is None:
            img = image_io.read_pixels(file_path, resolution_scale=resolution_scale)

        tc = Timecode.frame_to_timecode(frame_index, self.metadata["fps"], 0)

        return {
            "data": img,
            "channels": self.metadata["channels"],
            "frame_index": frame_index,
            "timecode": tc
        }

    def get_file_path(self, frame_index: int, fallback_nearest: bool = False) -> str | None:
        if fallback_nearest:
            if not self.existing_frame_numbers:
                return None
            closest_frame = min(self.existing_frame_numbers, key=lambda x: abs(x - frame_index))
            return self.frame_map.get(closest_frame)
        return self.frame_map.get(frame_index)

    def uses_native_path_decode(self) -> bool:
        return True

    def close(self):
        pass
