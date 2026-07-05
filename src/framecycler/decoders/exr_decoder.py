import os
import re
import cv2
import numpy as np
from typing import Dict, Any, List, Tuple
from .base import BaseDecoder
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
        # Read standard image dimensions using cv2
        # Use IMREAD_UNCHANGED to read float32 natively without clipping
        img = cv2.imread(first_file, cv2.IMREAD_UNCHANGED)
        if img is None:
            raise ValueError(f"Failed to load EXR file: {first_file}")
            
        h, w = img.shape[:2]
        
        # Parse channels from header to present custom layer options in menu dropdown
        channels = self._parse_exr_channels(first_file)
        if not channels:
            # Fallback based on image shape
            channels = ["B", "G", "R"]
            if len(img.shape) > 2 and img.shape[2] == 4:
                channels = ["B", "G", "R", "A"]
                
        # Map BGR back to RGB convention for standard menu display
        standard_channels = []
        if "R" in channels and "G" in channels and "B" in channels:
            standard_channels.extend(["R", "G", "B"])
            if "A" in channels:
                standard_channels.append("A")
        else:
            standard_channels = sorted(channels)
            
        # Add other custom channels/layers to menu
        for c in channels:
            if c not in ["R", "G", "B", "A", "Y", "BY", "RY"] and c not in standard_channels:
                standard_channels.append(c)
                
        return {
            "width": w,
            "height": h,
            "fps": 24.0,  # Default sequence FPS, can be adjusted in settings
            "frame_count": len(self.file_paths),
            "start_frame": self.start_frame,
            "end_frame": self.end_frame,
            "timecode_start": Timecode.frame_to_timecode(0, 24.0, self.start_frame),
            "channels": standard_channels
        }

    def _parse_exr_channels(self, file_path: str) -> List[str]:
        """
        Fast binary header parser to extract channel names from EXR without compiled OpenEXR bindings.
        """
        channels = []
        try:
            with open(file_path, "rb") as f:
                # Read first 100KB which safely contains the header
                header_data = f.read(102400)
                
                # Check magic number: 0x76, 0x2f, 0x31, 0x01
                if len(header_data) < 4 or header_data[:4] != b'\x76\x2f\x31\x01':
                    return []
                    
                # Look for "channels" attribute
                channel_idx = header_data.find(b"channels\x00")
                if channel_idx != -1:
                    # Found channels block
                    # Format: name(null-terminated), type(null-terminated), size(4 bytes), value...
                    # Read types: name (string), attribute type (string "chlist"), size (int32)
                    start_ptr = channel_idx + len("channels\x00")
                    attr_type_end = header_data.find(b"\x00", start_ptr)
                    attr_type = header_data[start_ptr:attr_type_end]
                    
                    if attr_type == b"chlist":
                        size_ptr = attr_type_end + 1
                        size = int.from_bytes(header_data[size_ptr:size_ptr+4], byteorder='little')
                        
                        # Loop through the chlist values
                        val_ptr = size_ptr + 4
                        end_ptr = val_ptr + size
                        
                        while val_ptr < end_ptr:
                            # Read channel name (null-terminated)
                            name_end = header_data.find(b"\x00", val_ptr, end_ptr)
                            if name_end == -1 or name_end == val_ptr:
                                break
                            ch_name = header_data[val_ptr:name_end].decode('utf-8', errors='ignore')
                            channels.append(ch_name)
                            
                            # Channel info size: pixel type (4 bytes), pLinear (1 byte), reserved (3 bytes), xSampling (4 bytes), ySampling (4 bytes) = 16 bytes
                            val_ptr = name_end + 1 + 16
        except Exception as e:
            print(f"Error parsing EXR channels: {e}")
        return channels

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
            
        # Read image
        img = cv2.imread(file_path, cv2.IMREAD_UNCHANGED)
        if img is None:
            raise ValueError(f"Failed to decode EXR frame: {file_path}")
            
        # OpenCV reads BGR/BGRA float32 by default
        # Convert BGR(A) to RGB(A)
        if len(img.shape) > 2:
            if img.shape[2] == 3:
                img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
            elif img.shape[2] == 4:
                img = cv2.cvtColor(img, cv2.COLOR_BGRA2RGBA)
                
        # Ensure float32 representation
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
