"""Floating annotation paint overlay; input is routed from Viewport (Metal-safe)."""

from __future__ import annotations

from typing import Callable, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPainter, QPaintEvent
from PySide6.QtWidgets import QInputDialog, QWidget

from ..translucent_window import FLOATING_OVERLAY_FLAGS, clear_translucent_backdrop
from .geometry import (
    hit_test_topmost,
    image_rect_for_viewport,
    translate_shape,
    widget_to_uv,
)
from .models import AnnotationKind, AnnotationShape, AnnotationTool, tool_to_kind
from .paint import paint_shape


class AnnotationOverlay(QWidget):
    """Paint-only Tool window. Mouse is handled via Viewport → handle_* methods."""

    shapes_changed = Signal()
    selection_changed = Signal()

    def __init__(self, viewport, parent: QWidget | None = None, *, floating: bool = True):
        if floating:
            super().__init__(parent, FLOATING_OVERLAY_FLAGS)
            self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
            self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
            # Always pass-through: native Metal surface owns input; Viewport routes draws.
            self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
            self.setWindowFlag(Qt.WindowType.WindowTransparentForInput, True)
        else:
            super().__init__(parent)
            self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
            self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
            self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

        self._viewport = viewport
        self._floating = floating
        self._shapes: list[AnnotationShape] = []
        self._tool = AnnotationTool.SELECT
        self._color = "#FFCC00"
        self._thickness = 0.004
        self._interactive = False
        self._draft: Optional[AnnotationShape] = None
        self._drag_index: Optional[int] = None
        self._drag_last_uv: Optional[tuple[float, float]] = None
        self._blocked_provider: Optional[Callable[[], bool]] = None

    def set_blocked_provider(self, provider: Callable[[], bool] | None) -> None:
        self._blocked_provider = provider

    def set_shapes(self, shapes: list[AnnotationShape]) -> None:
        self._shapes = list(shapes)
        self._draft = None
        self._drag_index = None
        # Empty content: sync repaint so macOS translucent backing store is wiped.
        if self._shapes:
            self.update()
        else:
            self.repaint()
        self.selection_changed.emit()

    def shapes(self) -> list[AnnotationShape]:
        return self._shapes

    def set_tool(self, tool: AnnotationTool) -> None:
        self._tool = tool
        self._draft = None
        self._drag_index = None
        if tool != AnnotationTool.SELECT:
            for s in self._shapes:
                s.selected = False
            self.selection_changed.emit()
        self.update()

    def tool(self) -> AnnotationTool:
        return self._tool

    def set_color(self, color: str) -> None:
        self._color = color

    def set_thickness(self, thickness: float) -> None:
        self._thickness = max(0.001, float(thickness))

    def set_interactive(self, enabled: bool) -> None:
        self._interactive = bool(enabled)
        if not self._interactive:
            self._draft = None
            self._drag_index = None
        self.update()

    def is_interactive(self) -> bool:
        return self._interactive

    def captures_left_drag(self) -> bool:
        """True when a drawing tool should steal left-drag (disable timeline scrub)."""
        return (
            self._interactive
            and not self._blocked()
            and self._tool != AnnotationTool.SELECT
        )

    def is_annotating(self) -> bool:
        """True while a stroke/select-drag is in progress."""
        return self._draft is not None or self._drag_index is not None

    def clear_all(self) -> None:
        if not self._shapes:
            return
        self._shapes.clear()
        self._draft = None
        self._drag_index = None
        self.shapes_changed.emit()
        self.selection_changed.emit()
        self.repaint()

    def delete_selected(self) -> None:
        before = len(self._shapes)
        self._shapes = [s for s in self._shapes if not s.selected]
        if len(self._shapes) != before:
            self.shapes_changed.emit()
            self.selection_changed.emit()
            self.repaint()

    def selected_count(self) -> int:
        return sum(1 for s in self._shapes if s.selected)

    def _blocked(self) -> bool:
        if self._blocked_provider is None:
            return False
        try:
            return bool(self._blocked_provider())
        except Exception:
            return False

    def _image_rect(self):
        vp = self._viewport
        scale_x, scale_y = vp._fit_scales()
        from ..viewport import COMPARE_TILE

        zoom = 1.0 if vp.compare_mode == COMPARE_TILE else vp.zoom
        # Prefer viewport size — overlay geometry matches, but input coords come from Viewport.
        w = float(vp.width()) if vp.width() > 0 else float(self.width())
        h = float(vp.height()) if vp.height() > 0 else float(self.height())
        return image_rect_for_viewport(
            w,
            h,
            scale_x,
            scale_y,
            zoom,
            float(vp.pan_offset.x()),
            float(vp.pan_offset.y()),
        )

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802
        painter = QPainter(self)
        if self._floating:
            clear_translucent_backdrop(painter, self.rect())

        image_rect = self._image_rect()
        for shape in self._shapes:
            paint_shape(painter, shape, image_rect)
        if self._draft is not None:
            paint_shape(painter, self._draft, image_rect)
        painter.end()

    # --- Input API called from Viewport (widget coords in viewport space) ---

    def handle_press(self, x: float, y: float, *, parent_widget: QWidget | None = None) -> bool:
        """Handle left-press. Returns True if the event was consumed."""
        if not self._interactive or self._blocked():
            return False
        image_rect = self._image_rect()
        uv = widget_to_uv(x, y, image_rect)
        if uv is None:
            return self.captures_left_drag()  # consume so scrub doesn't start off-image

        if self._tool == AnnotationTool.SELECT:
            idx = hit_test_topmost(self._shapes, uv[0], uv[1], image_rect=image_rect)
            for i, s in enumerate(self._shapes):
                s.selected = i == idx
            self._drag_index = idx
            self._drag_last_uv = uv if idx is not None else None
            self.selection_changed.emit()
            self.update()
            # Only consume when hitting a shape; miss → allow scrub/playback toggle.
            return idx is not None

        kind = tool_to_kind(self._tool)
        if kind is None:
            return False

        if kind == AnnotationKind.TEXT:
            host = parent_widget if parent_widget is not None else self
            text, ok = QInputDialog.getText(host, "Annotation Text", "Text:")
            if ok and text.strip():
                self._shapes.append(
                    AnnotationShape(
                        kind=AnnotationKind.TEXT,
                        points=[uv],
                        color=self._color,
                        thickness=self._thickness,
                        text=text.strip(),
                    )
                )
                self.shapes_changed.emit()
                self.update()
            return True

        self._draft = AnnotationShape(
            kind=kind,
            points=[uv, uv] if kind != AnnotationKind.FREEHAND else [uv],
            color=self._color,
            thickness=self._thickness,
        )
        self.update()
        return True

    def handle_move(self, x: float, y: float) -> bool:
        if not self._interactive or self._blocked():
            return False
        if self._draft is None and self._drag_index is None:
            return self.captures_left_drag()

        image_rect = self._image_rect()
        uv = widget_to_uv(x, y, image_rect)
        if uv is None:
            return True

        if self._tool == AnnotationTool.SELECT and self._drag_index is not None and self._drag_last_uv:
            du = uv[0] - self._drag_last_uv[0]
            dv = uv[1] - self._drag_last_uv[1]
            translate_shape(self._shapes[self._drag_index], du, dv)
            self._drag_last_uv = uv
            self.update()
            return True

        if self._draft is None:
            return self.captures_left_drag()

        if self._draft.kind == AnnotationKind.FREEHAND:
            last = self._draft.points[-1]
            if abs(last[0] - uv[0]) + abs(last[1] - uv[1]) > 0.001:
                self._draft.points.append(uv)
        else:
            if len(self._draft.points) < 2:
                self._draft.points.append(uv)
            else:
                self._draft.points[-1] = uv
        self.update()
        return True

    def handle_release(self, x: float, y: float) -> bool:
        if not self._interactive or self._blocked():
            return False

        if self._tool == AnnotationTool.SELECT:
            consumed = self._drag_index is not None
            if consumed:
                self.shapes_changed.emit()
            self._drag_index = None
            self._drag_last_uv = None
            return consumed

        if self._draft is None:
            return self.captures_left_drag()

        draft = self._draft
        self._draft = None
        if draft.kind == AnnotationKind.FREEHAND and len(draft.points) < 2:
            self.update()
            return True
        if draft.kind in (
            AnnotationKind.LINE,
            AnnotationKind.ARROW,
            AnnotationKind.RECT,
            AnnotationKind.ELLIPSE,
        ):
            if len(draft.points) < 2:
                self.update()
                return True
            a, b = draft.points[0], draft.points[1]
            if abs(a[0] - b[0]) + abs(a[1] - b[1]) < 0.002:
                self.update()
                return True
        self._shapes.append(draft)
        self.shapes_changed.emit()
        self.update()
        return True
