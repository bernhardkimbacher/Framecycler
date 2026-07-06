from abc import ABC, abstractmethod
from typing import Dict, Any, List, Tuple

class BaseDecoder(ABC):
    def _find_sequence_from_single_file(self, file_path: str) -> List[Tuple[int, str]]:
        import os
        import re
        file_path = os.path.abspath(file_path)
        if not os.path.isfile(file_path):
            return []
            
        dir_name = os.path.dirname(file_path)
        base_name = os.path.basename(file_path)
        name_part, ext = os.path.splitext(base_name)
        
        # Match the last sequence of digits in name_part (e.g. "0993" in "shot.0993")
        match = re.search(r'(\d+)(?:\D*)$', name_part)
        if not match:
            return []
            
        digit_string = match.group(1)
        start_pos = match.start(1)
        end_pos = match.end(1)
        
        prefix = name_part[:start_pos]
        suffix = name_part[end_pos:]
        
        # Build regex for matches with the same prefix, suffix, extension, and variable digits
        pattern = "^" + re.escape(prefix) + r"(\d+)" + re.escape(suffix) + re.escape(ext) + "$"
        regex = re.compile(pattern, re.IGNORECASE)
        
        matched_files = []
        try:
            for file in os.listdir(dir_name):
                m = regex.match(file)
                if m:
                    matched_files.append((int(m.group(1)), os.path.join(dir_name, file)))
        except Exception:
            return []
                
        if len(matched_files) <= 1:
            return []
            
        matched_files.sort(key=lambda x: x[0])
        return [(frame_num, os.path.abspath(path)) for frame_num, path in matched_files]
    @abstractmethod
    def get_metadata(self) -> Dict[str, Any]:
        """
        Returns metadata about the loaded media:
        {
            "width": int,
            "height": int,
            "fps": float,
            "frame_count": int,
            "timecode_start": str,
            "channels": List[str] # List of available layer channels (e.g. ['R', 'G', 'B', 'A'])
        }
        """
        pass

    @abstractmethod
    def read_frame(self, frame_index: int, resolution_scale: float = 1.0) -> Dict[str, Any]:
        """
        Decodes and returns the frame at the specified index.
        resolution_scale: 1.0 = full resolution; values below 1.0 downsample for playback.
        Returns:
        {
            "data": np.ndarray (float16, shape=(H, W, C)),
            "channels": List[str],
            "frame_index": int,
            "timecode": str
        }
        """
        pass

    @abstractmethod
    def close(self):
        """
        Clean up open files or media streams.
        """
        pass
