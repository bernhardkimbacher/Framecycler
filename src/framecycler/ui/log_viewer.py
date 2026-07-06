import os
import logging
from PySide6.QtWidgets import QDialog, QVBoxLayout, QPlainTextEdit, QHBoxLayout, QPushButton
from PySide6.QtGui import QKeySequence, QAction
from PySide6.QtCore import Qt
from ..core.logging_config import get_log_file_path
from .fonts import mono_font

class LogViewerDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Application Logs")
        self.resize(750, 500)
        self.setMinimumSize(500, 350)
        
        # Apply clean dark styling consistent with the app theme
        self.setStyleSheet("""
            QDialog {
                background-color: #1a1a1a;
                color: #e0e0e0;
            }
            QPlainTextEdit {
                background-color: #121212;
                color: #dcdcdc;
                border: 1px solid #333333;
                border-radius: 4px;
                padding: 6px;
            }
            QPushButton {
                background-color: #252525;
                color: #ffffff;
                border: 1px solid #3d3d3d;
                border-radius: 3px;
                padding: 5px 12px;
            }
            QPushButton:hover {
                background-color: #333333;
            }
            QPushButton:pressed {
                background-color: #444444;
            }
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        # Monospaced text box
        self.log_text = QPlainTextEdit(self)
        self.log_text.setReadOnly(True)
        
        self.log_text.setFont(mono_font(10))
        
        layout.addWidget(self.log_text)

        # Bottom buttons
        btn_layout = QHBoxLayout()
        
        self.btn_refresh = QPushButton("Refresh", self)
        self.btn_refresh.clicked.connect(self.refresh_logs)
        btn_layout.addWidget(self.btn_refresh)
        
        btn_layout.addStretch()
        
        self.btn_close = QPushButton("Close", self)
        self.btn_close.clicked.connect(self.accept)
        btn_layout.addWidget(self.btn_close)
        
        layout.addLayout(btn_layout)

        # Escape key close shortcut
        act_esc = QAction(self)
        act_esc.setShortcut(QKeySequence("Esc"))
        act_esc.triggered.connect(self.reject)
        self.addAction(act_esc)

        # Initial load
        self.refresh_logs()

    def refresh_logs(self):
        log_file = get_log_file_path()
        if not os.path.exists(log_file):
            self.log_text.setPlainText(f"Log file not found at: {log_file}\nNo logs recorded yet.")
            return

        try:
            with open(log_file, "r", encoding="utf-8", errors="ignore") as f:
                # Read the last 2000 lines to avoid blowing up memory if the log is huge
                lines = f.readlines()
                log_content = "".join(lines[-2000:])
                self.log_text.setPlainText(log_content)
                
            # Scroll to the bottom to display latest entries
            scrollbar = self.log_text.verticalScrollBar()
            scrollbar.setValue(scrollbar.maximum())
        except Exception as e:
            self.log_text.setPlainText(f"Error reading log file: {e}")
