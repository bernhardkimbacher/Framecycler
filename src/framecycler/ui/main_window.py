import os
import sys
from PySide6.QtWidgets import (QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
                             QFileDialog, QMenuBar, QMenu, QPushButton, 
                             QComboBox, QLabel, QDockWidget, QSlider, QInputDialog)
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QKeySequence, QFont

from ..core.settings import Settings
from ..core.timecode import Timecode
from ..core.cache import CacheEngine
from ..color.ocio_manager import OCIOManager
from ..decoders.exr_decoder import EXRDecoder
from ..decoders.dpx_decoder import DPXDecoder
from ..decoders.qt_decoder import QuickTimeDecoder
from ..decoders.image_io import ANAMORPHIC_PIXEL_ASPECT, SQUARE_PIXEL_ASPECT
from ..extensions.ocio_api_tool import OcioApiTool


from .viewport import Viewport
from .timeline import Timeline
from .theme import get_viewfinder_stylesheet
from .settings_dialog import SettingsDialog
from .widgets import WideComboBox, add_menu_section
from .fonts import ui_font

PRESET_FRAME_RATES = [
    ("23.976", 23.976),
    ("24", 24.0),
    ("25", 25.0),
    ("29.97", 29.97),
    ("30", 30.0),
]

PIXEL_ASPECT_MODES = [
    ("Square", "square"),
    ("Anamorphic", "anamorphic"),
]

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Framecycler Reboot")
        self.resize(1200, 800)
        
        # Load stylesheet
        self.setStyleSheet(get_viewfinder_stylesheet())
        
        # Core engines
        self.settings = Settings()
        self.ocio_manager = OCIOManager(self.settings.ocio_config_path)
        
        # Playback states
        self.decoders = [None, None]  # Slot 0 = A, Slot 1 = B
        self.caches = [None, None]
        self.active_slot = 0          # 0 = A, 1 = B (active comparison view)
        
        self.playing = False
        self.playback_direction = 1
        self.current_frame = 0
        self.start_frame = 0
        self.end_frame = 0
        self.in_point = 0
        self.out_point = 0
        self.fps = self.settings.default_fps
        self.active_exr_layer = "beauty"
        self.pixel_aspect_mode = "square"
        self.file_pixel_aspect_ratio = SQUARE_PIXEL_ASPECT
        
        # Dynamic menus references
        self.exr_layer_combo = None
        self.input_space_menu = None
        self.display_menu = None
        self.view_menu = None
        
        # Playback timer
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._playback_tick)
        
        # Plugins registration
        self.plugins = [OcioApiTool(self)]
        for plugin in self.plugins:
            plugin.on_init()
            
        # Build UI layout
        self._init_ui()
        
        # Apply hotkeys
        self._setup_hotkeys()
        
        # Enable Drag and Drop
        self.setAcceptDrops(True)

    def _init_ui(self):
        # Create Central Viewport
        self.viewport = Viewport(self.ocio_manager, self)
        self.viewport.wipe_changed.connect(self._on_wipe_moved)
        self.viewport.frame_scrubbed.connect(self.seek_to_frame)
        
        # Create Custom Timeline
        self.timeline = Timeline(self)
        self.timeline.frame_changed.connect(self.seek_to_frame)
        self.timeline.in_out_changed.connect(self._on_in_out_changed)
        
        # Central layout
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)
        main_layout.setContentsMargins(5, 5, 5, 5)
        main_layout.setSpacing(2)
        
        # Viewport Header controls row
        header_layout = QHBoxLayout()
        header_layout.setContentsMargins(10, 2, 10, 2)
        
        # Left header: Channel extraction icons / drop-down
        header_layout.addWidget(QLabel("LAYER:"))
        self.exr_layer_combo = WideComboBox(min_popup_width=240)
        self.exr_layer_combo.addItem("beauty")
        self.exr_layer_combo.currentIndexChanged.connect(self._on_exr_layer_changed)
        header_layout.addWidget(self.exr_layer_combo)
        
        # Resolution label (no "RESO:" text prefix, just width x height)
        lbl_font = ui_font(10)
        header_layout.addSpacing(20)
        self.lbl_resolution = QLabel("")
        self.lbl_resolution.setFont(lbl_font)
        header_layout.addWidget(self.lbl_resolution)
        
        # IN/OUT colorspace info and Look selector
        header_layout.addSpacing(20)
        self.lbl_ocio_info = QLabel("")
        self.lbl_ocio_info.setFont(lbl_font)
        header_layout.addWidget(self.lbl_ocio_info)

        header_layout.addSpacing(20)
        header_layout.addWidget(QLabel("LOOK:"))
        self.look_combo = WideComboBox(min_popup_width=420)
        self.look_combo.currentTextChanged.connect(self._on_look_combo_changed)
        self._populate_look_combo()
        header_layout.addWidget(self.look_combo)
        
        header_layout.addStretch()
        
        # Right header: channel quick buttons
        channels = [("RGB", 0), ("R", 1), ("G", 2), ("B", 3), ("A", 4), ("LUM", 5)]
        self.channel_buttons = {}
        for label, val in channels:
            btn = QPushButton(label)
            btn.setMaximumWidth(40)
            btn.setFocusPolicy(Qt.NoFocus)
            btn.setCheckable(True)
            if val == 0:
                btn.setChecked(True)
            btn.clicked.connect(lambda checked=False, v=val: self.toggle_channel_mask(v))
            header_layout.addWidget(btn)
            self.channel_buttons[val] = btn
            
        main_layout.addLayout(header_layout)
        main_layout.addWidget(self.viewport, stretch=1)
        
        # Readout row just above the timeline
        readout_widget = QWidget()
        readout_layout = QGridLayout(readout_widget)
        readout_layout.setContentsMargins(10, 2, 10, 2)
        readout_layout.setColumnStretch(0, 1)
        readout_layout.setColumnStretch(1, 0)
        readout_layout.setColumnStretch(2, 1)

        self.lbl_frame = QLabel("FR: 0000")
        self.lbl_fps = QLabel("FPS: 24.00")

        readout_font = ui_font(11)
        self.lbl_frame.setFont(readout_font)
        self.lbl_fps.setFont(readout_font)

        readout_layout.addWidget(self.lbl_frame, 0, 0, Qt.AlignLeft | Qt.AlignVCenter)
        readout_layout.addWidget(self.lbl_fps, 0, 1, Qt.AlignCenter)
        readout_layout.addWidget(QWidget(), 0, 2)

        main_layout.addWidget(readout_widget)

        # Transport controls row (above timeline)
        transport_layout = QHBoxLayout()
        transport_layout.setContentsMargins(10, 0, 10, 2)

        transport_font = ui_font(13)

        self.btn_begin = self._make_transport_button("|<", transport_font)
        self.btn_begin.setToolTip("Jump to beginning")
        self.btn_begin.clicked.connect(lambda: self.seek_to_frame(self.in_point))

        self.btn_step_back = self._make_transport_button("<|", transport_font)
        self.btn_step_back.setToolTip("Step back one frame")
        self.btn_step_back.clicked.connect(lambda: self.seek_to_frame(self.current_frame - 1))

        self.btn_play_reverse = self._make_transport_button("◀", transport_font)
        self.btn_play_reverse.setToolTip("Play reverse")
        self.btn_play_reverse.setCheckable(True)
        self.btn_play_reverse.clicked.connect(lambda: self._toggle_playback_direction(-1))

        self.btn_stop = self._make_transport_button("■", transport_font)
        self.btn_stop.setToolTip("Stop")
        self.btn_stop.clicked.connect(self.stop_playback)

        self.btn_play_forward = self._make_transport_button("▶", transport_font)
        self.btn_play_forward.setToolTip("Play forward")
        self.btn_play_forward.setCheckable(True)
        self.btn_play_forward.clicked.connect(lambda: self._toggle_playback_direction(1))

        self.btn_step_forward = self._make_transport_button("|>", transport_font)
        self.btn_step_forward.setToolTip("Step forward one frame")
        self.btn_step_forward.clicked.connect(lambda: self.seek_to_frame(self.current_frame + 1))

        self.btn_end = self._make_transport_button(">|", transport_font)
        self.btn_end.setToolTip("Jump to end")
        self.btn_end.clicked.connect(lambda: self.seek_to_frame(self.out_point))

        transport_layout.addStretch()
        for btn in (
            self.btn_begin,
            self.btn_step_back,
            self.btn_play_reverse,
            self.btn_stop,
            self.btn_play_forward,
            self.btn_step_forward,
            self.btn_end,
        ):
            transport_layout.addWidget(btn)
        transport_layout.addStretch()

        main_layout.addLayout(transport_layout)
        main_layout.addWidget(self.timeline)
        self.timeline.set_display_options(self.settings.timecode_mode, self.fps)
        self._update_readout_display()
        # Setup Menu bar
        self._build_menu()
        
        # Set status bar
        self.statusBar().showMessage("Ready.")

    def _build_menu(self):
        menubar = self.menuBar()
        
        # File menu
        file_menu = menubar.addMenu("&File")
        
        act_open_a = QAction("Open Media (Slot A)...", self)
        act_open_a.triggered.connect(lambda: self._open_file_dialog(0))
        file_menu.addAction(act_open_a)
        
        act_open_b = QAction("Open Media (Slot B)...", self)
        act_open_b.triggered.connect(lambda: self._open_file_dialog(1))
        file_menu.addAction(act_open_b)
        
        act_clear = QAction("Clear", self)
        act_clear.setShortcut(QKeySequence("Shift+X"))
        act_clear.triggered.connect(self.clear_media)
        file_menu.addAction(act_clear)
        
        file_menu.addSeparator()
        
        act_settings = QAction("Settings...", self)
        act_settings.triggered.connect(self._open_settings_dialog)
        file_menu.addAction(act_settings)
        
        act_exit = QAction("Exit", self)
        act_exit.triggered.connect(self.close)
        file_menu.addAction(act_exit)
        
        # View menu
        view_menu = menubar.addMenu("&View")
        act_hud = QAction("Toggle HUD", self)
        act_hud.setShortcut(QKeySequence("Ctrl+H"))
        act_hud.triggered.connect(self.viewport.toggle_hud)
        view_menu.addAction(act_hud)
        
        act_reset = QAction("Reset Pan/Zoom", self)
        act_reset.setShortcut(QKeySequence("F"))
        act_reset.triggered.connect(self.viewport.reset_view)
        view_menu.addAction(act_reset)

        # Image menu
        image_menu = menubar.addMenu("&Image")
        par_menu = image_menu.addMenu("Pixel Aspect Ratio")
        self.pixel_aspect_actions = []
        for label, mode in PIXEL_ASPECT_MODES:
            act = QAction(label, self)
            act.setCheckable(True)
            act.setChecked(mode == self.pixel_aspect_mode)
            act.triggered.connect(lambda checked=False, m=mode: self._set_pixel_aspect_mode(m))
            par_menu.addAction(act)
            self.pixel_aspect_actions.append((mode, act))

        # Playback menu
        playback_menu = menubar.addMenu("&Playback")

        add_menu_section(playback_menu, "Mode", first=True)

        self.loop_mode_actions = []
        loop_modes = [("Loop", "loop"), ("Bounce", "bounce"), ("Once", "once")]
        for label, mode in loop_modes:
            act = QAction(label, self)
            act.setCheckable(True)
            act.setChecked(mode == self.settings.loop_mode)
            act.triggered.connect(lambda checked=False, m=mode: self._set_loop_mode(m))
            playback_menu.addAction(act)
            self.loop_mode_actions.append((mode, act))

        playback_menu.addSeparator()

        frame_rate_menu = playback_menu.addMenu("Frame Rate")
        self.frame_rate_actions = []
        for label, fps in PRESET_FRAME_RATES:
            act = QAction(label, self)
            act.setCheckable(True)
            act.setChecked(self._fps_matches(self.fps, fps))
            act.triggered.connect(lambda checked=False, f=fps: self._set_playback_fps(f))
            frame_rate_menu.addAction(act)
            self.frame_rate_actions.append((fps, act))

        frame_rate_menu.addSeparator()
        act_custom_fps = QAction("Custom...", self)
        act_custom_fps.triggered.connect(self._prompt_custom_frame_rate)
        frame_rate_menu.addAction(act_custom_fps)

        add_menu_section(playback_menu, "Display")

        act_show_frames = QAction("Show Frames", self)
        act_show_frames.setCheckable(True)
        act_show_frames.setChecked(not self.settings.timecode_mode)
        act_show_frames.triggered.connect(lambda: self._set_timecode_display_mode(False))
        playback_menu.addAction(act_show_frames)

        act_show_timecode = QAction("Show Timecode", self)
        act_show_timecode.setCheckable(True)
        act_show_timecode.setChecked(self.settings.timecode_mode)
        act_show_timecode.triggered.connect(lambda: self._set_timecode_display_mode(True))
        playback_menu.addAction(act_show_timecode)

        self.timecode_display_actions = [
            (False, act_show_frames),
            (True, act_show_timecode),
        ]
        
        # Plugins menu
        plugins_menu = menubar.addMenu("&Plugins")
        for plugin in self.plugins:
            for action in plugin.get_menu_actions():
                plugins_menu.addAction(action)
                
        # Tools menu (grading and compare)
        tools_menu = menubar.addMenu("&Tools")

        add_menu_section(tools_menu, "Grading", first=True)

        act_exposure = QAction("Exposure...", self)
        act_exposure.setShortcut(QKeySequence("E"))
        act_exposure.triggered.connect(self._activate_exposure_mode)
        tools_menu.addAction(act_exposure)

        act_gamma = QAction("Gamma...", self)
        act_gamma.setShortcut(QKeySequence("Y"))
        act_gamma.triggered.connect(self._activate_gamma_mode)
        tools_menu.addAction(act_gamma)

        act_offset = QAction("Offset...", self)
        act_offset.setShortcut(QKeySequence("O"))
        act_offset.triggered.connect(self._activate_offset_mode)
        tools_menu.addAction(act_offset)

        act_reset_grade = QAction("Reset Color Grade", self)
        act_reset_grade.setShortcut(QKeySequence("Home"))
        act_reset_grade.triggered.connect(self._reset_grade)
        tools_menu.addAction(act_reset_grade)

        add_menu_section(tools_menu, "Compare")

        self.compare_actions = []
        compare_modes = [
            ("A Only", 0),
            ("Split Screen", 1),
            ("Difference", 2),
            ("Side-by-Side", 3),
        ]
        for label, mode in compare_modes:
            act = QAction(label, self)
            act.setCheckable(True)
            act.setChecked(mode == self.viewport.compare_mode)
            act.triggered.connect(lambda checked=False, m=mode: self._set_compare_mode(m))
            tools_menu.addAction(act)
            self.compare_actions.append((mode, act))
        
        # OCIO Pipeline menu
        self.ocio_menu = menubar.addMenu("&OCIO")
        self._build_ocio_submenu()
        
        # Help menu
        help_menu = menubar.addMenu("&Help")
        act_about = QAction("About Framecycler Reboot", self)
        act_about.setMenuRole(QAction.MenuRole.NoRole)
        act_about.triggered.connect(self._show_about_dialog)
        help_menu.addAction(act_about)
        if sys.platform == "darwin":
            act_about_native = QAction("About Framecycler Reboot", self)
            act_about_native.setMenuRole(QAction.MenuRole.AboutRole)
            act_about_native.triggered.connect(self._show_about_dialog)
        act_view_logs = QAction("View Logs", self)
        act_view_logs.triggered.connect(self._show_log_viewer)
        help_menu.addAction(act_view_logs)

    def _build_ocio_submenu(self):
        self.ocio_menu.clear()
        
        # 1. Input color spaces list
        self.input_space_menu = self.ocio_menu.addMenu("Input Color Space")
        for cs in self.ocio_manager.get_colorspaces():
            act = QAction(cs, self)
            act.setCheckable(True)
            act.setChecked(cs == self.ocio_manager.input_colorspace)
            act.triggered.connect(lambda checked=False, name=cs: self._set_input_colorspace(name))
            self.input_space_menu.addAction(act)
            
        # 2. Look list
        self.look_menu = self.ocio_menu.addMenu("Look")
        for look in self.ocio_manager.get_looks():
            act = QAction(look, self)
            act.setCheckable(True)
            act.setChecked(look == (self.ocio_manager.look or "None (Bypass)"))
            act.triggered.connect(lambda checked=False, name=look: self._set_look(name))
            self.look_menu.addAction(act)
            
        self.look_menu.addSeparator()
        act_load_lut = QAction("Load Custom LUT...", self)
        act_load_lut.triggered.connect(self._load_custom_lut)
        self.look_menu.addAction(act_load_lut)
        
        # 3. Display Output list
        self.display_output_menu = self.ocio_menu.addMenu("Display Output")
        for d in self.ocio_manager.get_display_outputs():
            act = QAction(d, self)
            act.setCheckable(True)
            act.setChecked(d == self.ocio_manager.display_output)
            act.triggered.connect(lambda checked=False, name=d: self._set_display_output(name))
            self.display_output_menu.addAction(act)

        self._populate_look_combo()

    def _setup_hotkeys(self):
        # Frame stepping
        self._add_shortcut("Left", lambda: self.seek_to_frame(self.current_frame - 1))
        self._add_shortcut("Right", lambda: self.seek_to_frame(self.current_frame + 1))
        self._add_shortcut("Shift+Left", lambda: self.seek_to_frame(self.current_frame - 10))
        self._add_shortcut("Shift+Right", lambda: self.seek_to_frame(self.current_frame + 10))
        
        # In / Out markers set via [ and ] keys
        self._add_shortcut("[", self._set_in_point_here)
        self._add_shortcut("]", self._set_out_point_here)
        
        # A/B comparisons
        self._add_shortcut("1", lambda: self._toggle_comparison_slot(0))
        self._add_shortcut("2", lambda: self._toggle_comparison_slot(1))
        
        # Play/Pause
        self._add_shortcut("Space", self.toggle_playback)

        # Frame / timecode display toggle
        self._add_shortcut("T", self._toggle_timecode_display)
        
        # Channel views toggles
        self._add_shortcut("R", lambda: self.toggle_channel_mask(1))
        self._add_shortcut("G", lambda: self.toggle_channel_mask(2))
        self._add_shortcut("B", lambda: self.toggle_channel_mask(3))
        self._add_shortcut("A", lambda: self.toggle_channel_mask(4))

    def _add_shortcut(self, key_str: str, callback):
        action = QAction(self)
        action.setShortcut(QKeySequence(key_str))
        action.triggered.connect(callback)
        self.addAction(action)

    def _make_transport_button(self, label: str, font: QFont) -> QPushButton:
        btn = QPushButton(label)
        btn.setFixedSize(36, 28)
        btn.setFocusPolicy(Qt.NoFocus)
        btn.setFont(font)
        return btn

    def _update_playback_buttons(self):
        if not hasattr(self, "btn_play_forward"):
            return
        self.btn_play_forward.blockSignals(True)
        self.btn_play_reverse.blockSignals(True)
        self.btn_play_forward.setChecked(self.playing and self.playback_direction == 1)
        self.btn_play_reverse.setChecked(self.playing and self.playback_direction == -1)
        self.btn_play_forward.blockSignals(False)
        self.btn_play_reverse.blockSignals(False)

    def _toggle_playback_direction(self, direction: int):
        if self.playing and self.playback_direction == direction:
            self.stop_playback()
        else:
            self.playback_direction = direction
            self.start_playback()

    def _populate_look_combo(self):
        if not hasattr(self, "look_combo") or self.look_combo is None:
            return
        self.look_combo.blockSignals(True)
        self.look_combo.clear()
        self.look_combo.addItems(self.ocio_manager.get_looks())
        current = self.ocio_manager.look or "None (Bypass)"
        idx = self.look_combo.findText(current)
        if idx >= 0:
            self.look_combo.setCurrentIndex(idx)
        self.look_combo.blockSignals(False)
        self.look_combo.refresh_popup_geometry()

    def _sync_look_combo(self, name: str):
        if not hasattr(self, "look_combo") or self.look_combo is None:
            return
        self.look_combo.blockSignals(True)
        idx = self.look_combo.findText(name)
        if idx >= 0:
            self.look_combo.setCurrentIndex(idx)
        self.look_combo.blockSignals(False)

    def _set_compare_mode(self, mode: int):
        self.viewport.set_compare_mode(mode)
        if hasattr(self, "compare_actions"):
            for m, act in self.compare_actions:
                act.setChecked(m == mode)

    def _pixel_aspect_for_mode(self, mode: str) -> float:
        if mode == "anamorphic":
            return ANAMORPHIC_PIXEL_ASPECT
        return SQUARE_PIXEL_ASPECT

    def _pixel_aspect_mode_for_value(self, par: float) -> str:
        if abs(par - ANAMORPHIC_PIXEL_ASPECT) < abs(par - SQUARE_PIXEL_ASPECT):
            return "anamorphic"
        return "square"

    def _set_pixel_aspect_mode(self, mode: str):
        self.pixel_aspect_mode = mode
        self.viewport.set_pixel_aspect_ratio(self._pixel_aspect_for_mode(mode))
        if hasattr(self, "pixel_aspect_actions"):
            for m, act in self.pixel_aspect_actions:
                act.setChecked(m == mode)

    def _apply_file_pixel_aspect_ratio(self, par: float):
        self.file_pixel_aspect_ratio = par if par > 0.0 else SQUARE_PIXEL_ASPECT
        mode = self._pixel_aspect_mode_for_value(self.file_pixel_aspect_ratio)
        self.pixel_aspect_mode = mode
        self.viewport.set_pixel_aspect_ratio(self.file_pixel_aspect_ratio)
        if hasattr(self, "pixel_aspect_actions"):
            for m, act in self.pixel_aspect_actions:
                act.setChecked(m == mode)

    def _open_file_dialog(self, slot: int):
        path, _ = QFileDialog.getOpenFileName(
            self, f"Load Media for Slot {'A' if slot == 0 else 'B'}",
            "",
            "EXR Images (*.exr);;DPX Images (*.dpx);;QuickTimes (*.mov *.mp4);;All Files (*)"
        )
        if path:
            self.load_media(path, slot)

    def load_media(self, path: str, slot: int):
        self.statusBar().showMessage(f"Loading media: {os.path.basename(path)}...")
        
        # Clear viewport references to prevent holding dangling pointers to old cache buffers
        self.viewport.clear_frames()
        
        # Shutdown existing slot cache
        if self.caches[slot] is not None:
            self.caches[slot].close()
            
        ext = os.path.splitext(path)[1].lower()
        
        try:
            # Instantiate correct decoder based on extension
            if ext == ".exr":
                decoder = EXRDecoder(path)
            elif ext == ".dpx":
                decoder = DPXDecoder(path)
            else:
                decoder = QuickTimeDecoder(path)
                
            self.decoders[slot] = decoder
            self.caches[slot] = CacheEngine(decoder, self.settings)
            
            # Setup sequence attributes from loaded slot 0
            if slot == 0:
                meta = decoder.get_metadata()
                self.fps = meta["fps"]
                self.start_frame = meta.get("start_frame", 0)
                self.end_frame = meta.get("end_frame", meta["frame_count"] - 1)
                self.in_point = self.start_frame
                self.out_point = self.end_frame
                self.current_frame = self.start_frame
                
                self.timeline.set_range(self.start_frame, self.end_frame)
                self.timeline.set_in_out(self.in_point, self.out_point)
                self.timeline.set_display_options(self.settings.timecode_mode, self.fps)
                self._sync_frame_rate_menu(self.fps)
                
                # Populate EXR layers in header combobox if applicable
                self.exr_layer_combo.clear()
                layers = meta.get("layers", [])
                if not layers and isinstance(decoder, EXRDecoder):
                    layers = ["beauty"]
                if layers:
                    self.exr_layer_combo.addItems(layers)
                    self.exr_layer_combo.refresh_popup_geometry()
                    if isinstance(decoder, EXRDecoder):
                        self.active_exr_layer = decoder.active_layer or layers[0]
                        self.viewport.exr_layer_str = self.active_exr_layer
                        layer_idx = self.exr_layer_combo.findText(self.active_exr_layer)
                        if layer_idx >= 0:
                            self.exr_layer_combo.blockSignals(True)
                            self.exr_layer_combo.setCurrentIndex(layer_idx)
                            self.exr_layer_combo.blockSignals(False)
                else:
                    self.exr_layer_combo.addItem("beauty")
                
                # Update resolution label readout outside the image
                w = meta.get("width", 0)
                h = meta.get("height", 0)
                self.lbl_resolution.setText(f"{w}x{h}")

                par = meta.get("pixel_aspect_ratio", SQUARE_PIXEL_ASPECT)
                self._apply_file_pixel_aspect_ratio(par)
                
                # Auto-detect input colorspace from filename/metadata
                detected_cs = self.ocio_manager.detect_input_colorspace(path, meta)
                self.ocio_manager.input_colorspace = detected_cs
                self.viewport.update_ocio_pipeline()
                self._build_ocio_submenu()
                
            self._update_ocio_info_label()
            
            # Perform initial frame read
            self.seek_to_frame(self.current_frame)
            self._update_ui_states()
            
            # Fire plugin event
            for plugin in self.plugins:
                plugin.on_media_loaded(slot, path, decoder.get_metadata())
                
            self.statusBar().showMessage(f"Successfully loaded Slot {'A' if slot == 0 else 'B'}.")
        except Exception as e:
            self.statusBar().showMessage(f"Error loading: {e}")
            print(f"Error: {e}")

    def clear_media(self):
        # 1. Stop playback if active
        self.stop_playback()
        
        # 2. Clear viewport references to prevent holding dangling pointers
        self.viewport.clear_frames()
        
        # 3. Close and delete all decoders and caches
        for slot in range(2):
            if self.caches[slot] is not None:
                self.caches[slot].close()
                self.caches[slot] = None
            self.decoders[slot] = None
            
        # 4. Reset playback parameters
        self.fps = self.settings.default_fps
        self.start_frame = 0
        self.end_frame = 0
        self.in_point = 0
        self.out_point = 0
        self.current_frame = 0
        
        # 5. Reset UI controls
        self.timeline.set_range(0, 0)
        self.timeline.set_in_out(0, 0)
        self.timeline.set_current_frame(0)
        self.timeline.set_cached_frames(set())
        
        self.exr_layer_combo.clear()
        self.exr_layer_combo.addItem("beauty")
        
        self.lbl_resolution.setText("")
        self.lbl_fps.setText(self._format_fps_label(self.fps))
        self.lbl_ocio_info.setText("")
        self._set_pixel_aspect_mode("square")
        self._update_readout_display()
        
        # 6. Redraw viewport and update status
        self.viewport.resolution_str = "0x0"
        self.viewport.update()
        self.statusBar().showMessage("Viewer inputs cleared.")

    def seek_to_frame(self, frame: int):
        # Clip to range
        frame = max(self.start_frame, min(self.end_frame, frame))
        self.current_frame = frame
        
        # Load frame from cache slots
        frame_a = None
        frame_b = None
        
        if self.caches[0] is not None:
            self.caches[0].set_playhead(frame, self.playback_direction)
            frame_a_dict = self.caches[0].get_frame(frame)
            if frame_a_dict:
                frame_a = frame_a_dict["data"]
                tc = frame_a_dict["timecode"] or Timecode.frame_to_timecode(frame, self.fps, 0)
                self.viewport.current_timecode = tc
                self.viewport.set_frame_a(
                    frame_a,
                    frame_a_dict["channels"],
                    frame,
                    tc,
                    self.fps
                )
                
        if self.caches[1] is not None:
            # Sync slot B playhead
            self.caches[1].set_playhead(frame, self.playback_direction)
            frame_b_dict = self.caches[1].get_frame(frame)
            if frame_b_dict:
                frame_b = frame_b_dict["data"]
                self.viewport.set_frame_b(frame_b)
                
        # Draw caching blocks in timeline
        if self.caches[self.active_slot] is not None:
            self.timeline.set_cached_frames(self.caches[self.active_slot].get_cached_frames())
            
        self.timeline.set_current_frame(frame)
        
        # Update UI readouts
        if hasattr(self, "lbl_fps") and self.lbl_fps:
            self.lbl_fps.setText(self._format_fps_label(self.fps))
        self._update_readout_display()
            
        # Fire plugin event
        for plugin in self.plugins:
            plugin.on_frame_changed(frame, self.viewport.current_timecode)

    def _playback_tick(self):
        next_frame = self.current_frame + self.playback_direction
        
        # Bounds and looping behavior
        if self.playback_direction > 0 and next_frame > self.out_point:
            if self.settings.loop_mode == "loop":
                next_frame = self.in_point
            elif self.settings.loop_mode == "bounce":
                self.playback_direction = -1
                next_frame = self.out_point - 1
            else:  # once
                self.stop_playback()
                return
        elif self.playback_direction < 0 and next_frame < self.in_point:
            if self.settings.loop_mode == "loop":
                next_frame = self.out_point
            elif self.settings.loop_mode == "bounce":
                self.playback_direction = 1
                next_frame = self.in_point + 1
            else:  # once
                self.stop_playback()
                return
                
        self.seek_to_frame(next_frame)

    def toggle_playback(self):
        if self.playing:
            self.stop_playback()
        else:
            self.start_playback()

    def start_playback(self):
        if self.timer.isActive():
            return
        self.playing = True
        self._update_playback_buttons()

        # Match rate timer interval (ms)
        interval_ms = int(1000.0 / self.fps)
        self.timer.start(interval_ms)

    def stop_playback(self):
        self.playing = False
        self.timer.stop()
        self._update_playback_buttons()

    # In / Out controls
    def _set_in_point_here(self):
        self.in_point = self.current_frame
        self.timeline.set_in_out(self.in_point, self.out_point)
        if self.caches[0]:
            self.caches[0].set_playback_range(self.in_point, self.out_point)
        if self.caches[1]:
            self.caches[1].set_playback_range(self.in_point, self.out_point)

    def _set_out_point_here(self):
        self.out_point = self.current_frame
        self.timeline.set_in_out(self.in_point, self.out_point)
        if self.caches[0]:
            self.caches[0].set_playback_range(self.in_point, self.out_point)
        if self.caches[1]:
            self.caches[1].set_playback_range(self.in_point, self.out_point)

    def _on_in_out_changed(self, in_pt, out_pt):
        self.in_point = in_pt
        self.out_point = out_pt
        if self.caches[0]:
            self.caches[0].set_playback_range(in_pt, out_pt)
        if self.caches[1]:
            self.caches[1].set_playback_range(in_pt, out_pt)

    def _toggle_comparison_slot(self, slot: int):
        self.active_slot = slot
        self.statusBar().showMessage(f"Viewing active Slot {'A' if slot == 0 else 'B'}")
        self.seek_to_frame(self.current_frame)

    def _toggle_timecode_display(self):
        self._set_timecode_display_mode(not self.settings.timecode_mode)

    def _set_timecode_display_mode(self, show_timecode: bool):
        self.settings.timecode_mode = show_timecode
        self.settings.save()
        self.timeline.set_display_options(self.settings.timecode_mode, self.fps)
        self._update_readout_display()
        if hasattr(self, "timecode_display_actions"):
            for mode, act in self.timecode_display_actions:
                act.setChecked(mode == show_timecode)

    def _update_readout_display(self):
        if not hasattr(self, "lbl_frame") or self.lbl_frame is None:
            return
        self.lbl_frame.setText(
            Timecode.format_position_label(
                self.current_frame, self.settings.timecode_mode, self.fps
            )
        )

    def _set_loop_mode(self, mode: str):
        self.settings.loop_mode = mode
        self.settings.save()
        if hasattr(self, "loop_mode_actions"):
            for m, act in self.loop_mode_actions:
                act.setChecked(m == mode)

    def _fps_matches(self, a: float, b: float, tol: float = 0.001) -> bool:
        return abs(a - b) < tol

    def _format_fps_label(self, fps: float) -> str:
        if self._fps_matches(fps, 23.976) or self._fps_matches(fps, 29.97):
            return f"FPS: {fps:.3f}"
        return f"FPS: {fps:.2f}"

    def _sync_frame_rate_menu(self, fps: float):
        if not hasattr(self, "frame_rate_actions"):
            return
        for preset_fps, act in self.frame_rate_actions:
            act.setChecked(self._fps_matches(fps, preset_fps))

    def _set_playback_fps(self, fps: float):
        fps = max(1.0, min(120.0, float(fps)))
        self.fps = fps
        self.settings.default_fps = fps
        self.settings.save()

        self.viewport.fps = fps
        if hasattr(self, "lbl_fps") and self.lbl_fps:
            self.lbl_fps.setText(self._format_fps_label(fps))

        self.timeline.set_display_options(self.settings.timecode_mode, self.fps)
        self._update_readout_display()
        self._sync_frame_rate_menu(fps)

        if self.playing:
            self.timer.start(int(1000.0 / self.fps))

    def _prompt_custom_frame_rate(self):
        fps, ok = QInputDialog.getDouble(
            self,
            "Custom Frame Rate",
            "Frames per second:",
            self.fps,
            1.0,
            120.0,
            3,
        )
        if ok:
            self._set_playback_fps(fps)

    def _on_exr_layer_changed(self, index):
        if self.exr_layer_combo.count() == 0:
            return
        layer = self.exr_layer_combo.currentText()
        self.active_exr_layer = layer
        self.viewport.exr_layer_str = layer
        decoder = self.decoders[0]
        if isinstance(decoder, EXRDecoder):
            decoder.active_layer = layer
            if self.caches[0] is not None:
                self.caches[0].clear()
            self.seek_to_frame(self.current_frame)
        else:
            self.viewport.update()

    def _on_look_combo_changed(self, text: str):
        if text and text != (self.ocio_manager.look or "None (Bypass)"):
            self._set_look(text)

    def _on_wipe_moved(self, pos):
        self.viewport.wipe_pos = pos

    # OCIO sub-menu handlers
    def _set_input_colorspace(self, name):
        self.ocio_manager.input_colorspace = name
        self.viewport.update_ocio_pipeline()
        self._build_ocio_submenu()
        self._update_ocio_info_label()

    def _set_look(self, name):
        self.ocio_manager.set_look(name)
        self.viewport.update_ocio_pipeline()
        self._sync_look_combo(name)
        self._build_ocio_submenu()
        self._update_ocio_info_label()

    def _set_display_output(self, name):
        self.ocio_manager.set_display_output(name)
        self.viewport.update_ocio_pipeline()
        self._build_ocio_submenu()
        self._update_ocio_info_label()

    def _load_custom_lut(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Load Custom LUT (.cube)", "", "LUT Files (*.cube)"
        )
        if path:
            self.ocio_manager.load_custom_lut(path)
            self.viewport.update_ocio_pipeline()
            self._populate_look_combo()
            self._build_ocio_submenu()
            self._update_ocio_info_label()
        
    def _update_ocio_info_label(self):
        if hasattr(self, "lbl_ocio_info") and self.lbl_ocio_info:
            info = f"IN: {self.ocio_manager.input_colorspace} | OUT: {self.ocio_manager.display_output}"
            
            # Append grading info if active
            grade_parts = []
            if self.ocio_manager.grade_exposure != 0.0:
                grade_parts.append(f"Exp: {self.ocio_manager.grade_exposure:+.2f}")
            if self.ocio_manager.grade_gamma != 1.0:
                grade_parts.append(f"Gam: {self.ocio_manager.grade_gamma:.2f}")
            if self.ocio_manager.grade_offset != 0.0:
                grade_parts.append(f"Off: {self.ocio_manager.grade_offset:+.3f}")
                
            if grade_parts:
                info += f" | GRADE: {', '.join(grade_parts)}"
                
            self.lbl_ocio_info.setText(info)

    def _activate_exposure_mode(self):
        self.viewport.adjustment_mode = 'exposure'
        self.viewport.adjust_start_value = self.ocio_manager.grade_exposure
        self.viewport.setCursor(Qt.SizeHorCursor)
        self.viewport.update()
        self.statusBar().showMessage("Interactive adjustment: Left-drag mouse horizontally to adjust EXPOSURE")

    def _activate_gamma_mode(self):
        self.viewport.adjustment_mode = 'gamma'
        self.viewport.adjust_start_value = self.ocio_manager.grade_gamma
        self.viewport.setCursor(Qt.SizeHorCursor)
        self.viewport.update()
        self.statusBar().showMessage("Interactive adjustment: Left-drag mouse horizontally to adjust GAMMA")

    def _activate_offset_mode(self):
        self.viewport.adjustment_mode = 'offset'
        self.viewport.adjust_start_value = self.ocio_manager.grade_offset
        self.viewport.setCursor(Qt.SizeHorCursor)
        self.viewport.update()
        self.statusBar().showMessage("Interactive adjustment: Left-drag mouse horizontally to adjust OFFSET")

    def _reset_grade(self):
        self.ocio_manager.grade_exposure = 0.0
        self.ocio_manager.grade_gamma = 1.0
        self.ocio_manager.grade_offset = 0.0
        self.viewport.update_ocio_pipeline()
        self._update_ocio_info_label()
        self.statusBar().showMessage("Color grading parameters reset to defaults.")

    def _show_about_dialog(self):
        from .about_dialog import AboutDialog
        dialog = AboutDialog(self)
        dialog.exec()

    def _show_log_viewer(self):
        from .log_viewer import LogViewerDialog
        dialog = LogViewerDialog(self)
        dialog.exec()

    def toggle_channel_mask(self, mask: int):
        if self.viewport.channel_mask == mask:
            self.viewport.set_channel_mask(0)  # Toggle back to RGB
            active_mask = 0
        else:
            self.viewport.set_channel_mask(mask)
            active_mask = mask
            
        if hasattr(self, "channel_buttons"):
            for val, btn in self.channel_buttons.items():
                btn.setChecked(val == active_mask)

    # Settings and helper dialogs
    def _open_settings_dialog(self):
        dialog = SettingsDialog(self.settings, self)
        if dialog.exec():
            # Apply changes
            self.settings.save()
            # Update cache worker thread sizes
            if self.caches[0]:
                self.caches[0].update_settings()
            if self.caches[1]:
                self.caches[1].update_settings()
            # Reload OCIO if path changed
            self.ocio_manager.load_config(self.settings.ocio_config_path)
            self.viewport.update_ocio_pipeline()
            self._populate_look_combo()
            self._build_ocio_submenu()

    def _update_ui_states(self):
        self._populate_look_combo()
        self._build_ocio_submenu()

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        for url in event.mimeData().urls():
            path = url.toLocalFile()
            if os.path.exists(path):
                self.load_media(path, self.active_slot)
                break

    def closeEvent(self, event):
        # Shutdown caching threads cleanly
        self.timer.stop()
        for cache in self.caches:
            if cache:
                cache.close()
        self.viewport.cleanup()
        event.accept()
