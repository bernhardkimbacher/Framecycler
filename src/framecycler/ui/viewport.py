import sys
import numpy as np
from dataclasses import dataclass
import shiboken6
from PySide6.QtWidgets import QWidget, QVBoxLayout
from PySide6.QtCore import Qt, QPoint, QRect, QRectF, Signal, QTimer, QByteArray, QEvent
from PySide6.QtGui import QPainter, QColor, QFont, QPen, QWindow, QExposeEvent, QResizeEvent
from ..color.ocio_manager import OCIOManager
from ..core.tile_layout import TileLayout, compute_tile_layouts
from .fonts import mono_font, ui_font
from .drag_drop_overlay import DragDropOverlay
from .overlay_geometry import aspect_mask_rect, displayed_image_rect, safe_inset_rect
from .translucent_window import (
    FLOATING_OVERLAY_FLAGS,
    configure_floating_overlay,
    paint_floating_overlay,
)

try:
    from .. import framecycler_engine
except ImportError:
    import framecycler_engine

COMPARE_SEQUENCE = 0
COMPARE_WIPE = 1
COMPARE_DIFFERENCE = 2
COMPARE_TILE = 3
COMPARE_BLEND = 4

FALSE_COLOR_OFF = 0
FALSE_COLOR_HEATMAP = 1
FALSE_COLOR_ZEBRA = 2


@dataclass
class TileDrawParams:
    source_index: int
    scale_x: float
    scale_y: float
    offset_x: float
    offset_y: float


class RhiViewportWindow(QWindow):
    def __init__(self, parent=None):
        super().__init__(parent)
        if sys.platform == "darwin":
            self.setSurfaceType(QWindow.MetalSurface)
        elif sys.platform == "win32":
            # D3D11 QRhi present (incl. HDR) needs a raster surface, not OpenGL.
            self.setSurfaceType(QWindow.RasterSurface)
        else:
            self.setSurfaceType(QWindow.VulkanSurface)
        self._renderer = None
        self.viewport_widget = None

    def mousePressEvent(self, event):
        self.setMouseGrabEnabled(True)
        if self.viewport_widget:
            self.viewport_widget.mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self.viewport_widget:
            self.viewport_widget.mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self.setMouseGrabEnabled(False)
        if self.viewport_widget:
            self.viewport_widget.mouseReleaseEvent(event)

    def wheelEvent(self, event):
        if self.viewport_widget:
            self.viewport_widget.wheelEvent(event)

    def keyPressEvent(self, event):
        if self.viewport_widget:
            self.viewport_widget.keyPressEvent(event)

    def keyReleaseEvent(self, event):
        if self.viewport_widget:
            self.viewport_widget.keyReleaseEvent(event)

    def set_renderer(self, renderer):
        self._renderer = renderer

    def exposeEvent(self, event: QExposeEvent):
        if self._renderer:
            exposed = self.isExposed()
            self._renderer.set_exposed(exposed)

    def resizeEvent(self, event: QResizeEvent):
        if self._renderer:
            sz = event.size()
            # Swapchain resize; aspect-fit is recomputed on the render thread
            # from the live swapchain size each present.
            self._renderer.set_pending_size(sz.width(), sz.height())


@dataclass
class ViewportFrameSlot:
    width: int = 0
    height: int = 0
    channels: int = 4
    pixel_aspect_ratio: float = 1.0
    timecode: str = "01:00:00:00"
    local_frame: int = 0
    decoder_frame: int = 0
    upload_token: int = 0
    cached: bool = False


class ViewportHudOverlay(QWidget):
    """Transparent HUD compositing layer above the native QRhi surface.

    Must not be a child of the QWindow container — that combination segfaults on
    macOS. Must also not be a sibling QWidget under the same parent as
    ``createWindowContainer``: on Metal/D3D the native surface stacks above
    Qt siblings, so the HUD would paint once then vanish after the first
    real frames. Use a floating Tool window parented to the main window
    (same pattern as DragDropOverlay, without WindowStaysOnTopHint).
    """

    def __init__(self, viewport: "Viewport", parent: QWidget, *, floating: bool = False):
        if floating:
            super().__init__(parent, FLOATING_OVERLAY_FLAGS)
            configure_floating_overlay(self)
            self.setAttribute(Qt.WA_TransparentForMouseEvents)
            self.setWindowFlag(Qt.WindowType.WindowTransparentForInput, True)
        else:
            super().__init__(parent)
            self.setAttribute(Qt.WA_TransparentForMouseEvents)
            self.setAttribute(Qt.WA_NoSystemBackground)
            self.setAttribute(Qt.WA_TranslucentBackground)
        self._viewport = viewport
        self._package_painters_provider = None
        self._frame_provider = None
        self._floating = floating

    def set_package_hud_hooks(self, painters_provider, frame_provider) -> None:
        """Optional hooks: painters_provider() -> list[HudPainterSpec], frame_provider() -> int."""
        self._package_painters_provider = painters_provider
        self._frame_provider = frame_provider

    def paintEvent(self, event):
        viewport = self._viewport

        def _paint(painter: QPainter) -> None:
            need_paint = (
                viewport.hud_visible
                or viewport.adjustment_mode
                or viewport.has_review_overlays()
            )
            if not need_paint:
                return
            if viewport.hud_visible:
                viewport._draw_hud(painter)
                if self._package_painters_provider is not None:
                    frame = 0
                    if self._frame_provider is not None:
                        try:
                            frame = int(self._frame_provider())
                        except Exception:
                            frame = 0
                    rect = self.rect()
                    for spec in self._package_painters_provider():
                        try:
                            spec.paint(painter, rect, frame)
                        except Exception:
                            pass
            viewport._draw_review_overlays(painter)
            if viewport.adjustment_mode:
                viewport._draw_adjustment_overlay(painter)

        if self._floating:
            paint_floating_overlay(self, _paint)
        else:
            painter = QPainter(self)
            _paint(painter)
            painter.end()


class ViewportContainer(QWidget):
    """Hosts the QRhi viewport; HUD and drag overlays are floating windows."""

    def __init__(self, ocio_manager: OCIOManager, main_window=None, parent=None):
        super().__init__(parent)
        self._main_window = main_window
        self._drag_enter_count = 0
        self._drag_drop_zone = DragDropOverlay.ZONE_SEQUENCE
        self.setAcceptDrops(True)
        self.viewport = Viewport(ocio_manager, main_window, self)
        # Parent HUD to the main window so it stacks above the Metal/D3D surface.
        hud_parent = main_window if main_window is not None else self
        self._hud_overlay = ViewportHudOverlay(
            self.viewport,
            hud_parent,
            floating=main_window is not None,
        )
        from .annotations.overlay import AnnotationOverlay

        self._annotation_overlay = AnnotationOverlay(
            self.viewport,
            hud_parent,
            floating=main_window is not None,
        )
        self._annotation_overlay.set_blocked_provider(
            lambda: bool(getattr(self.viewport, "dragging_wipe", False))
        )
        self._drag_overlay = DragDropOverlay(main_window, main_window=main_window, floating=True)
        self._sync_geometry()

        viewport_update = self.viewport.update

        def update_viewport(*args, **kwargs):
            viewport_update(*args, **kwargs)
            self._hud_overlay.update()
            self._annotation_overlay.update()

        self.viewport.update = update_viewport

        if main_window is not None:
            main_window.installEventFilter(self)

    def eventFilter(self, obj, event):
        main = getattr(self, "_main_window", None)
        if (
            main is not None
            and obj is main
            and hasattr(self, "_hud_overlay")
            and event.type()
            in (
                QEvent.Type.Move,
                QEvent.Type.Resize,
                QEvent.Type.WindowStateChange,
            )
        ):
            self._position_hud_overlay()
            self._position_annotation_overlay()
        return super().eventFilter(obj, event)

    def _sync_geometry(self):
        rect = self.rect()
        self.viewport.setGeometry(rect)
        self._position_hud_overlay()
        self._position_annotation_overlay()
        self._position_drag_overlay()
        # Viewport.resizeEvent (from setGeometry) refreshes zoom/pan/tiles;
        # aspect-fit is computed on the render thread from the live swapchain.

    def _position_hud_overlay(self):
        if self._hud_overlay._floating:
            top_left = self.mapToGlobal(QPoint(0, 0))
            self._hud_overlay.setGeometry(QRect(top_left, self.size()))
        else:
            self._hud_overlay.setGeometry(self.rect())

    def _position_annotation_overlay(self):
        overlay = getattr(self, "_annotation_overlay", None)
        if overlay is None:
            return
        if overlay._floating:
            top_left = self.mapToGlobal(QPoint(0, 0))
            overlay.setGeometry(QRect(top_left, self.size()))
        else:
            overlay.setGeometry(self.rect())

    def _position_drag_overlay(self):
        if self._main_window is None:
            return
        top_left = self.mapToGlobal(QPoint(0, 0))
        self._drag_overlay.setGeometry(QRect(top_left, self.size()))
        self._drag_overlay.set_split_x(None)

    def _show_drag_overlay(self) -> None:
        if self._main_window is None:
            return
        self._position_drag_overlay()
        self._drag_overlay.show()
        self._drag_overlay.raise_()

    def _hide_drag_overlay(self) -> None:
        self._drag_overlay.hide()

    def _update_drag_zone(self, viewer_pos: QPoint) -> None:
        w = self.width()
        if w <= 0:
            zone = DragDropOverlay.ZONE_SEQUENCE
        else:
            third = w / 3.0
            x = viewer_pos.x()
            if x < third:
                zone = DragDropOverlay.ZONE_REPLACE
            elif x < 2.0 * third:
                zone = DragDropOverlay.ZONE_SEQUENCE
            else:
                zone = DragDropOverlay.ZONE_STACK
        self._drag_drop_zone = zone
        self._drag_overlay.set_active_zone(zone)

    def dragEnterEvent(self, event):
        if self._main_window is None or not event.mimeData().hasUrls():
            event.ignore()
            return
        self._drag_enter_count += 1
        self._show_drag_overlay()
        event.acceptProposedAction()

    def dragMoveEvent(self, event):
        if self._main_window is None or not event.mimeData().hasUrls():
            event.ignore()
            return
        self._update_drag_zone(event.position().toPoint())
        event.acceptProposedAction()

    def dragLeaveEvent(self, event):
        if self._main_window is None:
            event.ignore()
            return
        self._drag_enter_count = max(0, self._drag_enter_count - 1)
        if self._drag_enter_count == 0:
            self._hide_drag_overlay()
        event.accept()

    def dropEvent(self, event):
        if self._main_window is None or not event.mimeData().hasUrls():
            event.ignore()
            return
        self._drag_enter_count = 0
        self._hide_drag_overlay()
        paths = [url.toLocalFile() for url in event.mimeData().urls() if url.toLocalFile()]
        if paths:
            mode = self._drag_drop_zone or DragDropOverlay.ZONE_SEQUENCE
            self._main_window._add_media(paths, mode=mode)
        event.acceptProposedAction()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._sync_geometry()

    def showEvent(self, event):
        super().showEvent(event)
        self._position_hud_overlay()
        self._position_annotation_overlay()
        annot = getattr(self, "_annotation_overlay", None)
        if annot is not None and annot._floating:
            annot.show()
            annot.raise_()
        if self._hud_overlay._floating:
            self._hud_overlay.show()
            self._hud_overlay.raise_()

    def hideEvent(self, event):
        if self._hud_overlay._floating:
            self._hud_overlay.hide()
        annot = getattr(self, "_annotation_overlay", None)
        if annot is not None and annot._floating:
            annot.hide()
        super().hideEvent(event)

    def update(self, *args, **kwargs):
        super().update(*args, **kwargs)
        self._hud_overlay.update()
        annot = getattr(self, "_annotation_overlay", None)
        if annot is not None:
            annot.update()

    def annotation_overlay(self):
        return getattr(self, "_annotation_overlay", None)


class Viewport(QWidget):
    wipe_changed = Signal(float)
    frame_scrubbed = Signal(int)
    scrub_started = Signal()
    scrub_finished = Signal()
    zoom_mode_changed = Signal(object)

    def __init__(self, ocio_manager: OCIOManager, parent=None, container=None):
        super().__init__(container)
        self.ocio_manager = ocio_manager
        self.main_window = parent
        self.viewport_container = container

        # Setup native window and C++ renderer
        self.viewport_window = RhiViewportWindow()
        self.viewport_window.viewport_widget = self
        self.native_renderer = framecycler_engine.RhiRenderer()
        
        ptr = shiboken6.getCppPointer(self.viewport_window)[0]
        self.native_renderer.initialize(int(ptr))
        self.viewport_window.set_renderer(self.native_renderer)

        # Setup container layout
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.container = QWidget.createWindowContainer(self.viewport_window, self)
        layout.addWidget(self.container)

        self.viewport_window.installEventFilter(self)
        self.setFocusProxy(self.container)

        self.frame_slots: list[ViewportFrameSlot] = []
        self.sequence_index = 0
        self.source_labels: list[str] = []

        self.pixel_aspect_ratio = 1.0

        self.compare_mode = COMPARE_SEQUENCE
        self.channel_mask = 0
        self.wipe_pos = 0.5
        self.false_color_mode = FALSE_COLOR_OFF
        self.zebra_lo = 0.02
        self.zebra_hi = 0.98

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

        self._renderer_initialized = True
        self._ocio_pipeline_ready = False

    def eventFilter(self, obj, event):
        viewport_window = getattr(self, "viewport_window", None)
        if viewport_window is not None and obj is viewport_window:
            et = event.type()
            if et in (QEvent.DragEnter, QEvent.DragMove, QEvent.DragLeave, QEvent.Drop):
                return self._handle_viewport_drag(et, event)
        return super().eventFilter(obj, event)

    def _handle_viewport_drag(self, et, event):
        container = self.viewport_container
        target = self.main_window
        if container is None or target is None:
            return False
        if et == QEvent.DragEnter and event.mimeData().hasUrls():
            container._drag_enter_count += 1
            container._show_drag_overlay()
            event.acceptProposedAction()
            return True
        if et == QEvent.DragMove and event.mimeData().hasUrls():
            pos = container.mapFromGlobal(
                self.viewport_window.mapToGlobal(event.position().toPoint())
            )
            container._update_drag_zone(pos)
            event.acceptProposedAction()
            return True
        if et == QEvent.DragLeave:
            container._drag_enter_count = max(0, container._drag_enter_count - 1)
            if container._drag_enter_count == 0:
                container._hide_drag_overlay()
            event.accept()
            return True
        if et == QEvent.Drop:
            container._drag_enter_count = 0
            container._hide_drag_overlay()
            paths = [url.toLocalFile() for url in event.mimeData().urls() if url.toLocalFile()]
            if paths:
                mode = container._drag_drop_zone or DragDropOverlay.ZONE_SEQUENCE
                target._add_media(paths, mode=mode)
            event.acceptProposedAction()
            return True
        return False

    def set_source_count(self, count: int) -> None:
        while len(self.frame_slots) < count:
            self.frame_slots.append(ViewportFrameSlot())
        while len(self.frame_slots) > count:
            self.frame_slots.pop()

    def set_frame(
        self,
        index: int,
        width: int,
        height: int,
        channels: int,
        *,
        local_frame: int = 0,
        decoder_frame: int | None = None,
        timecode: str = "01:00:00:00",
        fps: float | None = None,
        pixel_aspect_ratio: float | None = None,
        is_primary: bool = False,
        upload_token: int = 0,
        cached: bool = True,
    ):
        self.set_source_count(max(len(self.frame_slots), index + 1))
        slot = self.frame_slots[index]
        slot.width = width
        slot.height = height
        slot.channels = channels
        if pixel_aspect_ratio is not None:
            slot.pixel_aspect_ratio = pixel_aspect_ratio
        slot.local_frame = local_frame
        slot.decoder_frame = decoder_frame if decoder_frame is not None else upload_token
        slot.timecode = timecode
        slot.upload_token = upload_token
        slot.cached = cached

        if is_primary:
            self.sequence_index = index
            if fps is not None:
                self.fps = fps
            self.current_frame = local_frame
            self.current_timecode = timecode
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

    def set_false_color_mode(self, mode: int):
        self.false_color_mode = int(mode)
        self.update()

    def set_zebra_thresholds(self, lo: float, hi: float):
        self.zebra_lo = max(0.0, min(1.0, float(lo)))
        self.zebra_hi = max(self.zebra_lo, min(1.0, float(hi)))
        self.update()

    def toggle_hud(self):
        """Flip HUD visibility; keep View menu checkmark in sync when present."""
        self.hud_visible = not self.hud_visible
        mw = self.main_window
        act = getattr(mw, "act_hud", None) if mw is not None else None
        if act is not None and act.isChecked() != self.hud_visible:
            act.blockSignals(True)
            act.setChecked(self.hud_visible)
            act.blockSignals(False)
        self.update()

    def set_hud_visible(self, visible: bool) -> None:
        self.hud_visible = bool(visible)
        self.update()

    def _primary_slot(self) -> ViewportFrameSlot | None:
        if not self.frame_slots:
            return None
        if self.compare_mode == COMPARE_SEQUENCE:
            if 0 <= self.sequence_index < len(self.frame_slots):
                return self.frame_slots[self.sequence_index]
            return self.frame_slots[0]
        if self.compare_mode in (COMPARE_WIPE, COMPARE_DIFFERENCE, COMPARE_BLEND):
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
        last_key = getattr(self, "_last_ocio_pipeline_key", None)
        if last_key == bundle.pipeline_key and self._ocio_pipeline_ready:
            # Same OCIO graph — uniforms only (skip clear/bake/LUT reupload).
            self._sync_grading_uniforms()
            self.native_renderer.request_redraw()
            self.update()
            return

        self.native_renderer.clear_ocio_luts()
        slot_dims = [dim for _binding, dim, _name in bundle.sampler_bindings]
        if hasattr(self.native_renderer, "set_ocio_lut_slot_dims"):
            self.native_renderer.set_ocio_lut_slot_dims(slot_dims)

        self.native_renderer.set_shader_sources(
            bundle.pipeline_key,
            bundle.vertex_source,
            bundle.fragment_source,
        )

        # Map sampler name → texture payload for binding-order upload.
        by_sampler: dict[str, tuple[str, dict]] = {}
        for lut in bundle.textures_1d:
            by_sampler[lut["sampler"]] = ("2D", lut)
        for lut in bundle.textures_3d:
            by_sampler[lut["sampler"]] = ("3D", lut)

        for slot_index, (_binding, dim, name) in enumerate(bundle.sampler_bindings):
            entry = by_sampler.get(name)
            if entry is None:
                continue
            kind, lut = entry
            if kind == "3D" or dim == "3D":
                lut_data = np.array(lut["data"], dtype=np.float32)
                self.native_renderer.upload_ocio_lut_3d(
                    slot_index, lut["size"], lut_data.flatten().tolist()
                )
            else:
                lut_data = np.asarray(lut["data"], dtype=np.float32).flatten()
                channel = str(lut.get("channel", "")).upper()
                if "RGB" in channel and "A" not in channel.replace("RGB", ""):
                    channels = 3
                elif "RED" in channel or channel in ("R", "TEXTURE_RED_CHANNEL"):
                    channels = 1
                else:
                    # Infer from data length
                    pixels = int(lut["width"]) * int(lut["height"])
                    channels = max(1, int(lut_data.size // max(pixels, 1)))
                    channels = min(channels, 4)
                if hasattr(self.native_renderer, "upload_ocio_lut_2d"):
                    self.native_renderer.upload_ocio_lut_2d(
                        slot_index,
                        int(lut["width"]),
                        int(lut["height"]),
                        channels,
                        lut_data.tolist(),
                    )

        self._sync_grading_uniforms()
        self._last_ocio_pipeline_key = bundle.pipeline_key
        self._ocio_pipeline_ready = True
        self.native_renderer.request_redraw()
        self.update()

    def showEvent(self, event):
        super().showEvent(event)
        if not self._ocio_pipeline_ready:
            QTimer.singleShot(0, self.update_ocio_pipeline)
        elif self.viewport_window.isExposed():
            self.native_renderer.set_exposed(True)
            self.native_renderer.sync_and_render()

    def _build_tile_draws(self) -> list[TileDrawParams]:
        sizes = []
        aspects = []
        for slot in self.frame_slots:
            if not slot.cached or slot.width <= 0 or slot.height <= 0:
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
            return any(slot.cached for slot in self.frame_slots)
        primary = self._primary_slot()
        return primary is not None and primary.cached

    def _sync_display_cache_playheads(self) -> None:
        main_window = getattr(self, "main_window", None)
        if main_window is None:
            return
        session = getattr(main_window, "session", None)
        if session is None or session.empty:
            return
        plan = session.plan
        segment = plan.segment_at(main_window.current_frame)
        if segment is None:
            return
        direction = main_window.playback_direction if main_window.playing else 0
        versions = segment.display_versions()
        for index, version in enumerate(versions):
            if version.source is None or version.source.cache is None or version.offline:
                continue
            if index < len(self.frame_slots) and self.frame_slots[index].cached:
                decoder_frame = self.frame_slots[index].decoder_frame
            else:
                decoder_frame = plan.decoder_frame_for_version(
                    segment, version, main_window.current_frame
                )
            local_in, local_out = plan.playback_range_for_version(
                segment, version, main_window.in_point, main_window.out_point
            )
            self.native_renderer.set_source_playhead(
                index, decoder_frame, direction, local_in, local_out
            )

    def update(self, *args, **kwargs):
        if hasattr(self, "native_renderer") and self.native_renderer is not None:
            self._sync_display_cache_playheads()
            params = framecycler_engine.RenderParams()
            params.compare_mode = self.compare_mode
            params.sequence_index = self.sequence_index
            params.wipe_pos = self.wipe_pos
            params.channel_mask = self.channel_mask
            params.false_color_mode = self.false_color_mode
            params.zebra_lo = self.zebra_lo
            params.zebra_hi = self.zebra_hi

            widget_w, widget_h = self.width(), self.height()
            pan_x = (self.pan_offset.x() / widget_w) * 2.0 if widget_w > 0 else 0.0
            pan_y = -(self.pan_offset.y() / widget_h) * 2.0 if widget_h > 0 else 0.0
            # Aspect-fit is computed on the render thread from live swapchain size.
            params.zoom = 1.0 if self.compare_mode == COMPARE_TILE else float(self.zoom)
            primary = self._primary_slot()
            if primary is not None and primary.pixel_aspect_ratio > 0:
                params.pixel_aspect_ratio = float(primary.pixel_aspect_ratio)
            else:
                params.pixel_aspect_ratio = float(self.pixel_aspect_ratio or 1.0)
            params.pan_x = pan_x
            params.pan_y = pan_y

            # Build slots
            slots = []
            for source_idx, slot in enumerate(self.frame_slots):
                if slot.cached:
                    spec = framecycler_engine.FrameSlotSpec()
                    spec.source_index = source_idx
                    spec.frame_index = slot.decoder_frame
                    spec.upload_token = slot.upload_token
                    slots.append(spec)
            params.slots = slots

            # Build tiles
            if self.compare_mode == COMPARE_TILE:
                tiles = []
                for tile_draw in self._build_tile_draws():
                    t = framecycler_engine.TileSpec()
                    t.source_index = tile_draw.source_index
                    t.scale_x = tile_draw.scale_x
                    t.scale_y = tile_draw.scale_y
                    t.offset_x = tile_draw.offset_x
                    t.offset_y = tile_draw.offset_y
                    tiles.append(t)
                params.tiles = tiles

            self.native_renderer.update_render_params(params)
            self.native_renderer.sync_and_render()
            if hasattr(self, "viewport_window") and self.viewport_window is not None:
                self.viewport_window.requestUpdate()

        super().update(*args, **kwargs)

    def _wipe_label(self) -> str:
        if len(self.source_labels) >= 2:
            return f"{self.source_labels[0]} | {self.source_labels[1]}"
        return "1 | 2"

    def has_review_overlays(self) -> bool:
        settings = getattr(self.main_window, "settings", None) if self.main_window else None
        if settings is None:
            return False
        return bool(
            settings.overlay_mask_aspect > 0.0
            or settings.overlay_action_safe > 0.0
            or settings.overlay_title_safe > 0.0
        )

    def _image_rect_for_overlays(self) -> QRect:
        scale_x, scale_y = self._fit_scales()
        zoom = 1.0 if self.compare_mode == COMPARE_TILE else self.zoom
        rectf = displayed_image_rect(
            float(self.width()),
            float(self.height()),
            scale_x,
            scale_y,
            zoom,
            float(self.pan_offset.x()),
            float(self.pan_offset.y()),
        )
        return rectf.toRect()

    def _draw_review_overlays(self, painter: QPainter) -> None:
        if not self.has_review_overlays():
            return
        settings = self.main_window.settings
        image_rect = self._image_rect_for_overlays()
        if image_rect.isEmpty():
            return

        painter.setRenderHint(QPainter.Antialiasing)

        if settings.overlay_mask_aspect > 0.0:
            mask = aspect_mask_rect(QRectF(image_rect), settings.overlay_mask_aspect).toRect()
            opacity = max(0.0, min(1.0, float(settings.overlay_mask_opacity)))
            dim = QColor(0, 0, 0, int(round(255 * opacity)))
            # Dim regions of the image outside the aspect mask.
            if mask.top() > image_rect.top():
                painter.fillRect(
                    QRect(image_rect.left(), image_rect.top(), image_rect.width(), mask.top() - image_rect.top()),
                    dim,
                )
            if mask.bottom() < image_rect.bottom():
                painter.fillRect(
                    QRect(
                        image_rect.left(),
                        mask.bottom() + 1,
                        image_rect.width(),
                        image_rect.bottom() - mask.bottom(),
                    ),
                    dim,
                )
            if mask.left() > image_rect.left():
                painter.fillRect(
                    QRect(image_rect.left(), mask.top(), mask.left() - image_rect.left(), mask.height()),
                    dim,
                )
            if mask.right() < image_rect.right():
                painter.fillRect(
                    QRect(
                        mask.right() + 1,
                        mask.top(),
                        image_rect.right() - mask.right(),
                        mask.height(),
                    ),
                    dim,
                )

        if settings.overlay_action_safe > 0.0:
            action = safe_inset_rect(QRectF(image_rect), settings.overlay_action_safe).toRect()
            painter.setPen(QPen(QColor(0, 220, 80, 200), 1, Qt.SolidLine))
            painter.setBrush(Qt.NoBrush)
            painter.drawRect(action)

        if settings.overlay_title_safe > 0.0:
            title = safe_inset_rect(QRectF(image_rect), settings.overlay_title_safe).toRect()
            painter.setPen(QPen(QColor(80, 180, 255, 200), 1, Qt.DashLine))
            painter.setBrush(Qt.NoBrush)
            painter.drawRect(title)

    def _draw_hud(self, painter: QPainter):
        painter.setRenderHint(QPainter.Antialiasing)
        metrics_rect = self.rect()

        # Always-visible frame readout so Toggle HUD is obvious outside wipe mode.
        mw = self.main_window
        frame = int(getattr(mw, "current_frame", self.current_frame) if mw is not None else self.current_frame)
        tc = self.current_timecode or "—"
        label = f"FR {frame}   TC {tc}"
        if self.resolution_str and self.resolution_str != "0x0":
            label = f"{label}   {self.resolution_str}"

        font = mono_font(11, QFont.Weight.Bold)
        painter.setFont(font)
        fm = painter.fontMetrics()
        pad_x, pad_y = 8, 5
        text_w = fm.horizontalAdvance(label)
        text_h = fm.height()
        box = QRect(8, 8, text_w + pad_x * 2, text_h + pad_y * 2)
        painter.fillRect(box, QColor(0, 0, 0, 160))
        painter.setPen(QPen(QColor(0, 255, 120)))
        painter.drawText(
            box.adjusted(pad_x, pad_y, -pad_x, -pad_y),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            label,
        )

        if self.compare_mode in (COMPARE_WIPE, COMPARE_BLEND):
            wipe_x = int(self.wipe_pos * metrics_rect.width())
            color = QColor(80, 200, 255, 200) if self.compare_mode == COMPARE_BLEND else QColor(255, 165, 0, 180)
            painter.setPen(QPen(color, 2, Qt.DashLine))
            painter.drawLine(wipe_x, 0, wipe_x, metrics_rect.height())
            painter.setPen(QPen(color, 1))
            font = mono_font(10, QFont.Weight.Bold)
            painter.setFont(font)
            tag = "Blend" if self.compare_mode == COMPARE_BLEND else self._wipe_label()
            if self.compare_mode == COMPARE_BLEND:
                tag = f"{tag} {int(round(self.wipe_pos * 100))}%"
            painter.drawText(wipe_x + 5, metrics_rect.height() // 2, tag)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # Percent / free-zoom multipliers depend on widget size; aspect-fit itself
        # is recomputed on the render thread from the live swapchain each present.
        if self.zoom_mode != "fit" and self.compare_mode != COMPARE_TILE:
            self._apply_zoom_mode()
        self.update()

    def _annotation_overlay(self):
        container = getattr(self, "viewport_container", None)
        if container is None:
            return None
        return getattr(container, "_annotation_overlay", None)

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
            if self.compare_mode in (COMPARE_WIPE, COMPARE_BLEND):
                click_x_ratio = event.position().x() / self.width()
                if abs(click_x_ratio - self.wipe_pos) < 0.02:
                    self.dragging_wipe = True
                    self.setCursor(Qt.SizeHorCursor)
                    return

            annot = self._annotation_overlay()
            if annot is not None and annot.is_interactive():
                consumed = annot.handle_press(
                    event.position().x(),
                    event.position().y(),
                    parent_widget=self,
                )
                # Drawing tools always own left-drag (disable timeline scrub).
                if consumed or annot.captures_left_drag():
                    self.left_press_pos = None
                    self.left_drag_started = False
                    self.scrubbing_frames = False
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
        if self.main_window is not None and hasattr(self.main_window, "_probe_pointer_moved"):
            self.main_window._probe_pointer_moved(event.position())

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

        annot = self._annotation_overlay()
        if (
            annot is not None
            and annot.is_interactive()
            and (event.buttons() & Qt.LeftButton)
            and (annot.is_annotating() or annot.captures_left_drag())
        ):
            annot.handle_move(event.position().x(), event.position().y())
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
                self.scrub_started.emit()
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
        elif self.compare_mode in (COMPARE_WIPE, COMPARE_BLEND):
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

        annot = self._annotation_overlay()
        if (
            event.button() == Qt.LeftButton
            and annot is not None
            and annot.is_interactive()
            and (annot.is_annotating() or annot.captures_left_drag())
        ):
            annot.handle_release(event.position().x(), event.position().y())
            self.dragging_wipe = False
            self.panning = False
            self.scrubbing_frames = False
            self.left_press_pos = None
            self.left_drag_started = False
            self.setCursor(Qt.ArrowCursor)
            return

        was_frame_scrub = bool(self.scrubbing_frames)
        if event.button() == Qt.LeftButton and self.left_press_pos is not None:
            if self.scrubbing_frames and self.main_window:
                frame_offset = int(
                    (event.position().x() - self.scrub_start_x) / self.scrub_sensitivity
                )
                target_frame = self.scrub_start_frame + frame_offset
                self.frame_scrubbed.emit(target_frame)
            elif (
                not self.left_drag_started
                and not self.dragging_wipe
                and self.main_window
                and not (event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
            ):
                # Plain click toggles playback; Shift+click is for pixel probe only.
                self.main_window.toggle_playback()

        self.dragging_wipe = False
        self.panning = False
        self.scrubbing_frames = False
        self.left_press_pos = None
        self.left_drag_started = False
        self.setCursor(Qt.ArrowCursor)
        if was_frame_scrub:
            self.scrub_finished.emit()

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
        if hasattr(self, "native_renderer") and self.native_renderer is not None:
            self.native_renderer.shutdown()
        self._renderer_initialized = False
        self._ocio_pipeline_ready = False

    # Viewer present format (SDR / EDR / HDR10) — thin wrappers over RhiRenderer.
    VIEWER_OUTPUT_SDR = 0
    VIEWER_OUTPUT_EDR = 1
    VIEWER_OUTPUT_HDR10 = 2

    def set_viewer_output_mode(self, mode: int) -> None:
        renderer = getattr(self, "native_renderer", None)
        if renderer is None or not hasattr(renderer, "set_viewer_output_mode"):
            return
        renderer.set_viewer_output_mode(int(mode))
        if hasattr(renderer, "request_redraw"):
            renderer.request_redraw()

    def viewer_output_mode(self) -> int:
        renderer = getattr(self, "native_renderer", None)
        if renderer is None or not hasattr(renderer, "viewer_output_mode"):
            return self.VIEWER_OUTPUT_SDR
        return int(renderer.viewer_output_mode())

    def actual_viewer_output_mode(self) -> int:
        renderer = getattr(self, "native_renderer", None)
        if renderer is None or not hasattr(renderer, "actual_viewer_output_mode"):
            return self.VIEWER_OUTPUT_SDR
        return int(renderer.actual_viewer_output_mode())

    def is_viewer_output_mode_supported(self, mode: int) -> bool:
        renderer = getattr(self, "native_renderer", None)
        if renderer is None or not hasattr(renderer, "is_viewer_output_mode_supported"):
            return mode == self.VIEWER_OUTPUT_SDR
        return bool(renderer.is_viewer_output_mode_supported(int(mode)))

    def clear_frames(self):
        self.frame_slots.clear()
        self.source_labels.clear()
        self.sequence_index = 0
        self.pixel_aspect_ratio = 1.0
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
