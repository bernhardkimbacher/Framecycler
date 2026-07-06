import math

class Timecode:
    @staticmethod
    def frame_to_timecode(frame_number: int, fps: float, start_frame: int = 0) -> str:
        """
        Converts a frame number to a SMPTE timecode string (HH:MM:SS:FF).
        """
        actual_frame = frame_number + start_frame
        if actual_frame < 0:
            actual_frame = 0
            
        # Non-drop frame calculation
        fps_rounded = int(round(fps))
        if fps_rounded <= 0:
            fps_rounded = 24
            
        frames = actual_frame % fps_rounded
        seconds = (actual_frame // fps_rounded) % 60
        minutes = ((actual_frame // fps_rounded) // 60) % 60
        hours = (((actual_frame // fps_rounded) // 60) // 60) % 24
        
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}:{frames:02d}"

    @staticmethod
    def timecode_to_frame(tc_str: str, fps: float, start_frame: int = 0) -> int:
        """
        Converts a SMPTE timecode string (HH:MM:SS:FF) back to a frame number.
        """
        try:
            parts = tc_str.split(":")
            if len(parts) != 4:
                return 0
            
            hours = int(parts[0])
            minutes = int(parts[1])
            seconds = int(parts[2])
            frames = int(parts[3])
            
            fps_rounded = int(round(fps))
            if fps_rounded <= 0:
                fps_rounded = 24
                
            total_frames = (hours * 3600 * fps_rounded) + (minutes * 60 * fps_rounded) + (seconds * fps_rounded) + frames
            return total_frames - start_frame
        except Exception:
            return 0
            
    @staticmethod
    def format_frame(frame: int, show_timecode: bool, fps: float, start_frame: int = 0) -> str:
        if show_timecode:
            return Timecode.frame_to_timecode(frame, fps, start_frame)
        return str(frame)

    @staticmethod
    def format_position_label(frame: int, show_timecode: bool, fps: float, start_frame: int = 0) -> str:
        if show_timecode:
            return f"TC: {Timecode.frame_to_timecode(frame, fps, start_frame)}"
        return f"FR: {frame:04d}"
