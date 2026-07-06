import os
import re
import cv2
import numpy as np
from typing import Dict, Any, List, Tuple
from .base import BaseDecoder
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
        self.frame_numbers = sorted(list(self.frame_map.keys()))
        self.file_paths = [self.frame_map[fn] for fn in self.frame_numbers]
        
        self.start_frame = self.frame_numbers[0]
        self.end_frame = self.frame_numbers[-1]
        
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
            match = re.search(r'(\d+)(?:\D*)$', name_part)
            frame_num = int(match.group(1)) if match else 0
            return [(frame_num, os.path.abspath(pattern))]
            
        dir_name = os.path.dirname(pattern) or "."
        base_name = os.path.basename(pattern)
        
        if not os.path.isdir(dir_name):
            return []
            
        regex_pattern = base_name.replace("####", r"(\d+)")
        regex_pattern = re.sub(r"%\d*d", r"(\d+)", regex_pattern)
        regex_pattern = "^" + regex_pattern.replace(".", r"\.") + "$"
        regex_compiled = re.compile(regex_pattern)
        
        matched_files = []
        for file in os.listdir(dir_name):
            match = regex_compiled.match(file)
            if match:
                matched_files.append((int(match.group(1)), os.path.join(dir_name, file)))
                
        matched_files.sort(key=lambda x: x[0])
        return [(frame_num, os.path.abspath(path)) for frame_num, path in matched_files]

    def _read_sequence_metadata(self) -> Dict[str, Any]:
        first_file = self.file_paths[0]
        img = cv2.imread(first_file, cv2.IMREAD_UNCHANGED)
        if img is None:
            raise ValueError(f"Failed to load DPX file: {first_file}")
            
        h, w = img.shape[:2]
        
        # DPX is typically RGB or RGBA
        channels = ["R", "G", "B"]
        if len(img.shape) > 2 and img.shape[2] == 4:
            channels = ["R", "G", "B", "A"]
            
        # Read DPX header for transfer characteristic & colorimetric specification
        transfer_char = 0
        colorimetric = 0
        try:
            with open(first_file, "rb") as f:
                f.seek(801)
                b = f.read(2)
                if len(b) == 2:
                    transfer_char = b[0]
                    colorimetric = b[1]
        except Exception as e:
            print(f"DPXDecoder: failed to read transfer/colorimetric headers: {e}")

        return {
            "width": w,
            "height": h,
            "fps": 24.0,
            "frame_count": len(self.file_paths),
            "start_frame": self.start_frame,
            "end_frame": self.end_frame,
            "timecode_start": Timecode.frame_to_timecode(0, 24.0, self.start_frame),
            "channels": channels,
            "transfer_characteristic": transfer_char,
            "colorimetric_specification": colorimetric
        }

    def get_metadata(self) -> Dict[str, Any]:
        return self.metadata

    def read_frame(self, frame_index: int) -> Dict[str, Any]:
        if frame_index < self.start_frame or frame_index > self.end_frame:
            raise IndexError(f"Frame index {frame_index} out of bounds ({self.start_frame}-{self.end_frame})")
            
        # Get path for the requested frame, or closest available frame if missing
        file_path = self.frame_map.get(frame_index)
        if not file_path:
            closest_frame = min(self.frame_numbers, key=lambda x: abs(x - frame_index))
            file_path = self.frame_map[closest_frame]
            
        img = cv2.imread(file_path, cv2.IMREAD_UNCHANGED)
        if img is None:
            raise ValueError(f"Failed to decode DPX frame: {file_path}")
            
        # OpenCV loads DPX as BGR or BGRA. Convert to RGB/RGBA.
        if len(img.shape) > 2:
            if img.shape[2] == 3:
                img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
            elif img.shape[2] == 4:
                img = cv2.cvtColor(img, cv2.COLOR_BGRA2RGBA)
                
        # Normalize image to float32 [0.0, 1.0] depending on input type
        if img.dtype == np.uint8:
            img = img.astype(np.float32) / 255.0
        elif img.dtype == np.uint16:
            img = img.astype(np.float32) / 65535.0
        else:
            img = img.astype(np.float32)
            
        tc = Timecode.frame_to_timecode(frame_index, self.metadata["fps"], 0)
        
        return {
            "data": img,
            "channels": self.metadata["channels"],
            "frame_index": frame_index,
            "timecode": tc
        }

    def close(self):
        pass
