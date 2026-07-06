import numpy as np
from PySide6.QtOpenGLWidgets import QOpenGLWidget
from PySide6.QtCore import Qt, QPoint, QRectF, Signal
from PySide6.QtGui import QPainter, QColor, QFont, QPen
from ..color.ocio_manager import OCIOManager
from .fonts import mono_font, ui_font

# Import C++ Engine binary extension
try:
    from .. import framecycler_engine
except ImportError:
    import framecycler_engine

class Viewport(QOpenGLWidget):
    wipe_changed = Signal(float)
    frame_scrubbed = Signal(int)
    
    def __init__(self, ocio_manager: OCIOManager, parent=None):
        super().__init__(parent)
        self.ocio_manager = ocio_manager
        self.main_window = parent
        
        # Instantiate C++ GPU Renderer
        self.native_renderer = framecycler_engine.GLRenderer()
        
        # Keep references to active frame data to prevent Python GC sweeps
        self.frame_data_a = None
        self.frame_data_b = None
        self.width_a, self.height_a, self.channels_a = 0, 0, 3
        self.width_b, self.height_b, self.channels_b = 0, 0, 3
        self.pixel_aspect_ratio = 1.0
        
        # Viewport layout and sliders
        self.compare_mode = 0      # 0 = normal (A only), 1 = split-screen, 2 = difference, 3 = tiling
        self.channel_mask = 0      # 0 = RGBA, 1 = R, 2 = G, 3 = B, 4 = A, 5 = Lum
        self.wipe_pos = 0.5
        
        # Mouse zoom and drag
        self.zoom = 1.0
        self.pan_offset = QPoint(0, 0)
        self.last_mouse_pos = QPoint(0, 0)
        
        # State tracking flags
        self.panning = False
        self.dragging_wipe = False
        self.scrubbing_frames = False
        
        # Interactive grading parameters
        self.adjustment_mode = None
        self.adjust_start_x = 0
        self.adjust_start_value = 0.0
        self.scrub_start_x = 0
        self.scrub_start_frame = 0
        self.scrub_sensitivity = 8.0  # Pixels per frame scroll
        self.left_press_pos = None
        self.left_drag_started = False
        self.click_threshold = 5
        self.setMouseTracking(True)
        
        # Viewfinder HUD info overlays
        self.hud_visible = True
        self.current_frame = 0
        self.current_timecode = "01:00:00:00"
        self.fps = 24.0
        self.resolution_str = "0x0"
        self.exr_layer_str = "RGB"
        


    def set_frame_a(self, data: np.ndarray, channels: list, index: int, timecode: str, fps: float):
        self.frame_data_a = data
        self.height_a, self.width_a = data.shape[:2]
        self.channels_a = data.shape[2] if len(data.shape) > 2 else 1
        
        self.current_frame = index
        self.current_timecode = timecode
        self.fps = fps
        self.resolution_str = f"{self.width_a}x{self.height_a}"
        self.update()

    def set_frame_b(self, data: np.ndarray):
        self.frame_data_b = data
        if data is not None:
            self.height_b, self.width_b = data.shape[:2]
            self.channels_b = data.shape[2] if len(data.shape) > 2 else 1
        self.update()

    def set_pixel_aspect_ratio(self, par: float):
        if par <= 0.0:
            par = 1.0
        self.pixel_aspect_ratio = par
        self.update()

    def set_compare_mode(self, mode: int):
        self.compare_mode = mode
        self.update()

    def set_channel_mask(self, mask: int):
        self.channel_mask = mask
        self.update()

    def toggle_hud(self):
        self.hud_visible = not self.hud_visible
        self.update()

    def reset_view(self):
        self.zoom = 1.0
        self.pan_offset = QPoint(0, 0)
        self.update()

    def update_ocio_pipeline(self):
        """
        Compile new OCIO transformations and upload 3D LUT datasets to C++ Core.
        """
        self.makeCurrent()
        # Compile GLSL block from OCIO
        ocio_shader_code, lut3ds, lut1ds = self.ocio_manager.get_gpu_shader_glsl()
        
        # Load shader string into compiled C++ GLRenderer shader program
        self.native_renderer.set_ocio_shader(ocio_shader_code)
        
        # Upload 3D lookup tables directly into compiled texture buffer units in C++
        for i, lut in enumerate(lut3ds):
            lut_data = np.array(lut["data"], dtype=np.float32)
            self.native_renderer.upload_ocio_lut_3d(i, lut["size"], lut_data)
            
        self.doneCurrent()
        self.update()

    def initializeGL(self):
        # Delegate VAO/VBO creation and Extension Loading to C++
        self.native_renderer.initialize()
        
        # Compile default viewport pipelines
        self.update_ocio_pipeline()

    def paintGL(self):
        if self.frame_data_a is None:
            return
            
        # Aspect scaling calculations
        # Note: self.width()/height() return logical pixels; Qt's QOpenGLWidget already
        # sets glViewport to physical (device-pixel) dimensions before calling paintGL.
        # We use logical dimensions here for aspect ratio, which only uses ratios so
        # the result is identical whether logical or physical values are used.
        widget_w, widget_h = self.width(), self.height()
        aspect_widget = widget_w / widget_h
        aspect_frame = (self.width_a * self.pixel_aspect_ratio) / self.height_a
        
        scale_x = 1.0
        scale_y = 1.0
        
        if aspect_widget > aspect_frame:
            scale_x = aspect_frame / aspect_widget
        else:
            scale_y = aspect_widget / aspect_frame
            
        # Translate pans to normalized clip coords (-1.0 to 1.0)
        pan_x = (self.pan_offset.x() / widget_w) * 2.0
        pan_y = -(self.pan_offset.y() / widget_h) * 2.0
        
        # Ensure arrays are C-contiguous — non-contiguous views (e.g. zero-copy C++ cache slices)
        # can have unexpected strides that cause OpenGL to read garbage bytes between rows,
        # producing pixel streak artifacts.
        data_a = np.ascontiguousarray(self.frame_data_a)
        data_b = np.ascontiguousarray(self.frame_data_b) if self.frame_data_b is not None else None

        # Qt requires that QPainter be started BEFORE any native OpenGL calls inside
        # paintGL, and that raw GL calls be wrapped in beginNativePainting() /
        # endNativePainting(). Doing raw OpenGL then QPainter causes undefined compositing
        # behavior on macOS (image offset to one quadrant with pixel streaks).
        painter = QPainter(self)
        painter.beginNativePainting()

        # Run C++ compiled hardware rendering pass
        self.native_renderer.render(
            data_a, self.width_a, self.height_a, self.channels_a,
            data_b, self.width_b, self.height_b, self.channels_b,
            self.compare_mode, self.wipe_pos, self.channel_mask,
            scale_x * self.zoom, scale_y * self.zoom, pan_x, pan_y
        )

        painter.endNativePainting()

        # Draw camera viewfinder vector/text overlays on top of the rendered frame
        if self.hud_visible:
            self._draw_hud(painter)
            
        if self.adjustment_mode:
            self._draw_adjustment_overlay(painter)

        painter.end()

    def _draw_hud(self, painter: QPainter):
        painter.setRenderHint(QPainter.Antialiasing)
        metrics_rect = self.rect()
        
        if self.compare_mode == 1:
            wipe_x = int(self.wipe_pos * metrics_rect.width())
            painter.setPen(QPen(QColor(255, 165, 0, 180), 2, Qt.DashLine))
            painter.drawLine(wipe_x, 0, wipe_x, metrics_rect.height())
            painter.setPen(QPen(QColor(255, 165, 0, 220), 1))
            font = mono_font(10, QFont.Weight.Bold)
            painter.setFont(font)
            painter.drawText(wipe_x + 5, metrics_rect.height() // 2, "A | B")

    def resizeGL(self, w, h):
        # Qt's QOpenGLWidget already sets glViewport(0, 0, w, h) in physical device pixels
        # before calling resizeGL. No additional glViewport call is needed here.
        pass


            
    # Mouse actions
    def mousePressEvent(self, event):
        if self.adjustment_mode and event.button() == Qt.LeftButton:
            self.adjust_start_x = event.position().x()
            self.adjust_start_value = (
                self.ocio_manager.grade_exposure if self.adjustment_mode == 'exposure'
                else self.ocio_manager.grade_gamma if self.adjustment_mode == 'gamma'
                else self.ocio_manager.grade_offset
            )
            self.setCursor(Qt.SizeHorCursor)
            return

        if event.button() == Qt.LeftButton:
            if self.compare_mode == 1:
                click_x_ratio = event.position().x() / self.width()
                if abs(click_x_ratio - self.wipe_pos) < 0.02:
                    self.dragging_wipe = True
                    self.setCursor(Qt.SizeHorCursor)
                    return
            self.left_press_pos = event.position()
            self.left_drag_started = False
            self.scrub_start_x = event.position().x()
            self.scrub_start_frame = self.current_frame
        elif event.button() == Qt.MiddleButton:
            self.panning = True
            self.last_mouse_pos = event.position().toPoint()
            self.setCursor(Qt.ClosedHandCursor)

    def mouseMoveEvent(self, event):
        if self.adjustment_mode and (event.buttons() & Qt.LeftButton):
            delta_x = event.position().x() - self.adjust_start_x
            
            if self.adjustment_mode == 'exposure':
                new_val = self.adjust_start_value + delta_x * 0.02
                new_val = max(-5.0, min(5.0, new_val))
                self.ocio_manager.grade_exposure = new_val
            elif self.adjustment_mode == 'gamma':
                new_val = self.adjust_start_value + delta_x * 0.003
                new_val = max(0.1, min(5.0, new_val))
                self.ocio_manager.grade_gamma = new_val
            elif self.adjustment_mode == 'offset':
                new_val = self.adjust_start_value + delta_x * 0.002
                new_val = max(-0.5, min(0.5, new_val))
                self.ocio_manager.grade_offset = new_val
                
            self.update_ocio_pipeline()
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
            if not self.left_drag_started and (delta_x > self.click_threshold or delta_y > self.click_threshold):
                self.left_drag_started = True
                self.scrubbing_frames = True
                self.setCursor(Qt.SizeHorCursor)
            if self.scrubbing_frames:
                frame_offset = int((event.position().x() - self.scrub_start_x) / self.scrub_sensitivity)
                target_frame = self.scrub_start_frame + frame_offset
                self.frame_scrubbed.emit(target_frame)
        elif self.panning:
            delta = event.position().toPoint() - self.last_mouse_pos
            self.pan_offset += delta
            self.last_mouse_pos = event.position().toPoint()
            self.update()
        elif self.compare_mode == 1:
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
            if not self.left_drag_started and not self.dragging_wipe and self.main_window:
                self.main_window.toggle_playback()

        self.dragging_wipe = False
        self.panning = False
        self.scrubbing_frames = False
        self.left_press_pos = None
        self.left_drag_started = False
        self.setCursor(Qt.ArrowCursor)

    def wheelEvent(self, event):
        zoom_factor = 1.1 if event.angleDelta().y() > 0 else 0.9
        self.zoom = max(0.1, min(20.0, self.zoom * zoom_factor))
        self.update()

    def cleanup(self):
        self.makeCurrent()
        self.native_renderer.cleanup()
        self.doneCurrent()

    def clear_frames(self):
        self.frame_data_a = None
        self.frame_data_b = None
        self.pixel_aspect_ratio = 1.0
        self.update()

    def _draw_adjustment_overlay(self, painter: QPainter):
        painter.setRenderHint(QPainter.Antialiasing)
        rect = self.rect()
        
        # Determine value to display
        val = 0.0
        unit = ""
        if self.adjustment_mode == 'exposure':
            val = self.ocio_manager.grade_exposure
            val_str = f"{val:+.2f}"
            unit = " stops"
        elif self.adjustment_mode == 'gamma':
            val = self.ocio_manager.grade_gamma
            val_str = f"{val:.2f}"
        elif self.adjustment_mode == 'offset':
            val = self.ocio_manager.grade_offset
            val_str = f"{val:+.3f}"
            
        text = f"{self.adjustment_mode.upper()}: {val_str}{unit}"
        
        # Background box dimensions (tight fit)
        box_w = 250
        box_h = 36
        
        # Bottom-left corner with 20px padding
        box_x = 20
        box_y = rect.height() - box_h - 20
        
        # Draw rounded rectangle background (no border)
        painter.setBrush(QColor(0, 0, 0, 180)) # semi-transparent black
        painter.setPen(Qt.NoPen)
        painter.drawRoundedRect(box_x, box_y, box_w, box_h, 6.0, 6.0)
        
        # Draw text
        painter.setPen(QColor(255, 255, 255))
        font = ui_font(13, QFont.Weight.Bold)
        painter.setFont(font)
        painter.drawText(box_x, box_y, box_w, box_h, Qt.AlignCenter, text)
