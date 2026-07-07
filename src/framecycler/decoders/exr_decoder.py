import os
import re
import numpy as np
from typing import Dict, Any, List, Tuple, Optional
from .base import BaseDecoder
from . import image_io
from ..core.timecode import Timecode

class EXRDecoder(BaseDecoder):
    def __init__(self, file_path_pattern: str):
        """
        file_path_pattern: Can be a single EXR file or a sequence pattern like 'shot.####.exr' or 'shot.%04d.exr'.
        We resolve this pattern into a list of sorted (frame_number, file_path) tuples.
        """
        self.resolved_frames = self._resolve_pattern(file_path_pattern)
        if not self.resolved_frames:
            raise FileNotFoundError(f"No EXR files found matching: {file_path_pattern}")
            
        self.frame_map = {frame_num: path for frame_num, path in self.resolved_frames}
        self.frame_numbers = sorted(list(self.frame_map.keys()))
        self.file_paths = [self.frame_map[fn] for fn in self.frame_numbers]
        
        self.start_frame = self.frame_numbers[0]
        self.end_frame = self.frame_numbers[-1]
        self.active_layer: Optional[str] = "beauty"
        
        self.metadata = self._read_sequence_metadata()

    def _resolve_pattern(self, pattern: str) -> List[Tuple[int, str]]:
        if not pattern:
            return []
        
        # Check if it's a single file first
        if os.path.isfile(pattern):
            seq = self._find_sequence_from_single_file(pattern)
            if seq:
                return seq
            # Fallback to single file
            base = os.path.basename(pattern)
            name_part, ext = os.path.splitext(base)
            match = re.search(r'(\d+)(?:\D*)$', name_part)
            frame_num = int(match.group(1)) if match else 0
            return [(frame_num, os.path.abspath(pattern))]
            
        # Parse patterns like shot.####.exr or shot.%04d.exr
        dir_name = os.path.dirname(pattern) or "."
        base_name = os.path.basename(pattern)
        
        if not os.path.isdir(dir_name):
            return []
            
        # Convert #### to a regex match group for digits
        regex_pattern = base_name.replace("####", r"(\d+)")
        # Convert %04d to regex
        regex_pattern = re.sub(r"%\d*d", r"(\d+)", regex_pattern)
        
        # Escape other characters for regex
        regex_pattern = "^" + regex_pattern.replace(".", r"\.") + "$"
        regex_compiled = re.compile(regex_pattern)
        
        matched_files = []
        for file in os.listdir(dir_name):
            match = regex_compiled.match(file)
            if match:
                matched_files.append((int(match.group(1)), os.path.join(dir_name, file)))
                
        # Sort by frame number
        matched_files.sort(key=lambda x: x[0])
        return [(frame_num, os.path.abspath(path)) for frame_num, path in matched_files]

    def _read_sequence_metadata(self) -> Dict[str, Any]:
        first_file = self.file_paths[0]
        img_meta = image_io.read_metadata(first_file)
        layers = image_io.list_layers(first_file)
        channels = image_io.display_channels_for_metadata(img_meta)

        for layer in layers:
            if layer not in channels:
                channels.append(layer)

        if layers and self.active_layer not in layers:
            self.active_layer = layers[0]
                
        return {
            "width": img_meta.width,
            "height": img_meta.height,
            "pixel_aspect_ratio": img_meta.pixel_aspect_ratio,
            "fps": 24.0,  # Default sequence FPS, can be adjusted in settings
            "frame_count": len(self.file_paths),
            "start_frame": self.start_frame,
            "end_frame": self.end_frame,
            "timecode_start": Timecode.frame_to_timecode(0, 24.0, self.start_frame),
            "channels": channels,
            "layers": layers,
        }

    def get_metadata(self) -> Dict[str, Any]:
        return self.metadata

    def read_frame(self, frame_index: int, layer: Optional[str] = None, resolution_scale: float = 1.0) -> Dict[str, Any]:
        if frame_index < self.start_frame or frame_index > self.end_frame:
            raise IndexError(f"Frame index {frame_index} out of bounds ({self.start_frame}-{self.end_frame})")
            
        # Get path for the requested frame, or closest available frame if missing
        file_path = self.frame_map.get(frame_index)
        if not file_path:
            closest_frame = min(self.frame_numbers, key=lambda x: abs(x - frame_index))
            print(
                f"ExrDecoder: frame {frame_index} not in frame_map, "
                f"serving nearest available frame {closest_frame}"
            )
            file_path = self.frame_map[closest_frame]

        read_layer = layer if layer is not None else self.active_layer
        img = image_io.read_pixels(file_path, layer=read_layer, resolution_scale=resolution_scale)
        
        tc = Timecode.frame_to_timecode(frame_index, self.metadata["fps"], 0)
        
        return {
            "data": img,
            "channels": self.metadata["channels"],
            "frame_index": frame_index,
            "timecode": tc
        }

    def close(self):
        pass
