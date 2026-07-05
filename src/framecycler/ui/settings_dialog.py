import os
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel, 
                             QSlider, QDoubleSpinBox, QComboBox, QLineEdit, 
                             QPushButton, QFileDialog, QDialogButtonBox)
from PySide6.QtCore import Qt
from ..core.settings import Settings

class SettingsDialog(QDialog):
    def __init__(self, settings: Settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.setWindowTitle("Preferences")
        self.setMinimumWidth(400)
        
        self._init_ui()

    def _init_ui(self):
        layout = QVBoxLayout(self)
        
        # 1. Reader Threads setting (Slider)
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
        
        # 2. RAM Cache Limit (DoubleSpinBox)
        layout.addWidget(QLabel("RAM Cache Limit (GB):"))
        self.cache_spin = QDoubleSpinBox()
        self.cache_spin.setRange(0.5, 256.0)
        self.cache_spin.setSingleStep(1.0)
        self.cache_spin.setValue(self.settings.ram_cache_limit_gb)
        layout.addWidget(self.cache_spin)
        
        # 3. Default FPS (ComboBox)
        layout.addWidget(QLabel("Default Playback Framerate (FPS):"))
        self.fps_combo = QComboBox()
        self.fps_combo.addItems(["23.976", "24.0", "25.0", "29.97", "30.0", "48.0", "60.0"])
        self.fps_combo.setCurrentText(f"{self.settings.default_fps:.3f}" if self.settings.default_fps in [23.976, 29.97] else f"{self.settings.default_fps:.1f}")
        layout.addWidget(self.fps_combo)
        
        # 4. Custom OCIO Config File Path
        layout.addWidget(QLabel("Custom OCIO Configuration File (.ocio):"))
        path_layout = QHBoxLayout()
        self.ocio_path_edit = QLineEdit(self.settings.ocio_config_path)
        self.ocio_path_edit.setPlaceholderText("Bundled Studio Config (Default)")
        btn_browse = QPushButton("Browse...")
        btn_browse.clicked.connect(self._browse_ocio)
        path_layout.addWidget(self.ocio_path_edit)
        path_layout.addWidget(btn_browse)
        layout.addLayout(path_layout)
        
        # Standard buttons
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel, self)
        buttons.accepted.connect(self.accept_settings)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _browse_ocio(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select OpenColorIO Configuration",
            "",
            "OCIO Configs (*.ocio);;All Files (*)"
        )
        if path:
            self.ocio_path_edit.setText(path)

    def accept_settings(self):
        # Save modifications back to settings instance
        self.settings.reader_threads = self.thread_slider.value()
        self.settings.ram_cache_limit_gb = self.cache_spin.value()
        self.settings.default_fps = float(self.fps_combo.currentText())
        self.settings.ocio_config_path = self.ocio_path_edit.text().strip()
        self.accept()
