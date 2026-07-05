from PySide6.QtWidgets import QWidget
from PySide6.QtCore import Qt, QRect, Signal, QPoint
from PySide6.QtGui import QPainter, QColor, QPen, QBrush, QFont

class Timeline(QWidget):
    frame_changed = Signal(int)
    in_out_changed = Signal(int, int)
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(45)
        
        # Ranges & playback playhead state
        self.start_frame = 0
        self.end_frame = 100
        self.current_frame = 0
        self.in_point = 0
        self.out_point = 100
        
        # Caching information
        self.cached_frames = set()
        
        # Dragging state
        self.scrubbing = False
        
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
        
        # Draw ticks/numbers at top of timeline
        painter.setPen(QPen(QColor(100, 100, 100), 1))
        painter.setFont(QFont("Courier New", 7))
        
        # Ticks step calculation
        step = max(1, frame_range // 10)
        # Round step to logical increments
        if step > 100:
            step = (step // 100) * 100
        elif step > 10:
            step = (step // 10) * 10
            
        for f in range(self.start_frame, self.end_frame + 1, step):
            tx = self._x_from_frame(f)
            painter.drawLine(tx, track_y - 4, tx, track_y)
            painter.drawText(tx - 10, track_y - 6, str(f))
            
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
        
        # Draw Playhead position (thin red line + triangle)
        x_playhead = self._x_from_frame(self.current_frame)
        painter.setPen(QPen(QColor(230, 30, 30), 2))
        painter.drawLine(x_playhead, track_y - 6, x_playhead, track_y + track_h + 2)
        
        # Draw playhead triangle at top
        poly = [
            QPoint(x_playhead - 5, track_y - 6),
            QPoint(x_playhead + 5, track_y - 6),
            QPoint(x_playhead, track_y - 1)
        ]
        painter.setBrush(QBrush(QColor(230, 30, 30)))
        painter.setPen(Qt.NoPen)
        painter.drawPolygon(poly)
        
        painter.end()
