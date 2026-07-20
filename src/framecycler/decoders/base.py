from abc import ABC, abstractmethod
from typing import Dict, Any, List, Tuple
import os
import re

import numpy as np


def _is_version_token(digit_string: str, prefix: str) -> bool:
    """True when ``digit_string`` looks like a VFX version (v001), not a frame."""
    if len(digit_string) > 3:
        return False
    if not prefix:
        return False
    # Require a clear version marker immediately before the digits: _v / .v / -v
    return bool(re.search(r"[._\-]v$", prefix, re.IGNORECASE))


def _frame_token_match(name_part: str):
    """Pick the last digit run that is a frame field, not a version token."""
    matches = list(re.finditer(r"(\d+)", name_part))
    if not matches:
        return None
    for match in reversed(matches):
        digit_string = match.group(1)
        prefix = name_part[: match.start(1)]
        if _is_version_token(digit_string, prefix):
            continue
        return match
    # Only version-like digits — treat as singleton (no sequence).
    return None


def pattern_frame_regex(base_name: str) -> str:
    """Convert ``shot.####.exr`` / ``shot.%04d.exr`` to a fixed-width digit capture."""
    hash_match = re.search(r"#+", base_name)
    if hash_match:
        n = len(hash_match.group(0))
        return (
            "^"
            + re.escape(base_name[: hash_match.start()])
            + r"(\d{"
            + str(n)
            + r"})"
            + re.escape(base_name[hash_match.end() :])
            + "$"
        )
    pct_match = re.search(r"%(\d*)d", base_name)
    if pct_match:
        n = int(pct_match.group(1)) if pct_match.group(1) else 1
        return (
            "^"
            + re.escape(base_name[: pct_match.start()])
            + r"(\d{"
            + str(n)
            + r"})"
            + re.escape(base_name[pct_match.end() :])
            + "$"
        )
    return "^" + re.escape(base_name) + "$"


def placeholder_rgba(width: int, height: int, mode: str = "Flat Gray") -> np.ndarray:
    """Synthesize float16 RGBA placeholder matching C++ fill_placeholder_pixels."""
    w = max(1, int(width))
    h = max(1, int(height))
    img = np.empty((h, w, 4), dtype=np.float16)
    if mode == "Red X":
        img[:] = np.float16(0.1)
        img[..., 3] = np.float16(1.0)
        yy, xx = np.mgrid[0:h, 0:w]
        main = np.abs(yy / h - xx / w) < 0.015
        anti = np.abs(yy / h - (1.0 - xx / w)) < 0.015
        mask = main | anti
        img[mask, 0] = np.float16(1.0)
        img[mask, 1] = np.float16(0.0)
        img[mask, 2] = np.float16(0.0)
        img[mask, 3] = np.float16(1.0)
    else:
        # Flat Gray (and Nearest Frame when no neighbor exists)
        img[..., :3] = np.float16(0.05)
        img[..., 3] = np.float16(1.0)
    return img


class BaseDecoder(ABC):
    def _find_sequence_from_single_file(self, file_path: str) -> List[Tuple[int, str]]:
        file_path = os.path.abspath(file_path)
        if not os.path.isfile(file_path):
            return []

        dir_name = os.path.dirname(file_path)
        base_name = os.path.basename(file_path)
        name_part, ext = os.path.splitext(base_name)

        match = _frame_token_match(name_part)
        if not match:
            return []

        digit_string = match.group(1)
        pad = len(digit_string)
        start_pos = match.start(1)
        end_pos = match.end(1)

        prefix = name_part[:start_pos]
        suffix = name_part[end_pos:]

        # Sibling membership requires the same zero-padding width as the seed.
        pattern = (
            "^"
            + re.escape(prefix)
            + r"(\d{"
            + str(pad)
            + r"})"
            + re.escape(suffix)
            + re.escape(ext)
            + "$"
        )
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

    def get_file_path(self, frame_index: int, fallback_nearest: bool = False) -> str | None:
        """
        Returns the absolute file path of the frame at the specified index,
        or None if not a discrete image sequence file.
        """
        return None

    def uses_native_path_decode(self) -> bool:
        """
        True when frames are discrete image files decoded via C++ OIIO.
        False for container media (e.g. QuickTime) that use native movie decode
        or a PythonFallback callback (tests).
        """
        return False

    def uses_native_movie_decode(self) -> bool:
        """
        True when frames come from a shared C++ NativeMovieDecoder (FFmpeg).
        """
        return False

    @abstractmethod
    def close(self):
        """
        Clean up open files or media streams.
        """
        pass
