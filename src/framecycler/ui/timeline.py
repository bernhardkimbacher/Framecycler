from PySide6.QtWidgets import QWidget
from PySide6.QtCore import Qt, QRect, Signal, QPoint
from PySide6.QtGui import QPainter, QColor, QPen, QBrush

from ..core.timecode import Timecode
from .fonts import ui_font

class Timeline(QWidget):
    frame_changed = Signal(int)
    in_out_changed = Signal(int, int)
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(52)
        
        # Ranges & playback playhead state
        self.start_frame = 0
        self.end_frame = 100
        self.current_frame = 0
        self.in_point = 0
        self.out_point = 100
        self.fps = 24.0
        self.show_timecode = False
        
        # Caching information
        self.cached_frames = set()

        # Shot segment markers: list of (global_start, global_end, version_count)
        self.shot_markers: list[tuple[int, int, int]] = []
        
        # Dragging state
        self.scrubbing = False

    def set_display_options(self, show_timecode: bool, fps: float):
        self.show_timecode = show_timecode
        self.fps = fps
        self.update()
        
    def set_range(self, start: int, end: int):
        self.start_frame = start
        self.end_frame = max(start + 1, end)
        self.in_point = start
        self.out_point = end
        self.current_frame = max(start, min(end, self.current_frame))
        self.update()

    def set_current_frame(self, frame: int):
        self.current_frame = max(self.start_frame, min(self.end_frame, frame))
        self.update()

    def set_in_out(self, in_pt: int, out_pt: int):
        self.in_point = max(self.start_frame, min(self.end_frame, in_pt))
        self.out_point = max(self.in_point, min(self.end_frame, out_pt))
        self.in_out_changed.emit(self.in_point, self.out_point)
        self.update()

    def set_cached_frames(self, cached: set):
        self.cached_frames = cached
        self.update()

    def set_shot_markers(self, markers: list[tuple[int, int, int]]):
        """markers: [(global_start, global_end, version_count), ...]"""
        self.shot_markers = list(markers or [])
        self.update()

    def _frame_from_x(self, x: int) -> int:
        w = self.width() - 20
        if w <= 0:
            return self.start_frame
        ratio = (x - 10) / w
        ratio = max(0.0, min(1.0, ratio))
        frame_range = self.end_frame - self.start_frame
        return int(round(self.start_frame + ratio * frame_range))

    def _x_from_frame(self, frame: int) -> int:
        w = self.width() - 20
        frame_range = self.end_frame - self.start_frame
        if frame_range <= 0:
            return 10
        ratio = (frame - self.start_frame) / frame_range
        return int(10 + ratio * w)

    # Mouse scrubbing implementation
    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.scrubbing = True
            frame = self._frame_from_x(event.position().x())
            self.set_current_frame(frame)
            self.frame_changed.emit(frame)

    def mouseMoveEvent(self, event):
        if self.scrubbing:
            frame = self._frame_from_x(event.position().x())
            self.set_current_frame(frame)
            self.frame_changed.emit(frame)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.scrubbing = False

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        
        rect = self.rect()
        
        # Draw background bar
        painter.fillRect(rect, QColor(25, 25, 25))
        
        # Track coordinates
        track_y = rect.height() - 20
        track_h = 10
        track_x = 10
        track_w = rect.width() - 20
        
        # Draw track background
        painter.fillRect(track_x, track_y, track_w, track_h, QBrush(QColor(45, 45, 45)))

        # Shot boundaries and multi-version indicators
        if self.shot_markers and track_w > 0:
            for i, (seg_start, seg_end, version_count) in enumerate(self.shot_markers):
                x0 = self._x_from_frame(seg_start)
                x1 = self._x_from_frame(seg_end + 1)
                if version_count > 1:
                    painter.fillRect(
                        x0, track_y - 3, max(1, x1 - x0), 3,
                        QBrush(QColor(90, 140, 220, 160)),
                    )
                if i > 0:
                    painter.setPen(QPen(QColor(180, 180, 180, 160), 1))
                    painter.drawLine(x0, track_y - 4, x0, track_y + track_h + 2)
        
        # Draw Cache status blocks (green cache indicators)
        cache_brush = QBrush(QColor(0, 100, 0, 120))  # Dark forest green for cache blocks
        frame_range = self.end_frame - self.start_frame + 1
        
        if frame_range > 0 and track_w > 0:
            # Optimize: Group continuous cached blocks to avoid drawing thousands of single lines
            cached_sorted = sorted(list(self.cached_frames))
            if cached_sorted:
                groups = []
                start_block = cached_sorted[0]
                prev_block = cached_sorted[0]
                
                for f in cached_sorted[1:]:
                    if f == prev_block + 1:
                        prev_block = f
                    else:
                        groups.append((start_block, prev_block))
                        start_block = f
                        prev_block = f
                groups.append((start_block, prev_block))
                
                # Draw groups
                for start_f, end_f in groups:
                    x_start = self._x_from_frame(start_f)
                    # Width goes until next frame boundary
                    x_end = self._x_from_frame(end_f + 1)
                    block_w = max(2, x_end - x_start)
                    painter.fillRect(x_start, track_y, block_w, track_h, cache_brush)
        
        # Draw playback active range highlight (yellow/orange outline or shaded)
        x_in = self._x_from_frame(self.in_point)
        x_out = self._x_from_frame(self.out_point)
        painter.fillRect(x_in, track_y, x_out - x_in, track_h, QBrush(QColor(255, 165, 0, 20)))  # Semi-transparent yellow shade
        
        # Draw In/Out brackets
        in_color = QColor(255, 180, 0)
        out_color = QColor(255, 180, 0)
        
        # In point bracket '['
        painter.setPen(QPen(in_color, 2))
        painter.drawLine(x_in, track_y - 2, x_in, track_y + track_h + 2)
        painter.drawLine(x_in, track_y - 2, x_in + 4, track_y - 2)
        painter.drawLine(x_in, track_y + track_h + 2, x_in + 4, track_y + track_h + 2)
        
        # Out point bracket ']'
        painter.setPen(QPen(out_color, 2))
        painter.drawLine(x_out, track_y - 2, x_out, track_y + track_h + 2)
        painter.drawLine(x_out, track_y - 2, x_out - 4, track_y - 2)
        painter.drawLine(x_out, track_y + track_h + 2, x_out - 4, track_y + track_h + 2)
        
        # Draw playhead position (label, triangle, and line)
        x_playhead = self._x_from_frame(self.current_frame)
        label = Timecode.format_position_label(
            self.current_frame, self.show_timecode, self.fps, prefixed=False
        )

        label_font = ui_font(10)
        painter.setFont(label_font)
        metrics = painter.fontMetrics()
        text_w = metrics.horizontalAdvance(label)
        text_h = metrics.height()
        label_x = max(track_x, min(track_x + track_w - text_w, x_playhead - text_w // 2))
        label_y = 4

        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(QColor(30, 30, 30, 220)))
        painter.drawRoundedRect(label_x - 4, label_y, text_w + 8, text_h + 4, 3, 3)
        painter.setPen(QPen(QColor(220, 220, 220)))
        painter.drawText(label_x, label_y + text_h, label)

        tri_top = label_y + text_h + 8
        painter.setPen(QPen(QColor(230, 30, 30), 2))
        painter.drawLine(x_playhead, tri_top, x_playhead, track_y + track_h + 2)

        poly = [
            QPoint(x_playhead - 5, tri_top + 5),
            QPoint(x_playhead + 5, tri_top + 5),
            QPoint(x_playhead, tri_top),
        ]
        painter.setBrush(QBrush(QColor(230, 30, 30)))
        painter.setPen(Qt.NoPen)
        painter.drawPolygon(poly)
        
        painter.end()
