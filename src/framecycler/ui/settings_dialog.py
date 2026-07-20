from PySide6.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QSlider,
    QComboBox,
    QLineEdit,
    QPushButton,
    QFileDialog,
    QDialogButtonBox,
    QTabWidget,
    QWidget,
    QCheckBox,
    QScrollArea,
    QFrame,
    QGroupBox,
    QSpinBox,
    QDoubleSpinBox,
)
from PySide6.QtCore import Qt

from ..core.settings import Settings
from ..core.playback_timing import (
    PLAYBACK_TIMING_EVERY_FRAME,
    PLAYBACK_TIMING_REALTIME,
    normalize_playback_timing,
)
from ..core.system_memory import (
    cache_warning_text,
    clamp_cache_limits,
    gb_to_slider_ticks,
    get_platform_cache_limits,
    slider_ticks_to_gb,
)
from ..packages.manifest import discover_packages, is_package_enabled
from ..packages.paths import ensure_user_packages_dir, package_search_roots


class SettingsDialog(QDialog):
    def __init__(self, settings: Settings, parent=None, package_manager=None):
        super().__init__(parent)
        self.settings = settings
        self.package_manager = package_manager
        self.limits = get_platform_cache_limits()
        self._coupling = False
        self._package_checks: dict[str, QCheckBox] = {}
        self._package_manifests = []
        self._setting_widgets: dict[tuple[str, str], QWidget] = {}
        self.setWindowTitle("Preferences")
        self.setMinimumWidth(520)
        self._init_ui()

    def _init_ui(self):
        layout = QVBoxLayout(self)

        tabs = QTabWidget()
        tabs.addTab(self._build_general_tab(), "General")
        tabs.addTab(self._build_audio_tab(), "Audio")
        tabs.addTab(self._build_packages_tab(), "Packages")
        layout.addWidget(tabs)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel, self)
        buttons.accepted.connect(self.accept_settings)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _build_general_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        layout.addWidget(QLabel("Reader Thread Count:"))
        thread_layout = QHBoxLayout()
        self.thread_slider = QSlider(Qt.Horizontal)
        self.thread_slider.setRange(1, 32)
        self.thread_slider.setValue(self.settings.reader_threads)
        self.thread_label = QLabel(str(self.settings.reader_threads))
        self.thread_slider.valueChanged.connect(lambda v: self.thread_label.setText(str(v)))
        thread_layout.addWidget(self.thread_slider)
        thread_layout.addWidget(self.thread_label)
        layout.addLayout(thread_layout)

        decode_max_ticks = gb_to_slider_ticks(self.limits.decode_max_gb)
        display_max_ticks = gb_to_slider_ticks(self.limits.display_max_gb)

        layout.addWidget(QLabel("Decode Cache (GB):"))
        decode_layout = QHBoxLayout()
        self.decode_slider = QSlider(Qt.Horizontal)
        self.decode_slider.setRange(0, max(0, decode_max_ticks))
        self.decode_slider.setValue(gb_to_slider_ticks(self.settings.decode_cache_limit_gb))
        self.decode_label = QLabel(self._format_gb_label(self.settings.decode_cache_limit_gb, self.limits.decode_max_gb))
        self.decode_slider.valueChanged.connect(self._on_decode_changed)
        decode_layout.addWidget(self.decode_slider)
        decode_layout.addWidget(self.decode_label)
        layout.addLayout(decode_layout)

        layout.addWidget(QLabel("Display Cache (GB):"))
        display_layout = QHBoxLayout()
        self.display_slider = QSlider(Qt.Horizontal)
        self.display_slider.setRange(0, max(0, display_max_ticks))
        self.display_slider.setValue(gb_to_slider_ticks(self.settings.display_cache_limit_gb))
        self.display_label = QLabel(self._format_gb_label(self.settings.display_cache_limit_gb, self.limits.display_max_gb))
        self.display_slider.valueChanged.connect(self._on_display_changed)
        display_layout.addWidget(self.display_slider)
        display_layout.addWidget(self.display_label)
        layout.addLayout(display_layout)

        if self.limits.coupled:
            layout.addWidget(
                QLabel(
                    f"Combined limit: {self.limits.combined_max_gb:.1f} GB system memory "
                    "(macOS unified memory)"
                )
            )
        else:
            layout.addWidget(
                QLabel(
                    f"Decode max: {self.limits.decode_max_gb:.1f} GB RAM | "
                    f"Display max: {self.limits.display_max_gb:.1f} GB VRAM"
                )
            )

        self.warning_label = QLabel("")
        self.warning_label.setWordWrap(True)
        self.warning_label.setStyleSheet("color: #d9534f;")
        layout.addWidget(self.warning_label)
        self._update_warning()

        layout.addWidget(QLabel("Default Playback Framerate (FPS):"))
        self.fps_combo = QComboBox()
        self.fps_combo.addItems(["23.976", "24.0", "25.0", "29.97", "30.0", "48.0", "60.0"])
        self.fps_combo.setCurrentText(
            f"{self.settings.default_fps:.3f}"
            if self.settings.default_fps in [23.976, 29.97]
            else f"{self.settings.default_fps:.1f}"
        )
        layout.addWidget(self.fps_combo)

        layout.addWidget(QLabel("Default Playback:"))
        self.playback_timing_combo = QComboBox()
        self.playback_timing_combo.addItem("Play Every Frame", PLAYBACK_TIMING_EVERY_FRAME)
        self.playback_timing_combo.addItem("Play Realtime", PLAYBACK_TIMING_REALTIME)
        timing = normalize_playback_timing(self.settings.playback_timing)
        index = self.playback_timing_combo.findData(timing)
        self.playback_timing_combo.setCurrentIndex(max(0, index))
        layout.addWidget(self.playback_timing_combo)

        layout.addWidget(QLabel("Missing Frame Handling:"))
        self.missing_frame_combo = QComboBox()
        self.missing_frame_combo.addItems(["Nearest Frame", "Red X", "Flat Gray"])
        self.missing_frame_combo.setCurrentText(
            getattr(self.settings, "missing_frame_mode", "Nearest Frame")
        )
        layout.addWidget(self.missing_frame_combo)

        layout.addWidget(QLabel("Custom OCIO Configuration File (.ocio):"))
        path_layout = QHBoxLayout()
        self.ocio_path_edit = QLineEdit(self.settings.ocio_config_path)
        self.ocio_path_edit.setPlaceholderText("Bundled Studio Config (Default)")
        btn_browse = QPushButton("Browse...")
        btn_browse.clicked.connect(self._browse_ocio)
        path_layout.addWidget(self.ocio_path_edit)
        path_layout.addWidget(btn_browse)
        layout.addLayout(path_layout)

        self.prefer_edr_check = QCheckBox("Prefer EDR when available")
        self.prefer_edr_check.setChecked(bool(getattr(self.settings, "prefer_edr", False)))
        self.prefer_edr_check.setToolTip(
            "On launch, enable View → Viewer → EDR if the display/GPU supports it. "
            "Use the View menu to toggle mid-session."
        )
        layout.addWidget(self.prefer_edr_check)

        layout.addStretch(1)
        return page

    def _build_audio_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        layout.addWidget(QLabel("Output Device:"))
        self.audio_device_combo = QComboBox()
        self.audio_device_combo.setMinimumWidth(320)
        self._populate_audio_devices()
        layout.addWidget(self.audio_device_combo)

        refresh_row = QHBoxLayout()
        btn_refresh = QPushButton("Refresh Devices")
        btn_refresh.clicked.connect(self._populate_audio_devices)
        refresh_row.addWidget(btn_refresh)
        refresh_row.addStretch(1)
        layout.addLayout(refresh_row)

        hint = QLabel(
            "System Default follows the OS output device. "
            "Choose a specific device if Framecycler is routed to the wrong output."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #aaa;")
        layout.addWidget(hint)

        self.scrub_audio_check = QCheckBox("Scrub audio while seeking")
        self.scrub_audio_check.setChecked(bool(self.settings.scrub_audio))
        layout.addWidget(self.scrub_audio_check)

        layout.addStretch(1)
        return page

    def _populate_audio_devices(self) -> None:
        current = str(getattr(self.settings, "audio_output_device_id", "") or "")
        self.audio_device_combo.blockSignals(True)
        self.audio_device_combo.clear()
        devices = []
        try:
            from .. import framecycler_engine

            devices = list(framecycler_engine.list_audio_output_devices() or [])
        except Exception:
            devices = [{"id": "", "name": "System Default", "is_default": True}]
        if not devices:
            devices = [{"id": "", "name": "System Default", "is_default": True}]

        selected_index = 0
        for i, device in enumerate(devices):
            device_id = str(device.get("id", "") or "")
            name = str(device.get("name", "Device") or "Device")
            label = name
            if device.get("is_default") and device_id:
                label = f"{name} (default)"
            self.audio_device_combo.addItem(label, device_id)
            if device_id == current:
                selected_index = i
        # If saved id is missing (unplugged), keep System Default selected but preserve
        # the saved value until the user picks something else — match by id only.
        if current and selected_index == 0 and self.audio_device_combo.itemData(0) != current:
            self.audio_device_combo.insertItem(1, f"Unavailable device ({current[:8]}…)", current)
            selected_index = 1
        self.audio_device_combo.setCurrentIndex(selected_index)
        self.audio_device_combo.blockSignals(False)

    def _build_packages_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        ensure_user_packages_dir(self.settings.config_dir)

        note = QLabel(
            "Enable/disable and package settings apply when you click OK "
            "(no application restart required)."
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: #aaa;")
        layout.addWidget(note)

        roots_lines = []
        for source, root in package_search_roots(self.settings.config_dir):
            roots_lines.append(f"{source}: {root}")
        roots_label = QLabel("Search paths:\n" + "\n".join(roots_lines))
        roots_label.setWordWrap(True)
        roots_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(roots_label)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        list_host = QWidget()
        list_layout = QVBoxLayout(list_host)

        self._package_manifests = discover_packages(self.settings.config_dir)
        self._package_checks.clear()
        self._setting_widgets.clear()
        schemas = {}
        if self.package_manager is not None:
            schemas = self.package_manager.settings_schemas

        if not self._package_manifests:
            list_layout.addWidget(QLabel("No packages found."))
        else:
            for manifest in self._package_manifests:
                enabled = is_package_enabled(manifest, self.settings.package_enabled)
                checkbox = QCheckBox(f"{manifest.name} ({manifest.id})")
                checkbox.setChecked(enabled)
                checkbox.setToolTip(
                    f"{manifest.description}\n"
                    f"Source: {manifest.source}\n"
                    f"Path: {manifest.path}\n"
                    f"Default: {'enabled' if manifest.enabled_by_default else 'disabled'}"
                )
                self._package_checks[manifest.id] = checkbox
                list_layout.addWidget(checkbox)
                meta = QLabel(f"  {manifest.source} · v{manifest.version}")
                meta.setStyleSheet("color: #888; margin-left: 18px;")
                list_layout.addWidget(meta)

                fields = schemas.get(manifest.id) or []
                if fields:
                    group = QGroupBox(f"Settings — {manifest.name}")
                    group_layout = QVBoxLayout(group)
                    pkg_vals = (self.settings.package_settings or {}).get(manifest.id, {})
                    for field in fields:
                        row = QHBoxLayout()
                        row.addWidget(QLabel(field.label))
                        value = pkg_vals.get(field.key, field.default)
                        widget = self._make_setting_widget(field, value)
                        self._setting_widgets[(manifest.id, field.key)] = widget
                        row.addWidget(widget, stretch=1)
                        group_layout.addLayout(row)
                    list_layout.addWidget(group)

        list_layout.addStretch(1)
        scroll.setWidget(list_host)
        layout.addWidget(scroll, 1)
        return page

    def _make_setting_widget(self, field, value):
        if field.type == "bool":
            w = QCheckBox()
            w.setChecked(bool(value) if value is not None else bool(field.default))
            return w
        if field.type == "int":
            w = QSpinBox()
            w.setRange(-10_000_000, 10_000_000)
            w.setValue(int(value if value is not None else (field.default or 0)))
            return w
        if field.type == "float":
            w = QDoubleSpinBox()
            w.setRange(-1e9, 1e9)
            w.setDecimals(4)
            w.setValue(float(value if value is not None else (field.default or 0.0)))
            return w
        if field.type == "enum":
            w = QComboBox()
            choices = field.choices or []
            for choice in choices:
                w.addItem(str(choice), choice)
            current = value if value is not None else field.default
            idx = w.findData(current)
            if idx < 0:
                idx = w.findText(str(current))
            if idx >= 0:
                w.setCurrentIndex(idx)
            return w
        w = QLineEdit("" if value is None else str(value))
        return w

    def _read_setting_widget(self, field, widget):
        if field.type == "bool":
            return bool(widget.isChecked())
        if field.type == "int":
            return int(widget.value())
        if field.type == "float":
            return float(widget.value())
        if field.type == "enum":
            data = widget.currentData()
            return data if data is not None else widget.currentText()
        return widget.text()

    @staticmethod
    def _format_gb_label(value_gb: float, max_gb: float) -> str:
        return f"{value_gb:.1f} GB (of {max_gb:.1f} GB)"

    def _current_decode_gb(self) -> float:
        return slider_ticks_to_gb(self.decode_slider.value())

    def _current_display_gb(self) -> float:
        return slider_ticks_to_gb(self.display_slider.value())

    def _apply_coupled_limits(self, changed: str) -> None:
        if not self.limits.coupled or self._coupling:
            return
        self._coupling = True
        try:
            decode_gb, display_gb = clamp_cache_limits(
                self._current_decode_gb(),
                self._current_display_gb(),
                self.limits,
            )
            if changed == "decode" and display_gb != self._current_display_gb():
                self.display_slider.blockSignals(True)
                self.display_slider.setValue(gb_to_slider_ticks(display_gb))
                self.display_slider.blockSignals(False)
                self.display_label.setText(self._format_gb_label(display_gb, self.limits.display_max_gb))
            elif changed == "display" and decode_gb != self._current_decode_gb():
                self.decode_slider.blockSignals(True)
                self.decode_slider.setValue(gb_to_slider_ticks(decode_gb))
                self.decode_slider.blockSignals(False)
                self.decode_label.setText(self._format_gb_label(decode_gb, self.limits.decode_max_gb))
        finally:
            self._coupling = False

    def _on_decode_changed(self, ticks: int) -> None:
        decode_gb = slider_ticks_to_gb(ticks)
        self.decode_label.setText(self._format_gb_label(decode_gb, self.limits.decode_max_gb))
        self._apply_coupled_limits("decode")
        self._update_warning()

    def _on_display_changed(self, ticks: int) -> None:
        display_gb = slider_ticks_to_gb(ticks)
        self.display_label.setText(self._format_gb_label(display_gb, self.limits.display_max_gb))
        self._apply_coupled_limits("display")
        self._update_warning()

    def _update_warning(self) -> None:
        self.warning_label.setText(
            cache_warning_text(self._current_decode_gb(), self._current_display_gb(), self.limits)
        )

    def _browse_ocio(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Select OpenColorIO Configuration",
            "",
            "OCIO Configs (*.ocio);;All Files (*)",
        )
        if path:
            self.ocio_path_edit.setText(path)

    def accept_settings(self):
        decode_gb, display_gb = clamp_cache_limits(
            self._current_decode_gb(),
            self._current_display_gb(),
            self.limits,
        )
        self.settings.reader_threads = self.thread_slider.value()
        self.settings.decode_cache_limit_gb = decode_gb
        self.settings.display_cache_limit_gb = display_gb
        self.settings.default_fps = float(self.fps_combo.currentText())
        self.settings.playback_timing = normalize_playback_timing(
            self.playback_timing_combo.currentData()
        )
        self.settings.ocio_config_path = self.ocio_path_edit.text().strip()
        if hasattr(self, "prefer_edr_check"):
            self.settings.prefer_edr = bool(self.prefer_edr_check.isChecked())
        self.settings.missing_frame_mode = self.missing_frame_combo.currentText()
        if hasattr(self, "audio_device_combo"):
            device_id = self.audio_device_combo.currentData()
            self.settings.audio_output_device_id = str(device_id or "")
        if hasattr(self, "scrub_audio_check"):
            self.settings.scrub_audio = bool(self.scrub_audio_check.isChecked())

        overrides: dict[str, bool] = {}
        for manifest in self._package_manifests:
            checkbox = self._package_checks.get(manifest.id)
            if checkbox is None:
                continue
            checked = checkbox.isChecked()
            if checked != manifest.enabled_by_default:
                overrides[manifest.id] = checked
        self.settings.package_enabled = overrides

        schemas = {}
        if self.package_manager is not None:
            schemas = self.package_manager.settings_schemas
        pkg_settings = dict(self.settings.package_settings or {})
        for (package_id, key), widget in self._setting_widgets.items():
            fields = schemas.get(package_id) or []
            field = next((f for f in fields if f.key == key), None)
            if field is None:
                continue
            pkg_settings.setdefault(package_id, {})[key] = self._read_setting_widget(field, widget)
        self.settings.package_settings = pkg_settings
        self.accept()
