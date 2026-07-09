import os
import sys
from PySide6.QtWidgets import (QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
                             QFileDialog, QMenuBar, QMenu, QPushButton, 
                             QComboBox, QLabel, QDockWidget, QSlider, QInputDialog, QSplitter)
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QAction, QKeySequence, QFont

from ..core.settings import Settings
from ..core.timecode import Timecode
from ..core.cache import CacheEngine
from ..core.media_source import (
    MediaSource,
    decoder_frame_for_source,
    decoder_frame_to_local_index,
    global_to_local,
    local_frame_for_source,
    local_playback_range,
    rebuild_timeline_offsets,
)
from ..color.ocio_manager import OCIOManager
from ..decoders.exr_decoder import EXRDecoder
from ..decoders.dpx_decoder import DPXDecoder
from ..decoders.qt_decoder import QuickTimeDecoder
from ..decoders.image_io import ANAMORPHIC_PIXEL_ASPECT, SQUARE_PIXEL_ASPECT
from ..extensions.ocio_api_tool import OcioApiTool


from .viewport import ViewportContainer, COMPARE_SEQUENCE
from .timeline import Timeline
from .theme import get_viewfinder_stylesheet
from .settings_dialog import SettingsDialog
from .widgets import WideComboBox, add_menu_section
from .fonts import ui_font
from .source_list_panel import SourceListPanel

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

PRESET_RESOLUTION_SCALES = [
    ("1", 1.0),
    ("0.5", 0.5),
    ("0.25", 0.25),
]

class MainWindow(QMainWindow):
    # Emitted from CacheEngine worker threads when a decoded frame becomes available.
    # Qt automatically marshals this across threads to the GUI thread via a queued
    # connection, since a plain QTimer.singleShot() called from a non-Qt worker
    # thread never fires (that thread has no running event loop).
    _frame_ready_signal = Signal(int, int)

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
        self.sources: list[MediaSource] = []
        self.active_source_index = 0
        self._resolution_source_index = 0
        
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
        self.source_width = 0
        self.source_height = 0
        
        # Dynamic menus references
        self.exr_layer_combo = None
        self.input_space_menu = None
        self.display_menu = None
        self.view_menu = None
        
        # Playback timer
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._playback_tick)

        # Timeline cache indicator refresh (throttled during playback)
        self._cached_frames_timer = QTimer(self)
        self._cached_frames_timer.setInterval(250)
        self._cached_frames_timer.timeout.connect(self._refresh_timeline_cached_frames)
        
        # Plugins registration
        self.plugins = [OcioApiTool(self)]
        for plugin in self.plugins:
            plugin.on_init()
            
        # Build UI layout
        self._init_ui()
        
        # Apply hotkeys
        self._setup_hotkeys()
        
        # Cross-thread bridge: CacheEngine worker threads emit through this signal
        # so Qt can safely queue the update onto the GUI thread.
        self._frame_ready_signal.connect(self._on_cache_frame_ready)

    def _init_ui(self):
        # Create Central Viewport (QRhi surface + HUD overlay as siblings)
        self.viewport_panel = ViewportContainer(self.ocio_manager, self)
        self.viewport = self.viewport_panel.viewport
        self._apply_renderer_cache_settings()
        self.viewport.wipe_changed.connect(self._on_wipe_moved)
        self.viewport.frame_scrubbed.connect(self._on_timeline_scrub)
        self.viewport.zoom_mode_changed.connect(self._sync_zoom_actions)
        
        # Create Custom Timeline
        self.timeline = Timeline(self)
        self.timeline.frame_changed.connect(self._on_timeline_scrub)
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

        self.source_panel = SourceListPanel(self)
        self.source_panel.source_selected.connect(self._on_source_selected)
        self.source_panel.source_removed.connect(self._remove_source)
        self.source_panel.order_changed.connect(self._on_source_order_changed)
        self.source_panel.hide_requested.connect(lambda: self._set_source_panel_visible(False))

        self.viewer_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.viewer_splitter.addWidget(self.source_panel)
        self.viewer_splitter.addWidget(self.viewport_panel)
        self.viewer_splitter.setStretchFactor(0, 0)
        self.viewer_splitter.setStretchFactor(1, 1)
        self._source_panel_sizes = [220, 900]
        self.viewer_splitter.setSizes(self._source_panel_sizes)
        main_layout.addWidget(self.viewer_splitter, stretch=1)
        
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
        
        act_open = QAction("Add Media...", self)
        act_open.triggered.connect(self._open_file_dialog)
        file_menu.addAction(act_open)
        
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
        self.view_menu = view_menu
        act_hud = QAction("Toggle HUD", self)
        act_hud.setShortcut(QKeySequence("Ctrl+H"))
        act_hud.triggered.connect(self.viewport.toggle_hud)
        view_menu.addAction(act_hud)

        add_menu_section(view_menu, "Panels")
        self.act_media_sources = QAction("Media Sources", self)
        self.act_media_sources.setCheckable(True)
        self.act_media_sources.setChecked(True)
        self.act_media_sources.triggered.connect(self._toggle_source_panel)
        view_menu.addAction(self.act_media_sources)

        add_menu_section(view_menu, "Zoom")
        self.zoom_actions = []
        zoom_options = [
            ("Fit to Screen", "fit", "F"),
            ("Actual Size", 100, "1"),
            ("200%", 200, "2"),
            ("300%", 300, "3"),
            ("400%", 400, "4"),
        ]
        for label, mode, shortcut in zoom_options:
            act = QAction(label, self)
            act.setCheckable(True)
            act.setChecked(mode == "fit")
            act.setShortcut(QKeySequence(shortcut))
            act.triggered.connect(lambda checked=False, m=mode: self._set_zoom_mode(m))
            view_menu.addAction(act)
            self.zoom_actions.append((mode, act))

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

        resolution_menu = image_menu.addMenu("Resolution")
        self.resolution_actions = []
        for label, scale in PRESET_RESOLUTION_SCALES:
            act = QAction(label, self)
            act.setCheckable(True)
            act.setChecked(self._scale_matches(1.0, scale))
            act.triggered.connect(lambda checked=False, s=scale: self._set_resolution_scale(s))
            resolution_menu.addAction(act)
            self.resolution_actions.append((scale, act))

        resolution_menu.addSeparator()
        act_custom_resolution = QAction("Custom...", self)
        act_custom_resolution.triggered.connect(self._prompt_custom_resolution)
        resolution_menu.addAction(act_custom_resolution)

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
            ("Sequence", COMPARE_SEQUENCE),
            ("Wipe", 1),
            ("Difference", 2),
            ("Tile", 3),
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
        act_check_updates = QAction("Check for Updates…", self)
        act_check_updates.triggered.connect(self._check_for_updates)
        help_menu.addAction(act_check_updates)
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
        
        # Play/Pause
        self._add_shortcut("Space", self.toggle_playback)

        # Frame / timecode display toggle
        self._add_shortcut("T", self._toggle_timecode_display)
        
        # Channel views toggles
        self._add_shortcut("R", lambda: self.toggle_channel_mask(1))
        self._add_shortcut("G", lambda: self.toggle_channel_mask(2))
        self._add_shortcut("B", lambda: self.toggle_channel_mask(3))
        self._add_shortcut("A", lambda: self.toggle_channel_mask(4))

        # Qt maps "Ctrl" to Command on macOS; "Meta" is the physical Control key.
        self._add_shortcut(
            "Ctrl+Left",
            lambda: self._jump_to_adjacent_clip(-1),
            context=Qt.ShortcutContext.ApplicationShortcut,
        )
        self._add_shortcut(
            "Ctrl+Right",
            lambda: self._jump_to_adjacent_clip(1),
            context=Qt.ShortcutContext.ApplicationShortcut,
        )

    def _add_shortcut(self, key_str: str, callback, context=Qt.ShortcutContext.WindowShortcut):
        action = QAction(self)
        action.setShortcut(QKeySequence(key_str))
        action.setShortcutContext(context)
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

    def _toggle_source_panel(self, checked: bool = False):
        self._set_source_panel_visible(self.act_media_sources.isChecked())

    def _set_source_panel_visible(self, visible: bool):
        if visible == self.source_panel.isVisible():
            if hasattr(self, "act_media_sources"):
                self.act_media_sources.blockSignals(True)
                self.act_media_sources.setChecked(visible)
                self.act_media_sources.blockSignals(False)
            return

        if visible:
            self.source_panel.show()
            self.viewer_splitter.setSizes(self._source_panel_sizes)
        else:
            sizes = self.viewer_splitter.sizes()
            if sizes and sizes[0] > 0:
                self._source_panel_sizes = sizes
            self.source_panel.hide()

        if hasattr(self, "act_media_sources"):
            self.act_media_sources.blockSignals(True)
            self.act_media_sources.setChecked(visible)
            self.act_media_sources.blockSignals(False)

    def _set_compare_mode(self, mode: int):
        self.viewport.set_compare_mode(mode)
        if hasattr(self, "compare_actions"):
            for m, act in self.compare_actions:
                act.setChecked(m == mode)

    def _set_zoom_mode(self, mode):
        if mode == "fit":
            self.viewport.fit_to_screen()
        else:
            self.viewport.set_zoom_percent(mode)
        self._sync_zoom_actions(mode)

    def _sync_zoom_actions(self, mode=None):
        if not hasattr(self, "zoom_actions"):
            return
        if mode is None:
            mode = self.viewport.zoom_mode
        for m, act in self.zoom_actions:
            act.blockSignals(True)
            act.setChecked(m == mode)
            act.blockSignals(False)

    def _pixel_aspect_for_mode(self, mode: str) -> float:
        if mode == "anamorphic":
            return ANAMORPHIC_PIXEL_ASPECT
        return SQUARE_PIXEL_ASPECT

    def _pixel_aspect_mode_for_value(self, par: float) -> str:
        if abs(par - ANAMORPHIC_PIXEL_ASPECT) < abs(par - SQUARE_PIXEL_ASPECT):
            return "anamorphic"
        return "square"

    def _set_pixel_aspect_mode(self, mode: str):
        par = self._pixel_aspect_for_mode(mode)
        if self.sources:
            source_index = self._resolution_source_index
            if 0 <= source_index < len(self.sources):
                self.sources[source_index].pixel_aspect_ratio = par

        self.pixel_aspect_mode = mode
        self.file_pixel_aspect_ratio = par
        self.viewport.set_pixel_aspect_ratio(par)
        if hasattr(self, "pixel_aspect_actions"):
            for m, act in self.pixel_aspect_actions:
                act.setChecked(m == mode)
        self.seek_to_frame(self.current_frame)

    def _sync_pixel_aspect_ui_for_source(self, source_index: int):
        if source_index < 0 or source_index >= len(self.sources):
            return
        source = self.sources[source_index]
        par = source.pixel_aspect_ratio if source.pixel_aspect_ratio > 0.0 else SQUARE_PIXEL_ASPECT
        mode = self._pixel_aspect_mode_for_value(par)
        self.pixel_aspect_mode = mode
        self.file_pixel_aspect_ratio = par
        if self.viewport.compare_mode == COMPARE_SEQUENCE:
            self.viewport.set_pixel_aspect_ratio(par)
        if hasattr(self, "pixel_aspect_actions"):
            for m, act in self.pixel_aspect_actions:
                act.blockSignals(True)
                act.setChecked(m == mode)
                act.blockSignals(False)

    def _sync_display_ui_for_source(self, source_index: int):
        self._sync_resolution_ui_for_source(source_index)
        self._sync_pixel_aspect_ui_for_source(source_index)

    def resizeEvent(self, event):
        super().resizeEvent(event)

    def _open_file_dialog(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Add Media",
            "",
            "EXR Images (*.exr);;DPX Images (*.dpx);;QuickTimes (*.mov *.mp4);;All Files (*)",
        )
        if path:
            self._add_media([path], replace=False)

    def _load_source(self, path: str) -> MediaSource:
        ext = os.path.splitext(path)[1].lower()
        if ext == ".exr":
            decoder = EXRDecoder(path)
        elif ext == ".dpx":
            decoder = DPXDecoder(path)
        else:
            decoder = QuickTimeDecoder(path)

        cache = CacheEngine(decoder, self.settings, resolution_scale=1.0)
        meta = decoder.get_metadata()
        frame_count = int(meta.get("frame_count", 0))
        return MediaSource(
            path=path,
            decoder=decoder,
            cache=cache,
            frame_count=frame_count,
            fps=float(meta.get("fps", self.settings.default_fps)),
            decoder_start_frame=int(meta.get("start_frame", 0)),
            width=int(meta.get("width", 0)),
            height=int(meta.get("height", 0)),
            pixel_aspect_ratio=float(meta.get("pixel_aspect_ratio", SQUARE_PIXEL_ASPECT)),
            metadata=meta,
        )

    def _add_media(self, paths: list[str], *, replace: bool = False):
        valid_paths = [path for path in paths if path and os.path.exists(path)]
        if not valid_paths:
            return

        if replace:
            self._close_all_sources()
            self.viewport.clear_frames()

        loaded = 0
        was_empty = len(self.sources) == 0
        for path in valid_paths:
            self.statusBar().showMessage(f"Loading media: {os.path.basename(path)}...")
            try:
                source = self._load_source(path)
                source_index = len(self.sources)
                source.cache.add_frame_ready_callback(
                    lambda frame_index, s=source_index: self._frame_ready_signal.emit(s, frame_index)
                )
                self.sources.append(source)
                loaded += 1

                for plugin in self.plugins:
                    plugin.on_media_loaded(source_index, path, source.metadata)

                self.statusBar().showMessage(f"Added source: {source.display_name}")
            except Exception as exc:
                self.statusBar().showMessage(f"Error loading: {exc}")
                print(f"Error: {exc}")

        if loaded <= 0:
            return

        self._after_sources_changed(reset_timeline=replace or was_empty)
        self._update_ui_states()

    def _close_all_sources(self):
        for source in self.sources:
            source.cache.close()
        self.sources.clear()

    def _after_sources_changed(self, *, reset_timeline: bool = False):
        total_frames = rebuild_timeline_offsets(self.sources)
        self.viewport.native_renderer.clear_display_cache()
        self.viewport.set_source_count(len(self.sources))
        self.viewport.set_source_labels([source.display_name for source in self.sources])
        
        for index, source in enumerate(self.sources):
            self.viewport.native_renderer.register_cache(index, source.cache.native_cache)
            
        self.source_panel.set_sources(self.sources)

        if reset_timeline:
            self.end_frame = max(0, total_frames - 1)
            self.current_frame = 0
            self.start_frame = 0
            self.in_point = self.start_frame
            self.out_point = self.end_frame
            self.timeline.set_range(self.start_frame, self.end_frame)
            self.timeline.set_in_out(self.in_point, self.out_point)
        elif total_frames > 0:
            self.end_frame = max(0, total_frames - 1)
            self.current_frame = min(self.current_frame, self.end_frame)
            self.out_point = min(self.out_point, self.end_frame)
            self.in_point = min(self.in_point, self.out_point)
            self.timeline.set_range(self.start_frame, self.end_frame)
            self.timeline.set_in_out(self.in_point, self.out_point)

        if self.sources:
            if self.active_source_index >= len(self.sources):
                self.active_source_index = len(self.sources) - 1
            self.source_panel.set_active_index(self.active_source_index)
            self._apply_source_metadata(self.active_source_index)
        else:
            self.active_source_index = 0
            self.lbl_resolution.setText("")
            self.source_width = 0
            self.source_height = 0

        self._sync_all_cache_playback_ranges()
        self.seek_to_frame(self.current_frame)

    def _apply_source_metadata(self, source_index: int):
        if source_index < 0 or source_index >= len(self.sources):
            return
        source = self.sources[source_index]
        meta = source.metadata
        self.fps = source.fps
        self.source_width = source.width
        self.source_height = source.height
        self._sync_display_ui_for_source(source_index)
        self.timeline.set_display_options(self.settings.timecode_mode, self.fps)
        self._sync_frame_rate_menu(self.fps)

        self.exr_layer_combo.clear()
        layers = meta.get("layers", [])
        if not layers and isinstance(source.decoder, EXRDecoder):
            layers = ["beauty"]
        if layers:
            self.exr_layer_combo.addItems(layers)
            self.exr_layer_combo.refresh_popup_geometry()
            if isinstance(source.decoder, EXRDecoder):
                self.active_exr_layer = source.decoder.active_layer or layers[0]
                self.viewport.exr_layer_str = self.active_exr_layer
                layer_idx = self.exr_layer_combo.findText(self.active_exr_layer)
                if layer_idx >= 0:
                    self.exr_layer_combo.blockSignals(True)
                    self.exr_layer_combo.setCurrentIndex(layer_idx)
                    self.exr_layer_combo.blockSignals(False)
        else:
            self.exr_layer_combo.addItem("beauty")

        if source_index == 0 or len(self.sources) == 1:
            detected_cs = self.ocio_manager.detect_input_colorspace(source.path, meta)
            self.ocio_manager.input_colorspace = detected_cs
            self.viewport.update_ocio_pipeline()
            self._build_ocio_submenu()

        self._update_ocio_info_label()

    def _on_source_selected(self, index: int):
        if index < 0 or index >= len(self.sources):
            return
        self.active_source_index = index
        self._apply_source_metadata(index)
        self._refresh_timeline_cached_frames()
        self.statusBar().showMessage(f"Active source: {self.sources[index].display_name}")

    def _remove_source(self, index: int):
        if index < 0 or index >= len(self.sources):
            return
        source = self.sources.pop(index)
        source.cache.close()
        self.viewport.clear_frames()
        self._after_sources_changed()
        self.statusBar().showMessage(f"Removed source: {source.display_name}")

    def _on_source_order_changed(self, paths: list[str]):
        path_to_source = {source.path: source for source in self.sources}
        reordered = [path_to_source[path] for path in paths if path in path_to_source]
        if len(reordered) != len(self.sources):
            return
        self.sources = reordered
        self._after_sources_changed()

    def _sync_all_cache_playback_ranges(self):
        for index, source in enumerate(self.sources):
            local_in, local_out = local_playback_range(
                self.sources, index, self.in_point, self.out_point
            )
            source.cache.set_playback_range(local_in, local_out)

    def clear_media(self):
        self.stop_playback()
        self.viewport.clear_frames()
        self._close_all_sources()

        self.fps = self.settings.default_fps
        self.start_frame = 0
        self.end_frame = 0
        self.in_point = 0
        self.out_point = 0
        self.current_frame = 0
        self.active_source_index = 0

        self.timeline.set_range(0, 0)
        self.timeline.set_in_out(0, 0)
        self.timeline.set_current_frame(0)
        self.timeline.set_cached_frames(set())

        self.exr_layer_combo.clear()
        self.exr_layer_combo.addItem("beauty")
        self.source_panel.set_sources([])

        self.lbl_resolution.setText("")
        self.source_width = 0
        self.source_height = 0
        self.lbl_fps.setText(self._format_fps_label(self.fps))
        self.lbl_ocio_info.setText("")
        self._set_pixel_aspect_mode("square")
        self._update_readout_display()

        self.viewport.resolution_str = "0x0"
        self.viewport.set_source_labels([])
        self.viewport.update()
        self.statusBar().showMessage("Viewer inputs cleared.")

    def _on_timeline_scrub(self, frame: int):
        if self.playing:
            self.stop_playback()
        self.seek_to_frame(frame)

    def seek_to_frame(self, frame: int):
        if not self.sources:
            self.current_frame = 0
            self.timeline.set_current_frame(0)
            return

        frame = max(self.start_frame, min(self.end_frame, frame))
        self.current_frame = frame

        sequence_index, _ = global_to_local(self.sources, frame)
        self.viewport.set_sequence_index(sequence_index)
        self.source_panel.highlight_sequence_index(sequence_index)

        current_source = self.sources[sequence_index]
        self.fps = current_source.fps
        self.source_width = current_source.width
        self.source_height = current_source.height
        self._sync_display_ui_for_source(sequence_index)

        for index, source in enumerate(self.sources):
            decoder_frame = decoder_frame_for_source(self.sources, index, frame)
            source.cache.set_playhead(decoder_frame, self.playback_direction)

        self._apply_cached_frames(frame)

        if not self.playing:
            self._refresh_timeline_cached_frames()

        self.timeline.set_current_frame(frame)

        if hasattr(self, "lbl_fps") and self.lbl_fps:
            self.lbl_fps.setText(self._format_fps_label(self.fps))
        self._update_readout_display()

        for plugin in self.plugins:
            plugin.on_frame_changed(frame, self.viewport.current_timecode)

    def _apply_cached_frames(self, frame: int):
        if not self.sources:
            return

        sequence_index, _ = global_to_local(self.sources, frame)
        needs_update = False
        for index, source in enumerate(self.sources):
            local_frame = local_frame_for_source(self.sources, index, frame)
            decoder_frame = decoder_frame_for_source(self.sources, index, frame)
            frame_dict = source.cache.get_frame(decoder_frame)
            if not frame_dict:
                if index < len(self.viewport.frame_slots):
                    self.viewport.frame_slots[index].cached = False
                needs_update = True
                continue
            tc = frame_dict["timecode"] or Timecode.frame_to_timecode(decoder_frame, source.fps, 0)
            self.viewport.set_frame(
                index,
                frame_dict["width"],
                frame_dict["height"],
                frame_dict["channels"],
                local_frame=local_frame,
                decoder_frame=decoder_frame,
                timecode=tc,
                fps=source.fps,
                pixel_aspect_ratio=source.pixel_aspect_ratio,
                is_primary=(index == sequence_index),
                upload_token=frame_dict.get("upload_token", frame_dict["frame_index"]),
            )
        if needs_update:
            self.viewport.update()

    def _on_cache_frame_ready(self, source_index: int, frame_index: int):
        # Runs on the GUI thread: this slot is only ever invoked via
        # _frame_ready_signal, which Qt queues across threads automatically.
        if not self.sources:
            return
        decoder_at_playhead = decoder_frame_for_source(self.sources, source_index, self.current_frame)
        if frame_index != decoder_at_playhead:
            return
        self._apply_cached_frames(self.current_frame)
        if not self.playing:
            self._refresh_timeline_cached_frames()

    def _refresh_timeline_cached_frames(self):
        if not self.sources:
            self.timeline.set_cached_frames(set())
            return
        cache_index = self.active_source_index
        if cache_index < 0 or cache_index >= len(self.sources):
            cache_index = 0
        active_cache = self.sources[cache_index].cache
        decoder_frame = decoder_frame_for_source(self.sources, cache_index, self.current_frame)
        active_cache.set_playhead(decoder_frame, self.playback_direction)
        cached = active_cache.get_cached_frames()
        source = self.sources[cache_index]
        local_cached = {
            source.timeline_offset + decoder_frame_to_local_index(source, decoder_frame_num)
            for decoder_frame_num in cached
        }
        self.timeline.set_cached_frames(local_cached)

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
        self._cached_frames_timer.start()

    def stop_playback(self):
        self.playing = False
        self.timer.stop()
        self._cached_frames_timer.stop()
        self._refresh_timeline_cached_frames()
        self._update_playback_buttons()

    # In / Out controls
    def _jump_to_adjacent_clip(self, direction: int):
        if not self.sources or direction == 0:
            return

        if self.playing:
            self.stop_playback()

        current_index, _ = global_to_local(self.sources, self.current_frame)
        target_index = current_index + direction
        if target_index < 0 or target_index >= len(self.sources):
            return

        source = self.sources[target_index]
        clip_start = source.timeline_offset
        clip_end = source.timeline_end

        self.in_point = clip_start
        self.out_point = clip_end
        self.timeline.set_in_out(self.in_point, self.out_point)
        self._sync_all_cache_playback_ranges()
        self.seek_to_frame(clip_start)
        self.statusBar().showMessage(f"Clip: {source.display_name}")

    def _set_in_point_here(self):
        self.in_point = self.current_frame
        self.timeline.set_in_out(self.in_point, self.out_point)
        self._sync_all_cache_playback_ranges()

    def _set_out_point_here(self):
        self.out_point = self.current_frame
        self.timeline.set_in_out(self.in_point, self.out_point)
        self._sync_all_cache_playback_ranges()

    def _on_in_out_changed(self, in_pt, out_pt):
        self.in_point = in_pt
        self.out_point = out_pt
        self._sync_all_cache_playback_ranges()

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

    def _scale_matches(self, a: float, b: float, tol: float = 0.001) -> bool:
        return abs(a - b) < tol

    def _current_resolution_scale(self) -> float:
        if not self.sources:
            return 1.0
        index = self._resolution_source_index
        if index < 0 or index >= len(self.sources):
            return 1.0
        return self.sources[index].resolution_scale

    def _sync_resolution_ui_for_source(self, source_index: int):
        if source_index < 0 or source_index >= len(self.sources):
            return
        self._resolution_source_index = source_index
        scale = self.sources[source_index].resolution_scale
        self._sync_resolution_menu(scale)
        self._update_resolution_label()

    def _format_resolution_label(self, source_w: int, source_h: int, scale: float) -> str:
        if source_w <= 0 or source_h <= 0:
            return ""
        if scale >= 1.0 or self._scale_matches(scale, 1.0):
            return f"{source_w}x{source_h}"
        pct = int(round(scale * 100))
        return f"{source_w}x{source_h} @ {pct}%"

    def _update_resolution_label(self):
        if hasattr(self, "lbl_resolution") and self.lbl_resolution is not None:
            self.lbl_resolution.setText(
                self._format_resolution_label(
                    self.source_width,
                    self.source_height,
                    self._current_resolution_scale(),
                )
            )

    def _sync_resolution_menu(self, scale: float):
        if not hasattr(self, "resolution_actions"):
            return
        for preset_scale, act in self.resolution_actions:
            act.setChecked(self._scale_matches(scale, preset_scale))

    def _set_resolution_scale(self, scale: float):
        scale = Settings.clamp_resolution_scale(scale)
        if not self.sources:
            return

        source_index = self._resolution_source_index
        if source_index < 0 or source_index >= len(self.sources):
            return

        source = self.sources[source_index]
        if self._scale_matches(source.resolution_scale, scale):
            return

        source.resolution_scale = scale
        source.cache.set_resolution_scale(scale)
        source.cache.clear()

        self._sync_resolution_menu(scale)
        self._update_resolution_label()
        self.seek_to_frame(self.current_frame)

        pct = int(round(scale * 100))
        self.statusBar().showMessage(
            f"Resolution scale for {source.display_name}: {pct}%"
        )

    def _prompt_custom_resolution(self):
        scale, ok = QInputDialog.getDouble(
            self,
            "Custom Resolution Scale",
            "Scale factor (1.0 = full resolution):",
            self._current_resolution_scale(),
            0.01,
            1.0,
            3,
        )
        if ok:
            self._set_resolution_scale(scale)

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
        if self.exr_layer_combo.count() == 0 or not self.sources:
            return
        layer = self.exr_layer_combo.currentText()
        self.active_exr_layer = layer
        self.viewport.exr_layer_str = layer
        source_index = self.active_source_index
        if source_index < 0 or source_index >= len(self.sources):
            source_index = 0
        decoder = self.sources[source_index].decoder
        if isinstance(decoder, EXRDecoder):
            decoder.active_layer = layer
            self.sources[source_index].cache.clear()
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

    def _check_for_updates(self):
        from ..core.updater import check_for_updates_interactive
        check_for_updates_interactive(self)

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
    def _apply_renderer_cache_settings(self):
        renderer = self.viewport.native_renderer
        renderer.set_display_cache_limit_gb(self.settings.display_cache_limit_gb)
        if self.settings.display_cache_limit_gb <= 0.0:
            renderer.clear_display_cache()

    def _open_settings_dialog(self):
        dialog = SettingsDialog(self.settings, self)
        if dialog.exec():
            # Apply changes
            self.settings.save()
            # Update cache worker thread sizes
            for source in self.sources:
                source.cache.update_settings()
            self._apply_renderer_cache_settings()
            # Reload OCIO if path changed
            self.ocio_manager.load_config(self.settings.ocio_config_path)
            self.viewport.update_ocio_pipeline()
            self._populate_look_combo()
            self._build_ocio_submenu()

    def _update_ui_states(self):
        self._populate_look_combo()
        self._build_ocio_submenu()

    def closeEvent(self, event):
        self.timer.stop()
        self._close_all_sources()
        self.viewport.cleanup()
        event.accept()
