import numpy as np
from dataclasses import dataclass
from PySide6.QtWidgets import QRhiWidget, QWidget
from PySide6.QtCore import Qt, QPoint, Signal, QTimer, QByteArray
from PySide6.QtGui import QPainter, QColor, QFont, QPen, QRhiDepthStencilClearValue
from ..color.ocio_manager import OCIOManager
from ..core.tile_layout import TileLayout, compute_tile_layouts
from ..render.rhi_viewport_renderer import FrameRenderSlot, RhiViewportRenderer, TileDrawParams
from .fonts import mono_font, ui_font


COMPARE_SEQUENCE = 0
COMPARE_WIPE = 1
COMPARE_DIFFERENCE = 2
COMPARE_TILE = 3


@dataclass
class ViewportFrameSlot:
    data: np.ndarray | None = None
    upload_buffer: QByteArray | None = None
    upload_token: int = 0
    width: int = 0
    height: int = 0
    channels: int = 4
    pixel_aspect_ratio: float = 1.0
    timecode: str = "01:00:00:00"
    local_frame: int = 0


class ViewportHudOverlay(QWidget):
    """Transparent overlay sibling for HUD compositing.

    Must not be a child of QRhiWidget — that combination segfaults on macOS when
    the RHI backing texture is composited. Parent should be ViewportContainer.
    """

    def __init__(self, viewport: "Viewport", parent: QWidget):
        super().__init__(parent)
        self._viewport = viewport
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WA_NoSystemBackground)
        self.setAttribute(Qt.WA_TranslucentBackground)

    def paintEvent(self, event):
        viewport = self._viewport
        if not viewport.hud_visible and not viewport.adjustment_mode:
            return

        painter = QPainter(self)
        if viewport.hud_visible:
            viewport._draw_hud(painter)
        if viewport.adjustment_mode:
            viewport._draw_adjustment_overlay(painter)
        painter.end()


class ViewportContainer(QWidget):
    """Hosts the QRhi viewport and a transparent HUD overlay as siblings."""

    def __init__(self, ocio_manager: OCIOManager, main_window=None, parent=None):
        super().__init__(parent)
        self.viewport = Viewport(ocio_manager, main_window)
        self.viewport.setParent(self)
        self._hud_overlay = ViewportHudOverlay(self.viewport, self)
        self._hud_overlay.raise_()
        self._sync_geometry()

        viewport_update = self.viewport.update

        def update_viewport(*args, **kwargs):
            viewport_update(*args, **kwargs)
            self._hud_overlay.update()

        self.viewport.update = update_viewport

    def _sync_geometry(self):
        rect = self.rect()
        self.viewport.setGeometry(rect)
        self._hud_overlay.setGeometry(rect)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._sync_geometry()

    def update(self, *args, **kwargs):
        super().update(*args, **kwargs)
        self._hud_overlay.update()


class Viewport(QRhiWidget):
    wipe_changed = Signal(float)
    frame_scrubbed = Signal(int)
    zoom_mode_changed = Signal(object)

    def __init__(self, ocio_manager: OCIOManager, parent=None):
        super().__init__(parent)
        self.ocio_manager = ocio_manager
        self.main_window = parent

        self.native_renderer = RhiViewportRenderer()

        self.frame_slots: list[ViewportFrameSlot] = []
        self.sequence_index = 0
        self.source_labels: list[str] = []

        self.pixel_aspect_ratio = 1.0

        self.compare_mode = COMPARE_SEQUENCE
        self.channel_mask = 0
        self.wipe_pos = 0.5

        self.zoom = 1.0
        self.zoom_mode = "fit"
        self.pan_offset = QPoint(0, 0)
        self.last_mouse_pos = QPoint(0, 0)

        self.panning = False
        self.dragging_wipe = False
        self.scrubbing_frames = False

        self.adjustment_mode = None
        self.adjust_start_x = 0
        self.adjust_start_value = 0.0
        self.scrub_start_x = 0
        self.scrub_start_frame = 0
        self.scrub_sensitivity = 8.0
        self.left_press_pos = None
        self.left_drag_started = False
        self.click_threshold = 5
        self.setMouseTracking(True)

        self.hud_visible = True
        self.current_frame = 0
        self.current_timecode = "01:00:00:00"
        self.fps = 24.0
        self.resolution_str = "0x0"
        self.exr_layer_str = "RGB"

        self._renderer_initialized = False
        self._last_rhi = None
        self._ocio_pipeline_ready = False

    def set_source_count(self, count: int) -> None:
        while len(self.frame_slots) < count:
            self.frame_slots.append(ViewportFrameSlot())
        while len(self.frame_slots) > count:
            self.frame_slots.pop()
        self.native_renderer.ensure_texture_pool(count)

    def set_frame(
        self,
        index: int,
        data: np.ndarray | None,
        channels: list | None = None,
        *,
        local_frame: int = 0,
        timecode: str = "01:00:00:00",
        fps: float | None = None,
        upload_buffer: QByteArray | None = None,
        pixel_aspect_ratio: float | None = None,
        is_primary: bool = False,
    ):
        self.set_source_count(max(len(self.frame_slots), index + 1))
        slot = self.frame_slots[index]
        slot.data = data
        slot.upload_buffer = upload_buffer
        if data is not None:
            slot.upload_token += 1
            slot.height, slot.width = data.shape[:2]
            slot.channels = data.shape[2] if len(data.shape) > 2 else 1
        if pixel_aspect_ratio is not None:
            slot.pixel_aspect_ratio = pixel_aspect_ratio
        slot.local_frame = local_frame
        slot.timecode = timecode

        if is_primary:
            self.sequence_index = index
            if fps is not None:
                self.fps = fps
            self.current_frame = local_frame
            self.current_timecode = timecode
            if data is not None:
                self.resolution_str = f"{slot.width}x{slot.height}"
                self.pixel_aspect_ratio = slot.pixel_aspect_ratio
            self._apply_zoom_mode()

        self.update()

    def set_source_labels(self, labels: list[str]) -> None:
        self.source_labels = labels

    def set_sequence_index(self, index: int) -> None:
        self.sequence_index = index

    def set_pixel_aspect_ratio(self, par: float):
        if par <= 0.0:
            par = 1.0
        self.pixel_aspect_ratio = par
        if self.compare_mode != COMPARE_TILE and self.zoom_mode != "fit":
            self._apply_zoom_mode()
        self.update()

    def set_compare_mode(self, mode: int):
        if mode == COMPARE_TILE and self.compare_mode != COMPARE_TILE:
            self.zoom_mode = "fit"
            self.pan_offset = QPoint(0, 0)
            self.zoom = 1.0
            self.zoom_mode_changed.emit("fit")
        self.compare_mode = mode
        self.update()

    def set_channel_mask(self, mask: int):
        self.channel_mask = mask
        self.update()

    def toggle_hud(self):
        self.hud_visible = not self.hud_visible
        self.update()

    def _primary_slot(self) -> ViewportFrameSlot | None:
        if not self.frame_slots:
            return None
        if self.compare_mode == COMPARE_SEQUENCE:
            if 0 <= self.sequence_index < len(self.frame_slots):
                return self.frame_slots[self.sequence_index]
            return self.frame_slots[0]
        if self.compare_mode in (COMPARE_WIPE, COMPARE_DIFFERENCE):
            return self.frame_slots[0]
        return self.frame_slots[0]

    def _fit_scales(self):
        widget_w, widget_h = self.width(), self.height()
        if widget_w <= 0 or widget_h <= 0:
            return 1.0, 1.0

        if self.compare_mode == COMPARE_TILE:
            return 1.0, 1.0

        primary = self._primary_slot()
        if primary is None or primary.width <= 0 or primary.height <= 0:
            return 1.0, 1.0

        par = primary.pixel_aspect_ratio if primary.pixel_aspect_ratio > 0 else self.pixel_aspect_ratio
        aspect_widget = widget_w / widget_h
        aspect_frame = (primary.width * par) / primary.height
        scale_x = 1.0
        scale_y = 1.0
        if aspect_widget > aspect_frame:
            scale_x = aspect_frame / aspect_widget
        else:
            scale_y = aspect_widget / aspect_frame
        return scale_x, scale_y

    def _actual_size_zoom(self):
        scale_x, _ = self._fit_scales()
        widget_w = self.width()
        primary = self._primary_slot()
        if widget_w <= 0 or scale_x <= 0.0 or primary is None or primary.width <= 0:
            return 1.0
        par = primary.pixel_aspect_ratio if primary.pixel_aspect_ratio > 0 else self.pixel_aspect_ratio
        return (primary.width * par) / (widget_w * scale_x)

    def _apply_zoom_mode(self):
        if self.compare_mode == COMPARE_TILE:
            self.zoom = 1.0
            self.zoom_mode_changed.emit(self.zoom_mode)
            return
        if self.zoom_mode == "fit":
            self.zoom = 1.0
        elif isinstance(self.zoom_mode, int):
            self.zoom = self._actual_size_zoom() * (self.zoom_mode / 100.0)
        self.zoom_mode_changed.emit(self.zoom_mode)

    def fit_to_screen(self):
        self.zoom_mode = "fit"
        self.pan_offset = QPoint(0, 0)
        self._apply_zoom_mode()
        self.update()

    def set_zoom_percent(self, percent: int):
        if self.compare_mode == COMPARE_TILE:
            return
        self.zoom_mode = percent
        self.pan_offset = QPoint(0, 0)
        self._apply_zoom_mode()
        self.update()

    def reset_view(self):
        self.fit_to_screen()

    def _sync_grading_uniforms(self):
        self.native_renderer.clear_grading_uniforms()
        for name, value in self.ocio_manager.get_grading_uniform_values().items():
            if isinstance(value, tuple):
                self.native_renderer.set_grading_uniform_vec3(name, value[0], value[1], value[2])
            else:
                self.native_renderer.set_grading_uniform(name, float(value))

    def update_ocio_pipeline(self):
        bundle = self.ocio_manager.get_rhi_shader_bundle()
        self.native_renderer.clear_ocio_luts()
        self.native_renderer.set_shader_sources(
            bundle.pipeline_key,
            bundle.vertex_source,
            bundle.fragment_source,
        )
        for index, lut in enumerate(bundle.textures_3d):
            lut_data = np.array(lut["data"], dtype=np.float32)
            self.native_renderer.upload_ocio_lut_3d(index, lut["size"], lut_data)
        self._sync_grading_uniforms()
        self._ocio_pipeline_ready = True
        self.update()

    def _schedule_ocio_pipeline_update(self):
        if self.rhi() is None:
            return
        self.update_ocio_pipeline()

    def initialize(self, cb):
        if self.rhi() is None:
            return

        rhi = self.rhi()
        rhi_changed = self._last_rhi is not rhi
        self._last_rhi = rhi

        if rhi_changed:
            backend_name = rhi.backendName() if hasattr(rhi, "backendName") else str(rhi.backend())
            print(f"Viewport: QRhi backend = {backend_name}")

        self.native_renderer.initialize(rhi)
        self._renderer_initialized = True

        if rhi_changed:
            self._ocio_pipeline_ready = False
        if not self._ocio_pipeline_ready:
            QTimer.singleShot(0, self._schedule_ocio_pipeline_update)

    def _build_render_slots(self) -> list[FrameRenderSlot]:
        slots: list[FrameRenderSlot] = []
        for frame_slot in self.frame_slots:
            data = None
            if frame_slot.data is not None:
                data = (
                    frame_slot.data
                    if frame_slot.data.flags["C_CONTIGUOUS"]
                    else np.ascontiguousarray(frame_slot.data)
                )
            slots.append(
                FrameRenderSlot(
                    data=data,
                    width=frame_slot.width,
                    height=frame_slot.height,
                    channels=frame_slot.channels,
                    upload_token=frame_slot.upload_token,
                    upload_buffer=frame_slot.upload_buffer,
                    frame_index=frame_slot.local_frame,
                )
            )
        return slots

    def _build_tile_draws(self) -> list[TileDrawParams]:
        sizes = []
        aspects = []
        for slot in self.frame_slots:
            if slot.data is None or slot.width <= 0 or slot.height <= 0:
                sizes.append((0, 0))
                aspects.append(1.0)
            else:
                sizes.append((slot.width, slot.height))
                aspects.append(slot.pixel_aspect_ratio if slot.pixel_aspect_ratio > 0 else 1.0)

        layouts = compute_tile_layouts(sizes, aspects, self.width(), self.height())
        return [
            TileDrawParams(
                source_index=layout.source_index,
                scale_x=layout.scale_x,
                scale_y=layout.scale_y,
                offset_x=layout.offset_x,
                offset_y=layout.offset_y,
            )
            for layout in layouts
        ]

    def _has_visible_frame(self) -> bool:
        if self.compare_mode == COMPARE_TILE:
            return any(slot.data is not None for slot in self.frame_slots)
        primary = self._primary_slot()
        return primary is not None and primary.data is not None

    def render(self, cb):
        if self.rhi() is None:
            return

        if not self._has_visible_frame():
            batch = self.rhi().nextResourceUpdateBatch()
            cb.beginPass(
                self.renderTarget(),
                QColor(0, 0, 0),
                QRhiDepthStencilClearValue(1.0, 0),
                batch,
            )
            cb.endPass()
            return

        scale_x, scale_y = self._fit_scales()
        widget_w, widget_h = self.width(), self.height()
        pan_x = (self.pan_offset.x() / widget_w) * 2.0 if widget_w > 0 else 0.0
        pan_y = -(self.pan_offset.y() / widget_h) * 2.0 if widget_h > 0 else 0.0
        zoom = 1.0 if self.compare_mode == COMPARE_TILE else self.zoom

        tile_draws = self._build_tile_draws() if self.compare_mode == COMPARE_TILE else None

        self.native_renderer.render(
            cb,
            self.renderTarget(),
            self._build_render_slots(),
            self.compare_mode,
            self.sequence_index,
            self.wipe_pos,
            self.channel_mask,
            scale_x * zoom,
            scale_y * zoom,
            pan_x,
            pan_y,
            tile_draws=tile_draws,
        )

    def _wipe_label(self) -> str:
        if len(self.source_labels) >= 2:
            return f"{self.source_labels[0]} | {self.source_labels[1]}"
        return "1 | 2"

    def _draw_hud(self, painter: QPainter):
        painter.setRenderHint(QPainter.Antialiasing)
        metrics_rect = self.rect()

        if self.compare_mode == COMPARE_WIPE:
            wipe_x = int(self.wipe_pos * metrics_rect.width())
            painter.setPen(QPen(QColor(255, 165, 0, 180), 2, Qt.DashLine))
            painter.drawLine(wipe_x, 0, wipe_x, metrics_rect.height())
            painter.setPen(QPen(QColor(255, 165, 0, 220), 1))
            font = mono_font(10, QFont.Weight.Bold)
            painter.setFont(font)
            painter.drawText(wipe_x + 5, metrics_rect.height() // 2, self._wipe_label())

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.zoom_mode != "fit" and self.compare_mode != COMPARE_TILE:
            self._apply_zoom_mode()
            self.update()

    def mousePressEvent(self, event):
        if self.adjustment_mode and event.button() == Qt.LeftButton:
            self.adjust_start_x = event.position().x()
            self.adjust_start_value = (
                self.ocio_manager.grade_exposure
                if self.adjustment_mode == "exposure"
                else self.ocio_manager.grade_gamma
                if self.adjustment_mode == "gamma"
                else self.ocio_manager.grade_offset
            )
            self.setCursor(Qt.SizeHorCursor)
            return

        if event.button() == Qt.LeftButton:
            if self.compare_mode == COMPARE_WIPE:
                click_x_ratio = event.position().x() / self.width()
                if abs(click_x_ratio - self.wipe_pos) < 0.02:
                    self.dragging_wipe = True
                    self.setCursor(Qt.SizeHorCursor)
                    return
            self.left_press_pos = event.position()
            self.left_drag_started = False
            self.scrub_start_x = event.position().x()
            if self.main_window:
                self.scrub_start_frame = self.main_window.current_frame
            else:
                self.scrub_start_frame = self.current_frame
        elif event.button() == Qt.MiddleButton:
            self.panning = True
            self.last_mouse_pos = event.position().toPoint()
            self.setCursor(Qt.ClosedHandCursor)

    def mouseMoveEvent(self, event):
        if self.adjustment_mode and (event.buttons() & Qt.LeftButton):
            delta_x = event.position().x() - self.adjust_start_x

            if self.adjustment_mode == "exposure":
                new_val = self.adjust_start_value + delta_x * 0.02
                new_val = max(-5.0, min(5.0, new_val))
                self.ocio_manager.set_grading_values(exposure=new_val)
            elif self.adjustment_mode == "gamma":
                new_val = self.adjust_start_value + delta_x * 0.003
                new_val = max(0.1, min(5.0, new_val))
                self.ocio_manager.set_grading_values(gamma=new_val)
            elif self.adjustment_mode == "offset":
                new_val = self.adjust_start_value + delta_x * 0.002
                new_val = max(-0.5, min(0.5, new_val))
                self.ocio_manager.set_grading_values(offset=new_val)

            self._sync_grading_uniforms()
            if self.main_window:
                self.main_window.statusBar().showMessage(
                    f"Adjusting {self.adjustment_mode.upper()}: {new_val:.2f}"
                )
                self.main_window._update_ocio_info_label()
            self.update()
            return

        if self.dragging_wipe:
            self.wipe_pos = max(0.0, min(1.0, event.position().x() / self.width()))
            self.wipe_changed.emit(self.wipe_pos)
            self.update()
        elif self.left_press_pos is not None and (event.buttons() & Qt.LeftButton):
            delta_x = abs(event.position().x() - self.left_press_pos.x())
            delta_y = abs(event.position().y() - self.left_press_pos.y())
            if not self.left_drag_started and (
                delta_x > self.click_threshold or delta_y > self.click_threshold
            ):
                self.left_drag_started = True
                self.scrubbing_frames = True
                if self.main_window and self.main_window.playing:
                    self.main_window.stop_playback()
                self.setCursor(Qt.SizeHorCursor)
            if self.scrubbing_frames:
                frame_offset = int((event.position().x() - self.scrub_start_x) / self.scrub_sensitivity)
                target_frame = self.scrub_start_frame + frame_offset
                self.frame_scrubbed.emit(target_frame)
        elif self.panning:
            if self.compare_mode == COMPARE_TILE:
                return
            delta = event.position().toPoint() - self.last_mouse_pos
            self.pan_offset += delta
            self.last_mouse_pos = event.position().toPoint()
            self.update()
        elif self.compare_mode == COMPARE_WIPE:
            hover_x_ratio = event.position().x() / self.width()
            if abs(hover_x_ratio - self.wipe_pos) < 0.02:
                self.setCursor(Qt.SizeHorCursor)
            else:
                self.setCursor(Qt.ArrowCursor)

    def mouseReleaseEvent(self, event):
        if self.adjustment_mode:
            self.adjustment_mode = None
            self.setCursor(Qt.ArrowCursor)
            if self.main_window:
                self.main_window.statusBar().showMessage("Interactive adjustment finished.")
                self.main_window._update_ocio_info_label()
            self.update()
            return

        if event.button() == Qt.LeftButton and self.left_press_pos is not None:
            if self.scrubbing_frames and self.main_window:
                frame_offset = int(
                    (event.position().x() - self.scrub_start_x) / self.scrub_sensitivity
                )
                target_frame = self.scrub_start_frame + frame_offset
                self.frame_scrubbed.emit(target_frame)
            elif not self.left_drag_started and not self.dragging_wipe and self.main_window:
                self.main_window.toggle_playback()

        self.dragging_wipe = False
        self.panning = False
        self.scrubbing_frames = False
        self.left_press_pos = None
        self.left_drag_started = False
        self.setCursor(Qt.ArrowCursor)

    def wheelEvent(self, event):
        if self.compare_mode == COMPARE_TILE:
            return
        zoom_factor = 1.1 if event.angleDelta().y() > 0 else 0.9
        self.zoom = max(0.1, min(20.0, self.zoom * zoom_factor))
        if self.zoom_mode is not None:
            self.zoom_mode = None
            self.zoom_mode_changed.emit(None)
        self.update()

    def cleanup(self):
        self.native_renderer.cleanup()
        self._renderer_initialized = False
        self._last_rhi = None
        self._ocio_pipeline_ready = False

    def clear_frames(self):
        self.frame_slots.clear()
        self.source_labels.clear()
        self.sequence_index = 0
        self.pixel_aspect_ratio = 1.0
        self.native_renderer.reset_frame_textures()
        self.update()

    def _draw_adjustment_overlay(self, painter: QPainter):
        painter.setRenderHint(QPainter.Antialiasing)
        rect = self.rect()

        if self.adjustment_mode == "exposure":
            val = self.ocio_manager.grade_exposure
            val_str = f"{val:+.2f}"
            unit = " stops"
        elif self.adjustment_mode == "gamma":
            val = self.ocio_manager.grade_gamma
            val_str = f"{val:.2f}"
            unit = ""
        else:
            val = self.ocio_manager.grade_offset
            val_str = f"{val:+.3f}"
            unit = ""

        text = f"{self.adjustment_mode.upper()}: {val_str}{unit}"

        box_w = 250
        box_h = 36
        box_x = 20
        box_y = rect.height() - box_h - 20

        painter.setBrush(QColor(0, 0, 0, 180))
        painter.setPen(Qt.NoPen)
        painter.drawRoundedRect(box_x, box_y, box_w, box_h, 6.0, 6.0)

        painter.setPen(QColor(255, 255, 255))
        font = ui_font(13, QFont.Weight.Bold)
        painter.setFont(font)
        painter.drawText(box_x, box_y, box_w, box_h, Qt.AlignCenter, text)
