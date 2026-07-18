import os
import sys
import time
from PySide6.QtWidgets import (QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
                             QFileDialog, QMenuBar, QMenu, QPushButton, 
                             QComboBox, QLabel, QDockWidget, QSlider, QInputDialog, QSplitter)
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QAction, QKeySequence, QFont

from ..core.settings import Settings
from ..core.timecode import Timecode
from ..core.session import Session
from ..core import otio_model
from ..core.media_source import MediaSource, decoder_frame_to_local_index
from ..core.playback_timing import (
    PLAYBACK_TIMING_EVERY_FRAME,
    PLAYBACK_TIMING_REALTIME,
    advance_playback,
    every_frame_can_advance,
    normalize_playback_timing,
    realtime_steps,
)
from ..color.ocio_manager import OCIOManager
from ..decoders.exr_decoder import EXRDecoder
from ..decoders.image_io import ANAMORPHIC_PIXEL_ASPECT, SQUARE_PIXEL_ASPECT
from ..packages.manager import PackageManager


from .viewport import ViewportContainer, COMPARE_SEQUENCE
from .timeline import Timeline
from .timeline_editor import TimelineSegmentInfo, TimelineVersionInfo
from .theme import get_viewfinder_stylesheet
from .settings_dialog import SettingsDialog
from .widgets import WideComboBox, add_menu_section
from .fonts import ui_font
from .source_list_panel import SourceListPanel
from .drag_drop_overlay import DragDropOverlay

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
    _frame_ready_signal = Signal(str, int)  # media_path, decoder_frame

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Framecycler Reboot")
        self.resize(1200, 800)
        
        # Load stylesheet
        self.setStyleSheet(get_viewfinder_stylesheet())
        
        # Core engines
        self.settings = Settings()
        self.ocio_manager = OCIOManager(self.settings.ocio_config_path)
        self.session = Session(self.settings)
        self.session.set_changed_callback(self._on_session_changed)
        self.session.set_frame_ready_callback(
            lambda path, frame: self._frame_ready_signal.emit(path, frame)
        )
        self.session.set_input_colorspace_detector(
            self.ocio_manager.detect_input_colorspace
        )
        
        # Playback states
        self.active_shot_index = 0
        self.active_version_index = 0
        self._resolution_source_index = 0
        self._drag_enter_count = 0
        self._drag_drop_zone = DragDropOverlay.ZONE_SEQUENCE
        self._suppress_session_refresh = False
        self._reset_timeline_on_next_change = False
        self._applied_cdl_key: str | None = None
        self._applied_input_colorspace_key: str | None = None

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
        self._playback_anchor_time = 0.0
        self._playback_anchor_frame = 0
        self._playback_anchor_direction = 1
        
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
        
        self.package_manager = PackageManager(self, config_dir=self.settings.config_dir)

        # Build UI layout
        self._init_ui()
        
        # Apply hotkeys
        self._setup_hotkeys()
        
        # Cross-thread bridge: CacheEngine worker threads emit through this signal
        # so Qt can safely queue the update onto the GUI thread.
        self._frame_ready_signal.connect(self._on_cache_frame_ready)
        
        # Check renderer initialization status on startup
        QTimer.singleShot(500, self._check_renderer_status)

    @property
    def sources(self) -> list:
        """Compatibility: online media sources currently in the pool."""
        return self.session.all_online_sources()

    def _init_ui(self):
        # Create Central Viewport (QRhi surface + HUD overlay as siblings)
        self.viewport_panel = ViewportContainer(self.ocio_manager, self)
        self.viewport = self.viewport_panel.viewport
        self._apply_renderer_cache_settings()
        self.viewport.wipe_changed.connect(self._on_wipe_moved)
        self.viewport.frame_scrubbed.connect(self._on_timeline_scrub)
        self.viewport.zoom_mode_changed.connect(self._sync_zoom_actions)
        
        # NLE timeline editor
        self.timeline = Timeline(self)
        self.timeline.frame_changed.connect(self._on_timeline_scrub)
        self.timeline.in_out_changed.connect(self._on_in_out_changed)
        self.timeline.active_version_changed.connect(self._on_active_version_changed)
        self.timeline.shots_reordered.connect(self._on_shot_order_changed)
        self.timeline.shot_trimmed.connect(self._on_shot_trimmed)

        # Central layout: viewer column + resizable timeline pane
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)
        main_layout.setContentsMargins(5, 5, 5, 5)
        main_layout.setSpacing(2)

        viewer_column = QWidget()
        viewer_layout = QVBoxLayout(viewer_column)
        viewer_layout.setContentsMargins(0, 0, 0, 0)
        viewer_layout.setSpacing(2)

        # Viewport Header controls row
        header_layout = QHBoxLayout()
        header_layout.setContentsMargins(10, 2, 10, 2)

        header_layout.addWidget(QLabel("LAYER:"))
        self.exr_layer_combo = WideComboBox(min_popup_width=240)
        self.exr_layer_combo.addItem("beauty")
        self.exr_layer_combo.currentIndexChanged.connect(self._on_exr_layer_changed)
        header_layout.addWidget(self.exr_layer_combo)

        lbl_font = ui_font(10)
        header_layout.addSpacing(20)
        self.lbl_resolution = QLabel("")
        self.lbl_resolution.setFont(lbl_font)
        header_layout.addWidget(self.lbl_resolution)

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

        viewer_layout.addLayout(header_layout)

        self.source_panel = SourceListPanel(self)
        self.source_panel.shot_selected.connect(self._on_shot_selected)
        self.source_panel.shot_removed.connect(self._remove_shot)
        self.source_panel.version_removed.connect(self._remove_version)
        self.source_panel.active_version_changed.connect(self._on_active_version_changed)
        self.source_panel.order_changed.connect(self._on_shot_order_changed)
        self.source_panel.hide_requested.connect(lambda: self._set_source_panel_visible(False))

        self.viewer_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.viewer_splitter.addWidget(self.source_panel)
        self.viewer_splitter.addWidget(self.viewport_panel)
        self.viewer_splitter.setStretchFactor(0, 0)
        self.viewer_splitter.setStretchFactor(1, 1)
        self._source_panel_sizes = [220, 900]
        self.viewer_splitter.setSizes(self._source_panel_sizes)
        # Hidden by default; isVisible() is False until the window is shown, so
        # hide() must be called explicitly during construction.
        self.source_panel.hide()
        viewer_layout.addWidget(self.viewer_splitter, stretch=1)

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
        viewer_layout.addWidget(readout_widget)

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
        viewer_layout.addLayout(transport_layout)

        self.main_splitter = QSplitter(Qt.Orientation.Vertical)
        self.main_splitter.addWidget(viewer_column)
        self.main_splitter.addWidget(self.timeline)
        self.main_splitter.setStretchFactor(0, 1)
        self.main_splitter.setStretchFactor(1, 0)
        self.main_splitter.setChildrenCollapsible(False)
        self.timeline.setMinimumHeight(64)
        sizes = getattr(self.settings, "timeline_splitter_sizes", [700, 96])
        self.main_splitter.setSizes(sizes)
        self.main_splitter.splitterMoved.connect(self._on_main_splitter_moved)
        main_layout.addWidget(self.main_splitter, stretch=1)

        self.timeline.set_display_options(self.settings.timecode_mode, self.fps)
        self._update_readout_display()
        self._build_menu()
        self.statusBar().showMessage("Ready.")

    def _build_menu(self):
        menubar = self.menuBar()
        
        # File menu
        file_menu = menubar.addMenu("&File")
        
        act_open = QAction("Add Media...", self)
        act_open.triggered.connect(self._open_file_dialog)
        file_menu.addAction(act_open)

        act_import = QAction("Import Timeline...", self)
        act_import.triggered.connect(self._import_timeline)
        file_menu.addAction(act_import)

        act_export = QAction("Export Timeline...", self)
        act_export.triggered.connect(self._export_timeline)
        file_menu.addAction(act_export)
        
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
        self.act_media_sources = QAction("Shots", self)
        self.act_media_sources.setCheckable(True)
        self.act_media_sources.setChecked(False)
        self.act_media_sources.triggered.connect(self._toggle_source_panel)
        view_menu.addAction(self.act_media_sources)
        self._set_source_panel_visible(False)

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

        add_menu_section(playback_menu, "Timing", first=True)

        self.playback_timing_actions = []
        timing_modes = [
            ("Play Every Frame", PLAYBACK_TIMING_EVERY_FRAME),
            ("Play Realtime", PLAYBACK_TIMING_REALTIME),
        ]
        active_timing = normalize_playback_timing(self.settings.playback_timing)
        for label, mode in timing_modes:
            act = QAction(label, self)
            act.setCheckable(True)
            act.setChecked(mode == active_timing)
            act.triggered.connect(lambda checked=False, m=mode: self._set_playback_timing(m))
            playback_menu.addAction(act)
            self.playback_timing_actions.append((mode, act))

        add_menu_section(playback_menu, "Mode")

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
        
        # Packages menu (populated after packages activate)
        self._plugins_menu = menubar.addMenu("&Plugins")
        self.package_manager.load_enabled()
        self._rebuild_plugins_menu()

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
        elif self.playing:
            self.playback_direction = direction
            self._reset_playback_clock()
            self._update_playback_buttons()
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
        # Use isHidden() (not isVisible()): during __init__ the widget is not yet
        # shown, so isVisible() is False and would skip hide() incorrectly.
        currently_shown = not self.source_panel.isHidden()
        if visible == currently_shown:
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
        source = self._selected_source()
        if source is not None:
            source.pixel_aspect_ratio = par

        self.pixel_aspect_mode = mode
        self.file_pixel_aspect_ratio = par
        self.viewport.set_pixel_aspect_ratio(par)
        if hasattr(self, "pixel_aspect_actions"):
            for m, act in self.pixel_aspect_actions:
                act.setChecked(m == mode)
        self.seek_to_frame(self.current_frame)

    def _sync_pixel_aspect_ui_for_source(self, source):
        if source is None:
            return
        if isinstance(source, int):
            # Legacy index call — resolve selected source
            source = self._selected_source()
            if source is None:
                return
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

    def _sync_display_ui_for_source(self, source):
        self._sync_resolution_ui_for_source(source)
        self._sync_pixel_aspect_ui_for_source(source)

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
            self._add_media([path], mode="sequence")

    def _import_timeline(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Import Timeline",
            "",
            "OpenTimelineIO (*.otio *.otiod *.otioz);;All Files (*)",
        )
        if not path:
            return
        try:
            self.stop_playback()
            self.viewport.clear_frames()
            self._reset_timeline_on_next_change = True
            self.session.import_timeline(path)
            self.statusBar().showMessage(f"Imported timeline: {os.path.basename(path)}")
            self._update_ui_states()
        except Exception as exc:
            self.statusBar().showMessage(f"Import failed: {exc}")
            print(f"Import error: {exc}")

    def _export_timeline(self):
        if self.session.empty:
            self.statusBar().showMessage("Nothing to export.")
            return
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Export Timeline",
            "session.otio",
            "OpenTimelineIO (*.otio);;All Files (*)",
        )
        if not path:
            return
        if not path.lower().endswith(".otio"):
            path += ".otio"
        try:
            self.session.export_timeline(path)
            self.statusBar().showMessage(f"Exported timeline: {os.path.basename(path)}")
        except Exception as exc:
            self.statusBar().showMessage(f"Export failed: {exc}")
            print(f"Export error: {exc}")

    def _add_media(self, paths: list[str], *, mode: str = "sequence", replace: bool | None = None):
        # Back-compat for older call sites that passed replace=
        if replace is not None:
            mode = "replace" if replace else "sequence"
        if mode not in ("replace", "sequence", "stack"):
            mode = "sequence"

        valid_paths = [path for path in paths if path and os.path.exists(path)]
        if not valid_paths:
            return 0

        was_empty = self.session.empty
        if mode == "replace" or was_empty:
            self.viewport.clear_frames()
            self._reset_timeline_on_next_change = True

        self.statusBar().showMessage(f"Loading {len(valid_paths)} media file(s)...")
        try:
            loaded = self.session.add_media(
                valid_paths, mode=mode, playhead_frame=self.current_frame
            )
        except Exception as exc:
            self.statusBar().showMessage(f"Error loading: {exc}")
            print(f"Error: {exc}")
            return 0

        if loaded <= 0:
            errors = getattr(self.session, "_last_add_errors", [])
            if errors:
                self.statusBar().showMessage(f"Error loading: {errors[0]}")
            return 0

        for index, path in enumerate(valid_paths[:loaded]):
            source = self.session.media_pool.get(path)
            meta = source.metadata if source else {}
            self.package_manager.emit_media_loaded(index, path, meta)

        self.statusBar().showMessage(f"Added {loaded} media item(s) ({mode})")
        self._update_ui_states()
        return loaded

    def _close_all_sources(self):
        self.session.clear()

    def _on_session_changed(self):
        if self._suppress_session_refresh:
            return
        reset = self._reset_timeline_on_next_change
        self._reset_timeline_on_next_change = False
        self._refresh_from_session(reset_timeline=reset)
        if hasattr(self, "package_manager"):
            self.package_manager.emit_session_changed()

    def _rebuild_plugins_menu(self):
        menu = getattr(self, "_plugins_menu", None)
        if menu is None:
            return
        menu.clear()
        actions = self.package_manager.menu_actions
        if not actions:
            empty = QAction("No packages enabled", self)
            empty.setEnabled(False)
            menu.addAction(empty)
            return
        for action in actions:
            menu.addAction(action)

    def _refresh_from_session(self, *, reset_timeline: bool = False):
        plan = self.session.plan
        self.viewport.native_renderer.clear_display_cache()

        segment = plan.segment_at(self.current_frame) if not plan.empty else None
        versions = segment.display_versions() if segment is not None else []
        self.viewport.set_source_count(len(versions))
        labels = []
        for index, version in enumerate(versions):
            if version.source is not None and version.source.cache is not None and not version.offline:
                self.viewport.native_renderer.register_cache(index, version.source.cache.native_cache)
                labels.append(version.source.display_name)
            else:
                labels.append(version.clip.name or "offline")
        self.viewport.set_source_labels(labels)

        self.source_panel.set_plan(
            plan,
            selected_shot=self.session.selected_shot_index,
            selected_version=self.session.selected_version_index,
        )

        self._push_timeline_segments(plan)

        if plan.empty:
            self.start_frame = 0
            self.end_frame = 0
            self.current_frame = 0
            self.in_point = 0
            self.out_point = 0
            self.active_shot_index = 0
            self.active_version_index = 0
            self.lbl_resolution.setText("")
            self.source_width = 0
            self.source_height = 0
            self.timeline.set_range(0, 0)
            self.timeline.set_in_out(0, 0)
            self.timeline.set_cached_frames(set(), set())
            self._apply_resolved_cdl(force=True)
            self._apply_resolved_input_colorspace(force=True)
            return

        self.start_frame = plan.global_start
        self.end_frame = plan.global_end
        if reset_timeline:
            self.current_frame = self.start_frame
            self.in_point = self.start_frame
            self.out_point = self.end_frame
        else:
            self.current_frame = max(self.start_frame, min(self.current_frame, self.end_frame))
            self.out_point = max(self.start_frame, min(self.out_point, self.end_frame))
            self.in_point = max(self.start_frame, min(self.in_point, self.out_point))

        self.timeline.set_range(self.start_frame, self.end_frame)
        self.timeline.set_in_out(self.in_point, self.out_point)

        self.active_shot_index = self.session.selected_shot_index
        self.active_version_index = self.session.selected_version_index
        self._apply_selected_metadata()
        self._sync_all_cache_playback_ranges()
        self.seek_to_frame(self.current_frame)

    def _selected_source(self) -> MediaSource | None:
        return self.session.selected_source()

    def _apply_selected_metadata(self):
        source = self._selected_source()
        if source is None:
            return
        meta = source.metadata
        self.fps = self.session.effective_fps(self.current_frame)
        self.source_width = source.width
        self.source_height = source.height
        self._resolution_source_index = 0
        self._sync_display_ui_for_source(source)
        self.timeline.set_display_options(self.settings.timecode_mode, self.fps)
        self._sync_frame_rate_menu(self.fps)

        # Block signals for the whole repopulation: clear()/addItems() would
        # otherwise fire currentIndexChanged → cache.clear() mid-prefetch.
        self.exr_layer_combo.blockSignals(True)
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
                    self.exr_layer_combo.setCurrentIndex(layer_idx)
        else:
            self.exr_layer_combo.addItem("beauty")
        self.exr_layer_combo.blockSignals(False)

        clip = None
        stacks = otio_model.shot_stacks(self.session.timeline)
        if 0 <= self.session.selected_shot_index < len(stacks):
            clips = otio_model.version_clips(stacks[self.session.selected_shot_index])
            if 0 <= self.session.selected_version_index < len(clips):
                clip = clips[self.session.selected_version_index]
        if clip is not None:
            stored_cs = otio_model.get_input_colorspace(clip)
            if stored_cs is None:
                stored_cs = self.session.ensure_clip_input_colorspace(
                    clip, source.path, meta
                )
            if stored_cs:
                self.ocio_manager.input_colorspace = stored_cs
        else:
            detected_cs = self.ocio_manager.detect_input_colorspace(source.path, meta)
            self.ocio_manager.input_colorspace = detected_cs
        self.viewport.update_ocio_pipeline()
        self._applied_input_colorspace_key = (
            f"{self.session.selected_shot_index}:{self.session.selected_version_index}|"
            f"{self.ocio_manager.input_colorspace}"
        )
        self._build_ocio_submenu()
        self._update_ocio_info_label()

    def _on_shot_selected(self, shot_index: int, version_index: int):
        self.session.set_selection(shot_index, version_index)
        self.active_shot_index = shot_index
        self.active_version_index = version_index
        self._apply_selected_metadata()
        self._refresh_timeline_cached_frames()
        source = self._selected_source()
        name = source.display_name if source else f"shot {shot_index}"
        self.statusBar().showMessage(f"Selected: {name}")

    def _on_active_version_changed(self, shot_index: int, version_index: int):
        self.session.set_active_version(shot_index, version_index)
        self.statusBar().showMessage(f"Active version set (shot {shot_index}, v{version_index})")

    def _on_shot_trimmed(self, shot_index: int, source_start: int, duration: int):
        self.session.trim_active_version(shot_index, source_start, duration)
        self.statusBar().showMessage(
            f"Trimmed shot {shot_index}: start={source_start}, duration={duration}"
        )

    def _on_main_splitter_moved(self, *_args):
        sizes = self.main_splitter.sizes()
        if len(sizes) == 2 and sizes[1] > 0:
            self.settings.timeline_splitter_sizes = sizes
            self.settings.save()

    def _push_timeline_segments(self, plan) -> None:
        segments: list[TimelineSegmentInfo] = []
        for seg in plan.segments:
            versions: list[TimelineVersionInfo] = []
            for slot in seg.versions:
                avail_start, avail_count = otio_model.clip_available_range_frames(slot.clip)
                name = (
                    slot.source.display_name
                    if slot.source is not None
                    else (slot.clip.name or "offline")
                )
                versions.append(
                    TimelineVersionInfo(
                        name=name,
                        is_active=slot.is_active,
                        is_compare=slot.is_compare,
                        offline=slot.offline,
                        source_start=otio_model.clip_source_start_frames(slot.clip),
                        duration=otio_model.clip_duration_frames(slot.clip),
                        available_start=avail_start,
                        available_count=avail_count,
                    )
                )
            segments.append(
                TimelineSegmentInfo(
                    index=seg.index,
                    global_start=seg.global_start,
                    global_end=seg.global_end,
                    versions=versions,
                )
            )
        self.timeline.set_segments(segments)

    def _remove_shot(self, shot_index: int):
        self.viewport.clear_frames()
        self.session.remove_shot(shot_index)
        self.statusBar().showMessage(f"Removed shot {shot_index}")

    def _remove_version(self, shot_index: int, version_index: int):
        self.viewport.clear_frames()
        self.session.remove_version(shot_index, version_index)
        self.statusBar().showMessage(f"Removed version {version_index} from shot {shot_index}")

    def _on_shot_order_changed(self, shot_indices: list):
        self.session.reorder_shots(shot_indices)

    def _sync_all_cache_playback_ranges(self):
        plan = self.session.plan
        if plan.empty:
            return
        for segment in plan.adjacent_segments(self.current_frame):
            for version in segment.versions:
                if version.source is None or version.source.cache is None or version.offline:
                    continue
                local_in, local_out = plan.playback_range_for_version(
                    segment, version, self.in_point, self.out_point
                )
                version.source.cache.set_playback_range(local_in, local_out)

    def clear_media(self):
        self.stop_playback()
        self.viewport.clear_frames()
        self._suppress_session_refresh = True
        self.session.clear()
        self._suppress_session_refresh = False

        self.fps = self.settings.default_fps
        self.start_frame = 0
        self.end_frame = 0
        self.in_point = 0
        self.out_point = 0
        self.current_frame = 0
        self.active_shot_index = 0
        self.active_version_index = 0

        self.timeline.set_range(0, 0)
        self.timeline.set_in_out(0, 0)
        self.timeline.set_current_frame(0)
        self.timeline.set_cached_frames(set(), set())
        self.timeline.set_segments([])

        self.exr_layer_combo.clear()
        self.exr_layer_combo.addItem("beauty")
        self.source_panel.set_plan(self.session.plan)

        self.lbl_resolution.setText("")
        self.source_width = 0
        self.source_height = 0
        self.lbl_fps.setText(self._format_fps_label(self.fps))
        self.lbl_ocio_info.setText("")
        self._set_pixel_aspect_mode("square")
        self._update_readout_display()

        self.viewport.resolution_str = "0x0"
        self.viewport.set_source_count(0)
        self.viewport.set_source_labels([])
        self.viewport.native_renderer.clear_display_cache()
        self.viewport.update()
        self.statusBar().showMessage("Viewer inputs cleared.")

    def _on_timeline_scrub(self, frame: int):
        if self.playing:
            self.stop_playback()
        self.seek_to_frame(frame)

    def _apply_resolved_cdl(self, *, force: bool = False) -> None:
        """Apply Clip > Stack > Timeline CDL for the playhead active version."""
        if self.session.empty:
            cache_key = "empty"
            if not force and self._applied_cdl_key == cache_key:
                return
            self._applied_cdl_key = cache_key
            self.ocio_manager.reset_cdl_values()
            if hasattr(self, "viewport") and self.viewport is not None:
                self.viewport.update_ocio_pipeline()
            self._update_ocio_info_label()
            return

        cdl = self.session.resolved_cdl_for_active(self.current_frame)
        loc = self.session.playhead_shot_version(self.current_frame)
        loc_key = f"{loc[0]}:{loc[1]}" if loc else "none"
        cache_key = f"{loc_key}|{otio_model.cdl_cache_key(cdl)}"
        if not force and self._applied_cdl_key == cache_key:
            return
        self._applied_cdl_key = cache_key
        self.ocio_manager.apply_cdl_dict(cdl)
        if hasattr(self, "viewport") and self.viewport is not None:
            self.viewport.update_ocio_pipeline()
        self._update_ocio_info_label()

    def _apply_resolved_input_colorspace(self, *, force: bool = False) -> None:
        """Apply the playhead active clip's stored input colorspace to the viewer."""
        if self.session.empty:
            cache_key = "empty"
            if not force and self._applied_input_colorspace_key == cache_key:
                return
            self._applied_input_colorspace_key = cache_key
            if hasattr(self, "ocio_menu") and self.ocio_menu is not None:
                self._build_ocio_submenu()
            self._update_ocio_info_label()
            return

        loc = self.session.playhead_shot_version(self.current_frame)
        cs = self.session.resolved_input_colorspace_for_active(self.current_frame)
        if cs is None and loc is not None:
            # Lazily detect/store if an older timeline lacks the key
            shot_index, version_index = loc
            stacks = otio_model.shot_stacks(self.session.timeline)
            if 0 <= shot_index < len(stacks):
                clips = otio_model.version_clips(stacks[shot_index])
                if 0 <= version_index < len(clips):
                    clip = clips[version_index]
                    path = otio_model.media_path_from_clip(clip)
                    source = self.session.media_pool.get(path) if path else None
                    meta = source.metadata if source is not None else None
                    if path:
                        cs = self.session.ensure_clip_input_colorspace(clip, path, meta)

        if cs is None:
            return

        loc_key = f"{loc[0]}:{loc[1]}" if loc else "none"
        cache_key = f"{loc_key}|{cs}"
        if (
            not force
            and self._applied_input_colorspace_key == cache_key
            and self.ocio_manager.input_colorspace == cs
        ):
            return
        self._applied_input_colorspace_key = cache_key
        if self.ocio_manager.input_colorspace != cs:
            self.ocio_manager.input_colorspace = cs
            if hasattr(self, "viewport") and self.viewport is not None:
                self.viewport.update_ocio_pipeline()
        self._build_ocio_submenu()
        self._update_ocio_info_label()

    def seek_to_frame(self, frame: int):
        plan = self.session.plan
        if plan.empty:
            self.current_frame = 0
            self.timeline.set_current_frame(0)
            self._apply_resolved_cdl()
            self._apply_resolved_input_colorspace()
            return

        frame = max(self.start_frame, min(self.end_frame, frame))
        self.current_frame = frame

        segment = plan.segment_at(frame)
        if segment is None:
            return

        # Viewport slots = versions of the current shot stack
        versions = segment.display_versions()
        self.viewport.set_source_count(len(versions))
        labels = []
        for index, version in enumerate(versions):
            if version.source is not None and version.source.cache is not None and not version.offline:
                self.viewport.native_renderer.register_cache(index, version.source.cache.native_cache)
                labels.append(version.source.display_name)
            else:
                labels.append(version.clip.name or "offline")
        self.viewport.set_source_labels(labels)

        # Sequence mode shows the active version (always slot 0 in display_versions)
        self.viewport.set_sequence_index(0)
        self.source_panel.highlight_sequence_index(segment.index)

        active = segment.active
        current_source = active.source if active is not None else None
        previous_fps = self.fps
        override = self.session.playback_rate_override()
        if override is not None:
            self.fps = override
        elif current_source is not None:
            self.fps = current_source.fps
        else:
            self.fps = segment.rate

        if current_source is not None:
            self.source_width = current_source.width
            self.source_height = current_source.height
            self._sync_display_ui_for_source(current_source)

        # Prefetch playheads for current + adjacent shot versions
        for adj in plan.adjacent_segments(frame):
            for version in adj.versions:
                if version.source is None or version.source.cache is None or version.offline:
                    continue
                decoder_frame = plan.decoder_frame_for_version(adj, version, frame)
                version.source.cache.set_playhead(decoder_frame, self.playback_direction)

        self._apply_cached_frames(frame)

        if not self.playing:
            self._refresh_timeline_cached_frames()

        self.timeline.set_current_frame(frame)

        if hasattr(self, "lbl_fps") and self.lbl_fps:
            self.lbl_fps.setText(self._format_fps_label(self.fps))
        self._update_readout_display()

        # Restart timer when rate changes mid-playback
        if self.playing and not self._fps_matches(previous_fps, self.fps):
            self.timer.start(int(1000.0 / max(1.0, self.fps)))
            self._reset_playback_clock()

        self._apply_resolved_cdl()
        self._apply_resolved_input_colorspace()

        if hasattr(self, "package_manager"):
            self.package_manager.emit_frame_changed(frame, self.viewport.current_timecode)

    def _apply_cached_frames(self, frame: int):
        plan = self.session.plan
        if plan.empty:
            return
        segment = plan.segment_at(frame)
        if segment is None:
            return

        versions = segment.display_versions()
        local_frame = plan.local_index(segment, frame)
        needs_update = False
        for index, version in enumerate(versions):
            if version.source is None or version.source.cache is None or version.offline:
                if index < len(self.viewport.frame_slots):
                    self.viewport.frame_slots[index].cached = False
                needs_update = True
                continue
            decoder_frame = plan.decoder_frame_for_version(segment, version, frame)
            frame_dict = version.source.cache.get_frame(decoder_frame)
            if not frame_dict:
                if index < len(self.viewport.frame_slots):
                    self.viewport.frame_slots[index].cached = False
                needs_update = True
                continue
            source = version.source
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
                is_primary=version.is_active,
                upload_token=frame_dict.get("upload_token", frame_dict["frame_index"]),
            )
        if needs_update:
            self.viewport.update()

    def _on_cache_frame_ready(self, media_path: str, frame_index: int):
        # Runs on the GUI thread via _frame_ready_signal.
        plan = self.session.plan
        if plan.empty:
            return
        segment = plan.segment_at(self.current_frame)
        if segment is None:
            return
        matched_path = False
        for version in segment.display_versions():
            if version.source is None or version.offline:
                continue
            if os.path.abspath(version.source.path) != os.path.abspath(media_path):
                continue
            matched_path = True
            decoder_at_playhead = plan.decoder_frame_for_version(
                segment, version, self.current_frame
            )
            if frame_index != decoder_at_playhead:
                # Non-playhead decode: wake the renderer so GPU lookahead can
                # upload into the display cache while idle.
                if self.viewport is not None and self.viewport.native_renderer is not None:
                    self.viewport.native_renderer.request_redraw()
                if not self.playing:
                    self._refresh_timeline_cached_frames()
                continue
            self._apply_cached_frames(self.current_frame)
            if not self.playing:
                self._refresh_timeline_cached_frames()
            return
        if matched_path and not self.playing:
            self._refresh_timeline_cached_frames()

    def _refresh_timeline_cached_frames(self):
        plan = self.session.plan
        if plan.empty:
            self.timeline.set_cached_frames(set(), set())
            return
        source = self._selected_source()
        segment = None
        version = self.session.selected_version()
        if 0 <= self.session.selected_shot_index < len(plan.segments):
            segment = plan.segments[self.session.selected_shot_index]
        if source is None or source.cache is None or segment is None or version is None:
            # Fall back to active version of playhead segment
            segment = plan.segment_at(self.current_frame)
            if segment is None or segment.active is None or segment.active.source is None:
                self.timeline.set_cached_frames(set(), set())
                return
            version = segment.active
            source = version.source
        if source is None or source.cache is None:
            self.timeline.set_cached_frames(set(), set())
            return

        decoder_frame = plan.decoder_frame_for_version(segment, version, self.current_frame)
        source.cache.set_playhead(decoder_frame, self.playback_direction)
        cached = source.cache.get_cached_frames()
        ram_cached = {
            segment.global_start + decoder_frame_to_local_index(source, decoder_frame_num)
            for decoder_frame_num in cached
        }

        display_cached: set[int] = set()
        slot = self._display_slot_for_source(source)
        if slot is not None:
            try:
                gpu_frames = self.viewport.native_renderer.get_display_cached_frames(slot)
            except Exception:
                gpu_frames = []
            display_cached = {
                segment.global_start + decoder_frame_to_local_index(source, decoder_frame_num)
                for decoder_frame_num in gpu_frames
            }

        self.timeline.set_cached_frames(ram_cached, display_cached)

    def _display_slot_for_source(self, source: MediaSource) -> int | None:
        """Viewport compare-slot index for a source currently registered with the renderer."""
        plan = self.session.plan
        segment = plan.segment_at(self.current_frame)
        if segment is None or source is None or source.cache is None:
            return None
        target = source.cache.native_cache
        for index, version in enumerate(segment.display_versions()):
            if (
                version.source is not None
                and version.source.cache is not None
                and version.source.cache.native_cache is target
            ):
                return index
        return None

    def _reset_playback_clock(self):
        self._playback_anchor_time = time.monotonic()
        self._playback_anchor_frame = self.current_frame
        self._playback_anchor_direction = self.playback_direction

    def _schedule_decode_for_frame(self, frame: int) -> None:
        """Prioritize decode for ``frame`` without moving the playhead."""
        plan = self.session.plan
        if plan.empty:
            return
        segment = plan.segment_at(frame)
        if segment is None:
            return
        for version in segment.display_versions():
            if version.source is None or version.source.cache is None or version.offline:
                continue
            decoder_frame = plan.decoder_frame_for_version(segment, version, frame)
            version.source.cache.get_frame(decoder_frame)

    def _frame_ready_for_display(self, frame: int) -> bool:
        """True when every online compare slot for ``frame`` is in the decode cache."""
        plan = self.session.plan
        if plan.empty:
            return True
        segment = plan.segment_at(frame)
        if segment is None:
            return True
        online = [
            version
            for version in segment.display_versions()
            if version.source is not None
            and version.source.cache is not None
            and not version.offline
        ]
        if not online:
            return True
        for version in online:
            decoder_frame = plan.decoder_frame_for_version(segment, version, frame)
            if not version.source.cache.has_frame(decoder_frame):
                return False
        return True

    def _playback_tick(self):
        timing = normalize_playback_timing(self.settings.playback_timing)
        if timing == PLAYBACK_TIMING_REALTIME:
            steps = realtime_steps(
                time.monotonic() - self._playback_anchor_time,
                self.fps,
            )
            result = advance_playback(
                self._playback_anchor_frame,
                self._playback_anchor_direction,
                steps,
                self.in_point,
                self.out_point,
                self.settings.loop_mode,
            )
        else:
            result = advance_playback(
                self.current_frame,
                self.playback_direction,
                1,
                self.in_point,
                self.out_point,
                self.settings.loop_mode,
            )

        if result.frame is None:
            if result.stop:
                self.stop_playback()
            return

        if timing == PLAYBACK_TIMING_EVERY_FRAME and not result.stop:
            next_decode_ready = self._frame_ready_for_display(result.frame)
            if not every_frame_can_advance(next_decode_ready=next_decode_ready):
                self._schedule_decode_for_frame(result.frame)
                return

        if result.frame != self.current_frame or result.direction != self.playback_direction:
            self.playback_direction = result.direction
            self.seek_to_frame(result.frame)

        if result.stop:
            self.stop_playback()

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
        self._reset_playback_clock()

        # Match rate timer interval (ms)
        interval_ms = int(1000.0 / max(1.0, self.fps))
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
        plan = self.session.plan
        if plan.empty or direction == 0:
            return

        if self.playing:
            self.stop_playback()

        current = plan.segment_at(self.current_frame)
        if current is None:
            return
        target_index = current.index + direction
        if target_index < 0 or target_index >= len(plan.segments):
            return

        segment = plan.segments[target_index]
        self.in_point = segment.global_start
        self.out_point = segment.global_end
        self.timeline.set_in_out(self.in_point, self.out_point)
        self._sync_all_cache_playback_ranges()
        self.seek_to_frame(segment.global_start)
        name = segment.stack.name or f"Shot {target_index + 1}"
        self.statusBar().showMessage(f"Clip: {name}")

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

    def _sync_playback_timing_actions(self):
        timing = normalize_playback_timing(self.settings.playback_timing)
        if hasattr(self, "playback_timing_actions"):
            for mode, act in self.playback_timing_actions:
                act.setChecked(mode == timing)

    def _set_playback_timing(self, mode: str):
        self.settings.playback_timing = normalize_playback_timing(mode)
        self.settings.save()
        self._sync_playback_timing_actions()
        self._sync_upload_queue_policy()
        if self.playing:
            self._reset_playback_clock()

    def _scale_matches(self, a: float, b: float, tol: float = 0.001) -> bool:
        return abs(a - b) < tol

    def _current_resolution_scale(self) -> float:
        source = self._selected_source()
        if source is None:
            return 1.0
        return source.resolution_scale

    def _sync_resolution_ui_for_source(self, source):
        if source is None:
            return
        if isinstance(source, int):
            source = self._selected_source()
            if source is None:
                return
        scale = source.resolution_scale
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
        source = self._selected_source()
        if source is None or source.cache is None:
            return

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
        self.session.set_playback_rate_override(fps)
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
        if self.exr_layer_combo.count() == 0 or self.session.empty:
            return
        layer = self.exr_layer_combo.currentText()
        self.active_exr_layer = layer
        self.viewport.exr_layer_str = layer
        source = self._selected_source()
        if source is None:
            return
        decoder = source.decoder
        if isinstance(decoder, EXRDecoder) and source.cache is not None:
            decoder.active_layer = layer
            source.cache.clear()
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
        loc = self.session.playhead_shot_version(self.current_frame)
        if loc is not None:
            self.session.set_clip_input_colorspace(loc[0], loc[1], name)
        self.ocio_manager.input_colorspace = name
        loc_key = f"{loc[0]}:{loc[1]}" if loc else "none"
        self._applied_input_colorspace_key = f"{loc_key}|{name}"
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
            if not self.ocio_manager._cdl_is_identity():
                grade_parts.append("CDL")

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
        self.ocio_manager.reset_cdl_values()
        # Clear apply cache so the next version/shot change reloads OTIO CDL.
        # Does not wipe persisted metadata["framecycler"]["cdl"] keys.
        self._applied_cdl_key = None
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
    def _sync_upload_queue_policy(self):
        renderer = getattr(getattr(self, "viewport", None), "native_renderer", None)
        if renderer is None or not hasattr(renderer, "set_upload_queue_policy"):
            return
        from .. import framecycler_engine

        timing = normalize_playback_timing(self.settings.playback_timing)
        policy = (
            framecycler_engine.UploadQueuePolicy.Realtime
            if timing == PLAYBACK_TIMING_REALTIME
            else framecycler_engine.UploadQueuePolicy.EveryFrame
        )
        renderer.set_upload_queue_policy(policy)

    def _apply_renderer_cache_settings(self):
        renderer = self.viewport.native_renderer
        renderer.set_display_cache_limit_gb(self.settings.display_cache_limit_gb)
        if self.settings.display_cache_limit_gb <= 0.0:
            renderer.clear_display_cache()
        self._sync_upload_queue_policy()

    def _open_settings_dialog(self):
        dialog = SettingsDialog(self.settings, self)
        old_mode = getattr(self.settings, "missing_frame_mode", "Nearest Frame")
        old_timing = normalize_playback_timing(self.settings.playback_timing)
        if dialog.exec():
            # Apply changes
            self.settings.save()
            new_mode = getattr(self.settings, "missing_frame_mode", "Nearest Frame")
            new_timing = normalize_playback_timing(self.settings.playback_timing)
            self._sync_playback_timing_actions()
            self._sync_upload_queue_policy()
            if self.playing and new_timing != old_timing:
                self._reset_playback_clock()
            # Update cache worker thread sizes
            for source in self.session.all_online_sources():
                if source.cache is None:
                    continue
                if old_mode != new_mode:
                    source.cache.clear()
                source.cache.update_settings()
            self._apply_renderer_cache_settings()
            # Reload OCIO if path changed
            self.ocio_manager.load_config(self.settings.ocio_config_path)
            self.viewport.update_ocio_pipeline()
            self._populate_look_combo()
            self._build_ocio_submenu()
            if old_mode != new_mode:
                self.seek_to_frame(self.current_frame)

    def _update_ui_states(self):
        self._populate_look_combo()
        self._build_ocio_submenu()

    def _check_renderer_status(self):
        if hasattr(self, "viewport") and hasattr(self.viewport, "native_renderer"):
            if self.viewport.native_renderer.is_fallback_null_backend():
                from PySide6.QtGui import QGuiApplication
                if QGuiApplication.platformName() == "offscreen":
                    return
                from PySide6.QtWidgets import QMessageBox
                QMessageBox.warning(
                    self,
                    "Graphics Warning",
                    "Failed to initialize hardware accelerated graphics (Vulkan/Metal/Direct3D).\n\n"
                    "The application has fallen back to the offscreen Null backend, so no frames will be displayed. "
                    "Please check your graphics drivers and Vulkan/GPU support."
                )

    def closeEvent(self, event):
        if hasattr(self, "main_splitter"):
            sizes = self.main_splitter.sizes()
            if len(sizes) == 2 and sizes[1] > 0:
                self.settings.timeline_splitter_sizes = sizes
                self.settings.save()
        self.timer.stop()
        self._close_all_sources()
        self.viewport.cleanup()
        event.accept()
