import os
from PySide6.QtWidgets import (QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, 
                             QFileDialog, QMenuBar, QMenu, QPushButton, 
                             QComboBox, QLabel, QDockWidget, QSlider)
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QKeySequence, QFont

from ..core.settings import Settings
from ..core.timecode import Timecode
from ..core.cache import CacheEngine
from ..color.ocio_manager import OCIOManager
from ..decoders.exr_decoder import EXRDecoder
from ..decoders.dpx_decoder import DPXDecoder
from ..decoders.qt_decoder import QuickTimeDecoder
from ..extensions.ocio_api_tool import OcioApiTool


from .viewport import Viewport
from .timeline import Timeline
from .theme import get_viewfinder_stylesheet
from .settings_dialog import SettingsDialog

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Framecycler // VFX Review")
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
        self.fps = 24.0
        
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
        self.exr_layer_combo = QComboBox()
        self.exr_layer_combo.addItem("beauty")
        self.exr_layer_combo.currentIndexChanged.connect(self._on_exr_layer_changed)
        header_layout.addWidget(self.exr_layer_combo)
        
        header_layout.addSpacing(20)
        header_layout.addWidget(QLabel("COMPARE:"))
        self.compare_combo = QComboBox()
        self.compare_combo.addItems(["A Only", "Split Screen", "Difference", "Side-by-Side"])
        self.compare_combo.currentIndexChanged.connect(self.viewport.set_compare_mode)
        header_layout.addWidget(self.compare_combo)
        
        # Resolution label (no "RESO:" text prefix, just width x height)
        lbl_font = QFont("Segoe UI", 10)
        header_layout.addSpacing(20)
        self.lbl_resolution = QLabel("")
        self.lbl_resolution.setFont(lbl_font)
        header_layout.addWidget(self.lbl_resolution)
        
        # IN/OUT colorspace label just to the right of resolution
        header_layout.addSpacing(20)
        self.lbl_ocio_info = QLabel("")
        self.lbl_ocio_info.setFont(lbl_font)
        header_layout.addWidget(self.lbl_ocio_info)
        
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
        readout_layout = QHBoxLayout()
        readout_layout.setContentsMargins(10, 2, 10, 2)
        
        self.lbl_frame = QLabel("FR: 0000")
        self.lbl_fps = QLabel("FPS: 24.00")
        self.lbl_tc = QLabel("TC: 01:00:00:00")
        
        readout_font = QFont("Segoe UI", 11) # Timeline matched slightly bigger font
        self.lbl_frame.setFont(readout_font)
        self.lbl_fps.setFont(readout_font)
        self.lbl_tc.setFont(readout_font)
        
        readout_layout.addWidget(self.lbl_frame)
        readout_layout.addStretch()
        readout_layout.addWidget(self.lbl_fps)
        readout_layout.addStretch()
        readout_layout.addWidget(self.lbl_tc)
        
        main_layout.addLayout(readout_layout)
        main_layout.addWidget(self.timeline)
        
        # Transport controls row
        transport_layout = QHBoxLayout()
        transport_layout.setContentsMargins(10, 0, 10, 5)
        
        self.btn_play = QPushButton("PLAY")
        self.btn_play.clicked.connect(self.toggle_playback)
        self.btn_prev = QPushButton("<")
        self.btn_prev.setMaximumWidth(30)
        self.btn_prev.clicked.connect(lambda: self.seek_to_frame(self.current_frame - 1))
        self.btn_next = QPushButton(">")
        self.btn_next.setMaximumWidth(30)
        self.btn_next.clicked.connect(lambda: self.seek_to_frame(self.current_frame + 1))
        
        self.combo_loop = QComboBox()
        self.combo_loop.addItems(["LOOP", "BOUNCE", "ONCE"])
        self.combo_loop.setCurrentText(self.settings.loop_mode.upper())
        self.combo_loop.currentTextChanged.connect(self._on_loop_mode_changed)
        
        self.btn_tc_toggle = QPushButton("TC / FR")
        self.btn_tc_toggle.clicked.connect(self.toggle_timecode_mode)
        
        # Play/loop buttons in the center, and tc toggle on the right
        transport_layout.addStretch()
        transport_layout.addWidget(self.btn_prev)
        transport_layout.addWidget(self.btn_play)
        transport_layout.addWidget(self.btn_next)
        transport_layout.addWidget(self.combo_loop)
        transport_layout.addStretch()
        transport_layout.addWidget(self.btn_tc_toggle)
        
        main_layout.addLayout(transport_layout)
        
        # Build collapsible CDL Panel
        self._build_cdl_dock()
        
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
        
        # Plugins menu
        plugins_menu = menubar.addMenu("&Plugins")
        for plugin in self.plugins:
            for action in plugin.get_menu_actions():
                plugins_menu.addAction(action)
                
        # Tools menu
        tools_menu = menubar.addMenu("&Tools")
        cdl_toggle_act = self.cdl_dock.toggleViewAction()
        cdl_toggle_act.setText("CDL Color Grading")
        tools_menu.addAction(cdl_toggle_act)
        
        # OCIO Pipeline menu
        self.ocio_menu = menubar.addMenu("&OCIO")
        self._build_ocio_submenu()

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
            
        # 2. Displays list
        self.display_menu = self.ocio_menu.addMenu("Display Device")
        for d in self.ocio_manager.get_displays():
            act = QAction(d, self)
            act.setCheckable(True)
            act.setChecked(d == self.ocio_manager.display)
            act.triggered.connect(lambda checked=False, name=d: self._set_display(name))
            self.display_menu.addAction(act)
            
        # 3. Views list
        self.view_menu = self.ocio_menu.addMenu("View Transform")
        for v in self.ocio_manager.get_views(self.ocio_manager.display):
            act = QAction(v, self)
            act.setCheckable(True)
            act.setChecked(v == self.ocio_manager.view)
            act.triggered.connect(lambda checked=False, name=v: self._set_view_transform(name))
            self.view_menu.addAction(act)

    def _build_cdl_dock(self):
        self.cdl_dock = QDockWidget("CDL Color Grading", self)
        self.cdl_dock.setObjectName("cdl_dock")
        self.cdl_dock.setAllowedAreas(Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea)
        
        panel = QWidget()
        layout = QVBoxLayout(panel)
        
        # Slope slider
        slope_layout = QHBoxLayout()
        slope_layout.addWidget(QLabel("SLOPE (R, G, B)"))
        self.slope_val_label = QLabel("1.00")
        self.slope_val_label.setAlignment(Qt.AlignRight)
        slope_layout.addWidget(self.slope_val_label)
        layout.addLayout(slope_layout)
        
        self.slope_slider = QSlider(Qt.Horizontal)
        self.slope_slider.setRange(0, 200)  # 0.0 to 2.0
        self.slope_slider.setValue(100)
        self.slope_slider.setFocusPolicy(Qt.NoFocus)
        self.slope_slider.valueChanged.connect(self._on_cdl_changed)
        layout.addWidget(self.slope_slider)
        
        # Offset slider
        offset_layout = QHBoxLayout()
        offset_layout.addWidget(QLabel("OFFSET"))
        self.offset_val_label = QLabel("0.00")
        self.offset_val_label.setAlignment(Qt.AlignRight)
        offset_layout.addWidget(self.offset_val_label)
        layout.addLayout(offset_layout)
        
        self.offset_slider = QSlider(Qt.Horizontal)
        self.offset_slider.setRange(-100, 100)  # -1.0 to 1.0
        self.offset_slider.setValue(0)
        self.offset_slider.setFocusPolicy(Qt.NoFocus)
        self.offset_slider.valueChanged.connect(self._on_cdl_changed)
        layout.addWidget(self.offset_slider)
        
        # Power slider
        power_layout = QHBoxLayout()
        power_layout.addWidget(QLabel("POWER"))
        self.power_val_label = QLabel("1.00")
        self.power_val_label.setAlignment(Qt.AlignRight)
        power_layout.addWidget(self.power_val_label)
        layout.addLayout(power_layout)
        
        self.slope_power = QSlider(Qt.Horizontal)
        self.slope_power.setRange(10, 300)  # 0.1 to 3.0
        self.slope_power.setValue(100)
        self.slope_power.setFocusPolicy(Qt.NoFocus)
        self.slope_power.valueChanged.connect(self._on_cdl_changed)
        layout.addWidget(self.slope_power)
        
        # Saturation slider
        sat_layout = QHBoxLayout()
        sat_layout.addWidget(QLabel("SATURATION"))
        self.sat_val_label = QLabel("1.00")
        self.sat_val_label.setAlignment(Qt.AlignRight)
        sat_layout.addWidget(self.sat_val_label)
        layout.addLayout(sat_layout)
        
        self.sat_slider = QSlider(Qt.Horizontal)
        self.sat_slider.setRange(0, 200)  # 0.0 to 2.0
        self.sat_slider.setValue(100)
        self.sat_slider.setFocusPolicy(Qt.NoFocus)
        self.sat_slider.valueChanged.connect(self._on_cdl_changed)
        layout.addWidget(self.sat_slider)
        
        # Home button to reset CDL
        self.btn_home = QPushButton("Home")
        self.btn_home.setFocusPolicy(Qt.NoFocus)
        self.btn_home.clicked.connect(self._reset_cdl)
        layout.addWidget(self.btn_home)
        
        layout.addStretch()
        self.cdl_dock.setWidget(panel)
        self.addDockWidget(Qt.RightDockWidgetArea, self.cdl_dock)
        self.cdl_dock.setVisible(False)

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
        
        # Timecode display toggle
        self._add_shortcut("T", self.toggle_timecode_mode)
        
        # CDL Reset
        self._add_shortcut("Home", self._reset_cdl)
        
        # Channel views toggles
        self._add_shortcut("R", lambda: self.toggle_channel_mask(1))
        self._add_shortcut("G", lambda: self.toggle_channel_mask(2))
        self._add_shortcut("B", lambda: self.toggle_channel_mask(3))
        self._add_shortcut("A", lambda: self.toggle_channel_mask(4))
        
        # CDL Interactive Adjustments
        self._add_shortcut("P", lambda: self.activate_adjustment_mode('power'))
        self._add_shortcut("O", lambda: self.activate_adjustment_mode('offset'))
        self._add_shortcut("S", lambda: self.activate_adjustment_mode('slope'))
        self._add_shortcut("Shift+S", lambda: self.activate_adjustment_mode('saturation'))

    def _add_shortcut(self, key_str: str, callback):
        action = QAction(self)
        action.setShortcut(QKeySequence(key_str))
        action.triggered.connect(callback)
        self.addAction(action)

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
                
                # Populate EXR layers in header combobox if applicable
                self.exr_layer_combo.clear()
                layers = set()
                for chan in meta.get("channels", []):
                    if "." in chan:
                        layers.add(chan.split(".")[0])
                    else:
                        layers.add("beauty")
                self.exr_layer_combo.addItems(sorted(list(layers)))
                
                # Update resolution label readout outside the image
                w = meta.get("width", 0)
                h = meta.get("height", 0)
                self.lbl_resolution.setText(f"{w}x{h}")
                
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
                self.viewport.current_timecode = frame_a_dict["timecode"]
                self.viewport.set_frame_a(
                    frame_a,
                    frame_a_dict["channels"],
                    frame,
                    frame_a_dict["timecode"],
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
        if hasattr(self, "lbl_frame") and self.lbl_frame:
            self.lbl_frame.setText(f"FR: {frame:04d}")
        if hasattr(self, "lbl_fps") and self.lbl_fps:
            self.lbl_fps.setText(f"FPS: {self.fps:.2f}")
        if hasattr(self, "lbl_tc") and self.lbl_tc:
            self.lbl_tc.setText(f"TC: {self.viewport.current_timecode}")
            
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
        self.btn_play.setText("PAUSE")
        
        # Match rate timer interval (ms)
        interval_ms = int(1000.0 / self.fps)
        self.timer.start(interval_ms)

    def stop_playback(self):
        self.playing = False
        self.btn_play.setText("PLAY")
        self.timer.stop()

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

    def toggle_timecode_mode(self):
        self.settings.timecode_mode = not self.settings.timecode_mode
        self.settings.save()
        self.timeline.update()
        self.seek_to_frame(self.current_frame)

    def _on_loop_mode_changed(self, text):
        self.settings.loop_mode = text.lower()
        self.settings.save()

    def _on_exr_layer_changed(self, index):
        if self.exr_layer_combo.count() > 0:
            self.viewport.exr_layer_str = self.exr_layer_combo.currentText()
            self.viewport.update()

    def _on_wipe_moved(self, pos):
        self.viewport.wipe_pos = pos

    # OCIO sub-menu handlers
    def _set_input_colorspace(self, name):
        self.ocio_manager.input_colorspace = name
        self.viewport.update_ocio_pipeline()
        self._build_ocio_submenu()
        self._update_ocio_info_label()

    def _set_display(self, name):
        self.ocio_manager.display = name
        self.ocio_manager.view = self.ocio_manager.get_views(name)[0]
        self.viewport.update_ocio_pipeline()
        self._build_ocio_submenu()
        self._update_ocio_info_label()

    def _set_view_transform(self, name):
        self.ocio_manager.view = name
        self.viewport.update_ocio_pipeline()
        self._build_ocio_submenu()
        self._update_ocio_info_label()
        
    def _update_ocio_info_label(self):
        if hasattr(self, "lbl_ocio_info") and self.lbl_ocio_info:
            info = f"IN: {self.ocio_manager.input_colorspace} | OUT: {self.ocio_manager.display} ({self.ocio_manager.view})"
            self.lbl_ocio_info.setText(info)

    # CDL value slot adjustments
    def _on_cdl_changed(self):
        slope = self.slope_slider.value() / 100.0
        offset = self.offset_slider.value() / 100.0
        power = self.slope_power.value() / 100.0
        sat = self.sat_slider.value() / 100.0
        
        if hasattr(self, "slope_val_label"):
            self.slope_val_label.setText(f"{slope:.2f}")
        if hasattr(self, "offset_val_label"):
            self.offset_val_label.setText(f"{offset:.2f}")
        if hasattr(self, "power_val_label"):
            self.power_val_label.setText(f"{power:.2f}")
        if hasattr(self, "sat_val_label"):
            self.sat_val_label.setText(f"{sat:.2f}")
            
        self.ocio_manager.set_cdl(
            slope=[slope, slope, slope],
            offset=[offset, offset, offset],
            power=[power, power, power],
            sat=sat
        )
        self.viewport.update_ocio_pipeline()

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

    def activate_adjustment_mode(self, mode: str):
        if mode == 'slope':
            val = self.slope_slider.value() / 100.0
        elif mode == 'offset':
            val = self.offset_slider.value() / 100.0
        elif mode == 'power':
            val = self.slope_power.value() / 100.0
        elif mode == 'saturation':
            val = self.sat_slider.value() / 100.0
            
        self.viewport.adjustment_mode = mode
        self.viewport.adjust_start_value = val
        self.viewport.adjust_current_value = val
        self.viewport.update()
        self.statusBar().showMessage(f"Interactive adjustment: Drag mouse horizontally to adjust {mode.upper()}")

    def _reset_cdl(self):
        self.slope_slider.setValue(100)
        self.offset_slider.setValue(0)
        self.slope_power.setValue(100)
        self.sat_slider.setValue(100)

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
            self._build_ocio_submenu()

    def _update_ui_states(self):
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
