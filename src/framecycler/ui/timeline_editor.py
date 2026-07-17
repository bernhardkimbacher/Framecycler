"""Multi-lane NLE-style timeline with a fixed display lane and per-shot version stacks."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto
from typing import List, Optional, Sequence, Set

from PySide6.QtCore import QPoint, QRect, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QPainter, QPen, QWheelEvent
from PySide6.QtWidgets import QWidget

from ..core.timecode import Timecode
from .fonts import ui_font

RULER_H = 22
LANE_H = 22
MARGIN_X = 10
TRIM_HANDLE_W = 6
MIN_PPF = 0.05
MAX_PPF = 40.0


@dataclass
class TimelineVersionInfo:
    name: str
    is_active: bool = False
    is_compare: bool = False
    offline: bool = False
    source_start: int = 0
    duration: int = 0
    available_start: int = 0
    available_count: int = 0


@dataclass
class TimelineSegmentInfo:
    index: int
    global_start: int
    global_end: int
    versions: List[TimelineVersionInfo] = field(default_factory=list)

    @property
    def active_index(self) -> int:
        for i, version in enumerate(self.versions):
            if version.is_active:
                return i
        return 0

    @property
    def frame_count(self) -> int:
        return max(0, self.global_end - self.global_start + 1)


def stack_offset_for_active(active_index: int, lane_h: int = LANE_H) -> float:
    """Pixel offset so version `active_index` sits on the display lane (y += offset)."""
    return -float(active_index) * lane_h


def active_index_for_stack_offset(
    offset_px: float,
    version_count: int,
    lane_h: int = LANE_H,
) -> int:
    """Map a stack Y offset to the version index aligned with the display lane."""
    if version_count <= 0:
        return 0
    if lane_h <= 0:
        return 0
    # y(i) = display + offset + i * lane_h; want y(i) closest to display → i ≈ -offset/lane_h
    index = int(round(-offset_px / lane_h))
    return max(0, min(version_count - 1, index))


class _DragMode(Enum):
    NONE = auto()
    SCRUB = auto()
    PAN = auto()
    STACK_VERTICAL = auto()
    SHOT_REORDER = auto()
    TRIM_LEFT = auto()
    TRIM_RIGHT = auto()


class TimelineEditor(QWidget):
    frame_changed = Signal(int)
    in_out_changed = Signal(int, int)
    active_version_changed = Signal(int, int)  # shot_index, version_index
    shots_reordered = Signal(object)  # list[int] permutation
    shot_trimmed = Signal(int, int, int)  # shot_index, source_start, duration

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(64)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)

        self.start_frame = 0
        self.end_frame = 100
        self.current_frame = 0
        self.in_point = 0
        self.out_point = 100
        self.fps = 24.0
        self.show_timecode = False
        self.cached_frames: Set[int] = set()
        self.display_cached_frames: Set[int] = set()
        self.segments: List[TimelineSegmentInfo] = []

        self._view_start = 0.0
        self._ppf = 2.0
        self._pending_fit = False
        self._drag = _DragMode.NONE
        self._drag_shot = -1
        self._drag_origin = QPoint()
        self._drag_start_frame = 0
        self._preview_offset = 0.0
        self._reorder_preview: Optional[List[int]] = None
        self._trim_source_start = 0
        self._trim_duration = 0
        self._trim_origin_start = 0
        self._trim_origin_duration = 0
        self._pan_view_start = 0.0

    # ----- public API (MainWindow) -----

    def set_display_options(self, show_timecode: bool, fps: float):
        self.show_timecode = show_timecode
        self.fps = fps
        self.update()

    def set_range(self, start: int, end: int):
        old_start, old_end = self.start_frame, self.end_frame
        self.start_frame = start
        self.end_frame = max(start, end)
        self.in_point = max(self.start_frame, min(self.end_frame, self.in_point))
        self.out_point = max(self.in_point, min(self.end_frame, self.out_point))
        self.current_frame = max(self.start_frame, min(self.end_frame, self.current_frame))
        # Refit when the sequence span changes (new/removed/trimmed clips).
        if self.start_frame != old_start or self.end_frame != old_end:
            self.fit_to_sequence()
        else:
            self.update()

    def fit_to_sequence(self):
        """Zoom/pan so the full start..end range fills the timeline width."""
        span = max(1, self.end_frame - self.start_frame + 1)
        track_w = self._track_width()
        if track_w <= 1 or self.width() <= 2 * MARGIN_X:
            self._pending_fit = True
            self.update()
            return
        self._ppf = max(MIN_PPF, min(MAX_PPF, track_w / float(span)))
        self._view_start = float(self.start_frame)
        self._pending_fit = False
        self.update()

    def set_current_frame(self, frame: int):
        self.current_frame = max(self.start_frame, min(self.end_frame, frame))
        self.update()

    def set_in_out(self, in_pt: int, out_pt: int):
        self.in_point = max(self.start_frame, min(self.end_frame, in_pt))
        self.out_point = max(self.in_point, min(self.end_frame, out_pt))
        self.in_out_changed.emit(self.in_point, self.out_point)
        self.update()

    def set_cached_frames(self, cached: set, display_cached: set | None = None):
        """Set decode/RAM and optional display/VRAM cache frames (global timeline space)."""
        self.cached_frames = set(cached or [])
        # None means leave VRAM set unchanged; pass set() to clear both.
        if display_cached is not None:
            self.display_cached_frames = set(display_cached)
        self.update()

    @staticmethod
    def coalesce_frame_runs(frames: Set[int]) -> List[tuple[int, int]]:
        """Group sorted global frames into inclusive (start, end) runs."""
        if not frames:
            return []
        cached_sorted = sorted(frames)
        groups: List[tuple[int, int]] = []
        start_block = prev = cached_sorted[0]
        for f in cached_sorted[1:]:
            if f == prev + 1:
                prev = f
            else:
                groups.append((start_block, prev))
                start_block = prev = f
        groups.append((start_block, prev))
        return groups

    def set_shot_markers(self, markers: list):
        """Back-compat: convert (start, end, version_count) markers into bare segments."""
        segments: List[TimelineSegmentInfo] = []
        for i, item in enumerate(markers or []):
            if len(item) >= 3:
                g0, g1, vcount = int(item[0]), int(item[1]), int(item[2])
            else:
                continue
            versions = [
                TimelineVersionInfo(name=f"v{j}", is_active=(j == 0))
                for j in range(max(1, vcount))
            ]
            segments.append(TimelineSegmentInfo(i, g0, g1, versions))
        self.set_segments(segments)

    def set_segments(self, segments: Sequence[TimelineSegmentInfo]):
        self.segments = list(segments or [])
        self._reorder_preview = None
        self._preview_offset = 0.0
        self._drag_shot = -1
        self.update()

    def set_plan_segments(self, segments: Sequence[TimelineSegmentInfo]):
        self.set_segments(segments)

    # ----- geometry -----

    def _track_width(self) -> int:
        return max(1, self.width() - 2 * MARGIN_X)

    def _body_top(self) -> int:
        return RULER_H

    def _body_height(self) -> int:
        return max(LANE_H * 3, self.height() - RULER_H)

    def _display_lane_y(self) -> int:
        return self._body_top() + self._body_height() // 2 - LANE_H // 2

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._pending_fit:
            self.fit_to_sequence()

    def _frame_from_x(self, x: float) -> int:
        frame = self._view_start + (x - MARGIN_X) / max(self._ppf, 1e-6)
        return int(round(max(self.start_frame, min(self.end_frame, frame))))

    def _x_from_frame(self, frame: float) -> int:
        return int(MARGIN_X + (frame - self._view_start) * self._ppf)

    def _ordered_segments(self) -> List[TimelineSegmentInfo]:
        order = self._reorder_preview
        if order is None:
            return self.segments
        try:
            return [self.segments[i] for i in order]
        except IndexError:
            return self.segments

    def _layout_segments_for_draw(self) -> List[TimelineSegmentInfo]:
        """Segments in draw order; during reorder preview, retarget time ranges sequentially."""
        segs = self._ordered_segments()
        if self._reorder_preview is None:
            return segs
        cursor = self.start_frame
        laid_out: List[TimelineSegmentInfo] = []
        for seg in segs:
            count = seg.frame_count
            end = cursor + count - 1 if count > 0 else cursor - 1
            laid_out.append(
                TimelineSegmentInfo(
                    index=seg.index,
                    global_start=cursor,
                    global_end=end,
                    versions=seg.versions,
                )
            )
            cursor = end + 1
        return laid_out

    def _segment_at_x(self, x: int) -> Optional[TimelineSegmentInfo]:
        frame = self._frame_from_x(x)
        for seg in self._layout_segments_for_draw():
            if seg.global_start <= frame <= seg.global_end:
                # Return the live segment (stable index) for interactions
                return next((s for s in self.segments if s.index == seg.index), seg)
        return None

    def _version_rect(self, seg: TimelineSegmentInfo, version_index: int, offset: float) -> QRect:
        x0 = self._x_from_frame(seg.global_start)
        x1 = self._x_from_frame(seg.global_end + 1)
        y = int(self._display_lane_y() + offset + version_index * LANE_H)
        return QRect(x0, y, max(2, x1 - x0), LANE_H - 1)

    def _stack_offset(self, seg: TimelineSegmentInfo) -> float:
        base = stack_offset_for_active(seg.active_index)
        if self._drag == _DragMode.STACK_VERTICAL and self._drag_shot == seg.index:
            return base + self._preview_offset
        return base

    # ----- hit testing -----

    def _hit_trim_edge(self, pos: QPoint, seg: TimelineSegmentInfo) -> Optional[_DragMode]:
        active = seg.active_index
        rect = self._version_rect(seg, active, self._stack_offset(seg))
        if abs(pos.x() - rect.left()) <= TRIM_HANDLE_W and rect.adjusted(0, -2, 0, 2).contains(pos):
            return _DragMode.TRIM_LEFT
        if abs(pos.x() - rect.right()) <= TRIM_HANDLE_W and rect.adjusted(0, -2, 0, 2).contains(pos):
            return _DragMode.TRIM_RIGHT
        return None

    def _hit_stack(self, pos: QPoint) -> Optional[TimelineSegmentInfo]:
        for seg in self._layout_segments_for_draw():
            offset = self._stack_offset(seg)
            for vi in range(len(seg.versions)):
                if self._version_rect(seg, vi, offset).contains(pos):
                    return next((s for s in self.segments if s.index == seg.index), seg)
        return None

    # ----- events -----

    def wheelEvent(self, event: QWheelEvent):
        delta = event.angleDelta().y()
        if delta == 0:
            return
        mods = event.modifiers()
        if mods & Qt.ControlModifier:
            anchor_x = event.position().x()
            anchor_frame = self._view_start + (anchor_x - MARGIN_X) / max(self._ppf, 1e-6)
            factor = 1.15 if delta > 0 else 1 / 1.15
            self._ppf = max(MIN_PPF, min(MAX_PPF, self._ppf * factor))
            self._view_start = anchor_frame - (anchor_x - MARGIN_X) / max(self._ppf, 1e-6)
            self.update()
        else:
            # Horizontal pan (shift or plain vertical wheel)
            frames = (-delta / 120.0) * max(1.0, (self.end_frame - self.start_frame + 1) * 0.02)
            if mods & Qt.ShiftModifier:
                frames *= 3.0
            self._view_start += frames
            self.update()
        event.accept()

    def mousePressEvent(self, event):
        pos = event.position().toPoint()
        if event.button() == Qt.MiddleButton or (
            event.button() == Qt.LeftButton and event.modifiers() & Qt.AltModifier
        ):
            self._drag = _DragMode.PAN
            self._drag_origin = pos
            self._pan_view_start = self._view_start
            return

        if event.button() != Qt.LeftButton:
            return

        self._drag_origin = pos
        self._drag_start_frame = self._frame_from_x(pos.x())

        if pos.y() < self._body_top():
            self._drag = _DragMode.SCRUB
            frame = self._frame_from_x(pos.x())
            self.set_current_frame(frame)
            self.frame_changed.emit(frame)
            return

        seg = self._segment_at_x(pos.x())
        if seg is not None:
            trim = self._hit_trim_edge(pos, seg)
            if trim is not None:
                active = seg.versions[seg.active_index] if seg.versions else None
                self._drag = trim
                self._drag_shot = seg.index
                if active is not None:
                    self._trim_origin_start = active.source_start
                    self._trim_origin_duration = max(1, active.duration or seg.frame_count)
                    self._trim_source_start = self._trim_origin_start
                    self._trim_duration = self._trim_origin_duration
                return

            hit = self._hit_stack(pos)
            if hit is not None:
                self._drag_shot = hit.index
                self._drag = _DragMode.NONE  # decide on move: vertical vs horizontal
                self._preview_offset = 0.0
                return

        self._drag = _DragMode.SCRUB
        frame = self._frame_from_x(pos.x())
        self.set_current_frame(frame)
        self.frame_changed.emit(frame)

    def mouseMoveEvent(self, event):
        pos = event.position().toPoint()
        if self._drag == _DragMode.PAN:
            dx = pos.x() - self._drag_origin.x()
            self._view_start = self._pan_view_start - dx / max(self._ppf, 1e-6)
            self.update()
            return

        if self._drag == _DragMode.SCRUB:
            frame = self._frame_from_x(pos.x())
            self.set_current_frame(frame)
            self.frame_changed.emit(frame)
            return

        if self._drag in (_DragMode.TRIM_LEFT, _DragMode.TRIM_RIGHT):
            seg = next((s for s in self.segments if s.index == self._drag_shot), None)
            if seg is None or not seg.versions:
                return
            active = seg.versions[seg.active_index]
            avail_start = active.available_start
            avail_count = max(1, active.available_count)
            avail_end = avail_start + avail_count
            frame = self._frame_from_x(pos.x())
            delta = frame - self._drag_start_frame
            if self._drag == _DragMode.TRIM_LEFT:
                right_media = self._trim_origin_start + self._trim_origin_duration
                new_start = max(avail_start, min(right_media - 1, self._trim_origin_start + delta))
                self._trim_source_start = new_start
                self._trim_duration = max(1, right_media - new_start)
            else:
                self._trim_source_start = self._trim_origin_start
                self._trim_duration = max(
                    1,
                    min(avail_end - self._trim_origin_start, self._trim_origin_duration + delta),
                )
            self.update()
            return

        # Pending stack interaction: choose mode from dominant axis
        if self._drag_shot >= 0 and self._drag == _DragMode.NONE and event.buttons() & Qt.LeftButton:
            dx = pos.x() - self._drag_origin.x()
            dy = pos.y() - self._drag_origin.y()
            if abs(dx) < 3 and abs(dy) < 3:
                return
            if abs(dy) >= abs(dx):
                self._drag = _DragMode.STACK_VERTICAL
            else:
                self._drag = _DragMode.SHOT_REORDER
                self._reorder_preview = list(range(len(self.segments)))

        if self._drag == _DragMode.STACK_VERTICAL:
            self._preview_offset = float(pos.y() - self._drag_origin.y())
            self.update()
            return

        if self._drag == _DragMode.SHOT_REORDER:
            self._update_reorder_preview(pos.x())
            self.update()
            return

        # Cursor feedback
        seg = self._segment_at_x(pos.x())
        if seg is not None and self._hit_trim_edge(pos, seg):
            self.setCursor(Qt.SizeHorCursor)
        elif self._hit_stack(pos) is not None:
            self.setCursor(Qt.SizeVerCursor)
        else:
            self.setCursor(Qt.ArrowCursor)

    def mouseReleaseEvent(self, event):
        if event.button() not in (Qt.LeftButton, Qt.MiddleButton):
            return

        if self._drag == _DragMode.STACK_VERTICAL and self._drag_shot >= 0:
            seg = next((s for s in self.segments if s.index == self._drag_shot), None)
            if seg is not None and seg.versions:
                base = stack_offset_for_active(seg.active_index)
                new_index = active_index_for_stack_offset(base + self._preview_offset, len(seg.versions))
                if new_index != seg.active_index:
                    self.active_version_changed.emit(seg.index, new_index)

        elif self._drag == _DragMode.SHOT_REORDER and self._reorder_preview is not None:
            if self._reorder_preview != list(range(len(self.segments))):
                self.shots_reordered.emit(list(self._reorder_preview))

        elif self._drag in (_DragMode.TRIM_LEFT, _DragMode.TRIM_RIGHT) and self._drag_shot >= 0:
            self.shot_trimmed.emit(self._drag_shot, self._trim_source_start, self._trim_duration)

        elif self._drag == _DragMode.NONE and self._drag_shot >= 0 and event.button() == Qt.LeftButton:
            # Click without drag: scrub to frame under cursor
            frame = self._frame_from_x(event.position().x())
            self.set_current_frame(frame)
            self.frame_changed.emit(frame)

        self._drag = _DragMode.NONE
        self._drag_shot = -1
        self._preview_offset = 0.0
        self._reorder_preview = None
        self.setCursor(Qt.ArrowCursor)
        self.update()

    def _update_reorder_preview(self, x: int):
        if not self.segments:
            return
        n = len(self.segments)
        order = list(range(n))
        if self._drag_shot < 0 or self._drag_shot >= n:
            self._reorder_preview = order
            return
        # Target insert index by center of other shots
        centers = []
        for seg in self.segments:
            cx = (self._x_from_frame(seg.global_start) + self._x_from_frame(seg.global_end + 1)) / 2.0
            centers.append((seg.index, cx))
        centers.sort(key=lambda t: t[1])
        target = n - 1
        for i, (idx, cx) in enumerate(centers):
            if x < cx:
                target = i
                break
        order.remove(self._drag_shot)
        order.insert(min(target, len(order)), self._drag_shot)
        self._reorder_preview = order

    # ----- painting -----

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        rect = self.rect()
        painter.fillRect(rect, QColor(22, 22, 24))

        body_top = self._body_top()
        display_y = self._display_lane_y()

        # Ruler
        painter.fillRect(0, 0, rect.width(), RULER_H, QColor(30, 30, 34))
        painter.setPen(QPen(QColor(70, 70, 78)))
        painter.drawLine(0, RULER_H - 1, rect.width(), RULER_H - 1)
        self._paint_ruler(painter)

        # Display lane band
        painter.fillRect(
            MARGIN_X,
            display_y,
            self._track_width(),
            LANE_H,
            QBrush(QColor(50, 70, 100, 70)),
        )
        painter.setPen(QPen(QColor(90, 140, 220, 180), 1, Qt.DashLine))
        painter.drawRect(MARGIN_X, display_y, self._track_width(), LANE_H)

        # In/out shade across display lane
        x_in = self._x_from_frame(self.in_point)
        x_out = self._x_from_frame(self.out_point)
        painter.fillRect(
            x_in, display_y, max(1, x_out - x_in), LANE_H,
            QBrush(QColor(255, 165, 0, 28)),
        )

        # Cache indicator lines above display lane
        self._paint_cache_lines(painter, display_y)

        # Segments / version stacks
        for seg in self._layout_segments_for_draw():
            self._paint_segment(painter, seg)

        # In/out brackets
        painter.setPen(QPen(QColor(255, 180, 0), 2))
        for x, sign in ((x_in, 1), (x_out, -1)):
            painter.drawLine(x, display_y - 2, x, display_y + LANE_H + 2)
            painter.drawLine(x, display_y - 2, x + 4 * sign, display_y - 2)
            painter.drawLine(x, display_y + LANE_H + 2, x + 4 * sign, display_y + LANE_H + 2)

        # Playhead
        self._paint_playhead(painter, body_top)
        painter.end()

    def _paint_ruler(self, painter: QPainter):
        painter.setFont(ui_font(9))
        painter.setPen(QPen(QColor(160, 160, 165)))
        track_w = self._track_width()
        if track_w <= 0:
            return
        span = max(1.0, track_w / max(self._ppf, 1e-6))
        step = 10
        for candidate in (1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000):
            if candidate * self._ppf >= 50:
                step = candidate
                break
        first = int(self._view_start) - (int(self._view_start) % step)
        f = first
        end_vis = self._view_start + span + step
        while f <= end_vis:
            x = self._x_from_frame(f)
            if MARGIN_X <= x <= self.width() - MARGIN_X:
                painter.drawLine(x, RULER_H - 6, x, RULER_H - 1)
                label = Timecode.format_position_label(f, self.show_timecode, self.fps, prefixed=False)
                painter.drawText(x + 2, 12, label)
            f += step

    def _paint_cache_lines(self, painter: QPainter, display_y: int):
        """Two thin strips above the display lane: green=RAM, purple=VRAM."""
        line_h = 2
        gap = 1
        green_y = display_y - (line_h * 2 + gap + 1)
        purple_y = display_y - (line_h + 1)
        self._paint_frame_runs(
            painter, self.cached_frames, green_y, line_h, QColor(0, 120, 40)
        )
        self._paint_frame_runs(
            painter, self.display_cached_frames, purple_y, line_h, QColor(123, 77, 255)
        )

    def _paint_frame_runs(
        self,
        painter: QPainter,
        frames: Set[int],
        y: int,
        height: int,
        color: QColor,
    ):
        if not frames or height <= 0:
            return
        brush = QBrush(color)
        for start_f, end_f in self.coalesce_frame_runs(frames):
            x0 = self._x_from_frame(start_f)
            x1 = self._x_from_frame(end_f + 1)
            painter.fillRect(x0, y, max(2, x1 - x0), height, brush)

    def _paint_segment(self, painter: QPainter, seg: TimelineSegmentInfo):
        offset = self._stack_offset(seg)
        active_i = seg.active_index
        for vi, version in enumerate(seg.versions):
            rect = self._version_rect(seg, vi, offset)
            if rect.right() < MARGIN_X or rect.left() > self.width() - MARGIN_X:
                continue
            if version.offline:
                fill = QColor(70, 50, 50)
            elif vi == active_i:
                fill = QColor(70, 110, 170)
            else:
                fill = QColor(55, 55, 62)
            if self._drag == _DragMode.SHOT_REORDER and self._drag_shot == seg.index:
                fill = QColor(fill.red(), fill.green(), fill.blue(), 180)
            # Preview trim width for active
            if (
                self._drag in (_DragMode.TRIM_LEFT, _DragMode.TRIM_RIGHT)
                and self._drag_shot == seg.index
                and vi == active_i
            ):
                # Show trimmed duration relative to original segment start
                x0 = self._x_from_frame(seg.global_start)
                x1 = self._x_from_frame(seg.global_start + self._trim_duration)
                rect = QRect(x0, rect.y(), max(2, x1 - x0), rect.height())

            painter.fillRect(rect, QBrush(fill))
            border = QColor(140, 180, 240) if vi == active_i else QColor(90, 90, 100)
            painter.setPen(QPen(border, 1))
            painter.drawRect(rect.adjusted(0, 0, -1, -1))

            painter.setFont(ui_font(9))
            painter.setPen(QPen(QColor(220, 220, 220)))
            label = version.name
            if version.is_compare and not version.is_active:
                label = f"[C] {label}"
            if version.offline:
                label = f"[OFF] {label}"
            painter.drawText(rect.adjusted(4, 0, -4, 0), Qt.AlignVCenter | Qt.AlignLeft, label)

            if vi == active_i:
                # Trim handles
                painter.fillRect(rect.left(), rect.top(), 3, rect.height(), QColor(255, 200, 80))
                painter.fillRect(rect.right() - 2, rect.top(), 3, rect.height(), QColor(255, 200, 80))

    def _paint_playhead(self, painter: QPainter, body_top: int):
        x = self._x_from_frame(self.current_frame)
        label = Timecode.format_position_label(
            self.current_frame, self.show_timecode, self.fps, prefixed=False
        )
        painter.setFont(ui_font(10))
        metrics = painter.fontMetrics()
        text_w = metrics.horizontalAdvance(label)
        text_h = metrics.height()
        label_x = max(MARGIN_X, min(self.width() - MARGIN_X - text_w, x - text_w // 2))
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(QColor(30, 30, 30, 220)))
        painter.drawRoundedRect(label_x - 4, 2, text_w + 8, text_h + 2, 3, 3)
        painter.setPen(QPen(QColor(220, 220, 220)))
        painter.drawText(label_x, 2 + text_h - 2, label)

        painter.setPen(QPen(QColor(230, 30, 30), 2))
        painter.drawLine(x, body_top, x, self.height() - 2)
        poly = [
            QPoint(x - 5, body_top + 2),
            QPoint(x + 5, body_top + 2),
            QPoint(x, body_top + 8),
        ]
        painter.setBrush(QBrush(QColor(230, 30, 30)))
        painter.setPen(Qt.NoPen)
        painter.drawPolygon(poly)
