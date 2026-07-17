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
)
from PySide6.QtCore import Qt

from ..core.settings import Settings
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
    def __init__(self, settings: Settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.limits = get_platform_cache_limits()
        self._coupling = False
        self._package_checks: dict[str, QCheckBox] = {}
        self._package_manifests = []
        self.setWindowTitle("Preferences")
        self.setMinimumWidth(480)
        self._init_ui()

    def _init_ui(self):
        layout = QVBoxLayout(self)

        tabs = QTabWidget()
        tabs.addTab(self._build_general_tab(), "General")
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

        layout.addWidget(QLabel("Missing Frame Handling:"))
        self.missing_frame_combo = QComboBox()
        self.missing_frame_combo.addItems(["Nearest Frame", "Red X", "Flat Gray"])
        self.missing_frame_combo.setCurrentText(getattr(self.settings, "missing_frame_mode", "Nearest Frame"))
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

        layout.addStretch(1)
        return page

    def _build_packages_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        ensure_user_packages_dir(self.settings.config_dir)

        restart_note = QLabel("Package enable/disable changes apply after restart.")
        restart_note.setWordWrap(True)
        restart_note.setStyleSheet("color: #aaa;")
        layout.addWidget(restart_note)

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

        list_layout.addStretch(1)
        scroll.setWidget(list_host)
        layout.addWidget(scroll, 1)
        return page

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
        self.settings.ocio_config_path = self.ocio_path_edit.text().strip()
        self.settings.missing_frame_mode = self.missing_frame_combo.currentText()

        overrides: dict[str, bool] = {}
        for manifest in self._package_manifests:
            checkbox = self._package_checks.get(manifest.id)
            if checkbox is None:
                continue
            checked = checkbox.isChecked()
            if checked != manifest.enabled_by_default:
                overrides[manifest.id] = checked
        self.settings.package_enabled = overrides
        self.accept()
