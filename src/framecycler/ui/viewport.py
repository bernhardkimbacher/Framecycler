import numpy as np
from PySide6.QtOpenGLWidgets import QOpenGLWidget
from PySide6.QtCore import Qt, QPoint, QRectF, Signal
from PySide6.QtGui import QPainter, QColor, QFont, QPen
from ..color.ocio_manager import OCIOManager

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
        self.scrub_start_x = 0
        self.scrub_start_frame = 0
        self.scrub_sensitivity = 8.0  # Pixels per frame scroll
        self.setMouseTracking(True)
        
        # Viewfinder HUD info overlays
        self.hud_visible = True
        self.current_frame = 0
        self.current_timecode = "01:00:00:00"
        self.fps = 24.0
        self.resolution_str = "0x0"
        self.exr_layer_str = "RGB"
        
        # Interactive CDL Adjustment flags
        self.adjustment_mode = None
        self.adjust_start_x = 0
        self.adjust_start_value = 0.0
        self.adjust_current_value = 0.0

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
        widget_w, widget_h = self.width(), self.height()
        aspect_widget = widget_w / widget_h
        aspect_frame = self.width_a / self.height_a
        
        scale_x = 1.0
        scale_y = 1.0
        
        if aspect_widget > aspect_frame:
            scale_x = aspect_frame / aspect_widget
        else:
            scale_y = aspect_widget / aspect_frame
            
        # Translate pans to normalized clip coords (-1.0 to 1.0)
        pan_x = (self.pan_offset.x() / widget_w) * 2.0
        pan_y = -(self.pan_offset.y() / widget_h) * 2.0
        
        # Run C++ compiled hardware rendering pass
        self.native_renderer.render(
            self.frame_data_a, self.width_a, self.height_a, self.channels_a,
            self.frame_data_b, self.width_b, self.height_b, self.channels_b,
            self.compare_mode, self.wipe_pos, self.channel_mask,
            scale_x * self.zoom, scale_y * self.zoom, pan_x, pan_y
        )
        
        # Draw camera viewfinder vector/text overlays
        if self.hud_visible or self.adjustment_mode:
            painter = QPainter(self)
            
            # Draw interactive CDL adjustment overlay (green Courier New bold at top center)
            if self.adjustment_mode:
                font = QFont("Courier New", 10, QFont.Bold)
                painter.setFont(font)
                painter.setPen(QColor(0, 255, 0, 220))
                text = f"Adjusting {self.adjustment_mode.upper()}: {self.adjust_current_value:.2f}"
                text_w = painter.fontMetrics().horizontalAdvance(text)
                margin = 15
                painter.drawText((self.rect().width() - text_w) // 2, margin + 15, text)
                
            if self.hud_visible:
                self._draw_hud(painter)
                
            painter.end()

    def _draw_hud(self, painter: QPainter):
        painter.setRenderHint(QPainter.Antialiasing)
        metrics_rect = self.rect()
        
        if self.compare_mode == 1:
            wipe_x = int(self.wipe_pos * metrics_rect.width())
            painter.setPen(QPen(QColor(255, 165, 0, 180), 2, Qt.DashLine))
            painter.drawLine(wipe_x, 0, wipe_x, metrics_rect.height())
            painter.setPen(QPen(QColor(255, 165, 0, 220), 1))
            font = QFont("Courier New", 10, QFont.Bold)
            painter.setFont(font)
            painter.drawText(wipe_x + 5, metrics_rect.height() // 2, "A | B")

    def resizeGL(self, w, h):
        pass

    # Mouse actions
    def mousePressEvent(self, event):
        if self.adjustment_mode and event.button() == Qt.LeftButton:
            self.adjust_start_x = event.position().x()
            self.adjust_start_value = self.adjust_current_value
            self.setCursor(Qt.SizeHorCursor)
            return
            
        if event.button() == Qt.LeftButton:
            if self.compare_mode == 1:
                click_x_ratio = event.position().x() / self.width()
                if abs(click_x_ratio - self.wipe_pos) < 0.02:
                    self.dragging_wipe = True
                    self.setCursor(Qt.SizeHorCursor)
                    return
            self.scrubbing_frames = True
            self.scrub_start_x = event.position().x()
            self.scrub_start_frame = self.current_frame
            self.setCursor(Qt.SizeHorCursor)
        elif event.button() == Qt.MiddleButton:
            self.panning = True
            self.last_mouse_pos = event.position().toPoint()
            self.setCursor(Qt.ClosedHandCursor)

    def mouseMoveEvent(self, event):
        if self.adjustment_mode and (event.buttons() & Qt.LeftButton):
            delta_x = event.position().x() - self.adjust_start_x
            
            sens = 0.005 if self.adjustment_mode == 'offset' else 0.01
            new_val = self.adjust_start_value + delta_x * sens
            
            if self.adjustment_mode == 'slope':
                new_val = max(0.0, min(2.0, new_val))
                self.main_window.slope_slider.setValue(int(new_val * 100))
            elif self.adjustment_mode == 'offset':
                new_val = max(-1.0, min(1.0, new_val))
                self.main_window.offset_slider.setValue(int(new_val * 100))
            elif self.adjustment_mode == 'power':
                new_val = max(0.1, min(3.0, new_val))
                self.main_window.slope_power.setValue(int(new_val * 100))
            elif self.adjustment_mode == 'saturation':
                new_val = max(0.0, min(2.0, new_val))
                self.main_window.sat_slider.setValue(int(new_val * 100))
                
            self.adjust_current_value = new_val
            self.update()
            return
            
        if self.dragging_wipe:
            self.wipe_pos = max(0.0, min(1.0, event.position().x() / self.width()))
            self.wipe_changed.emit(self.wipe_pos)
            self.update()
        elif self.scrubbing_frames:
            delta_x = event.position().x() - self.scrub_start_x
            frame_offset = int(delta_x / self.scrub_sensitivity)
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
            self.main_window.statusBar().showMessage("Interactive adjustment finished.")
            self.update()
            return
            
        self.dragging_wipe = False
        self.panning = False
        self.scrubbing_frames = False
        self.setCursor(Qt.ArrowCursor)

    def wheelEvent(self, event):
        zoom_factor = 1.1 if event.angleDelta().y() > 0 else 0.9
        self.zoom = max(0.1, min(20.0, self.zoom * zoom_factor))
        self.update()

    def cleanup(self):
        self.makeCurrent()
        self.native_renderer.cleanup()
        self.doneCurrent()
