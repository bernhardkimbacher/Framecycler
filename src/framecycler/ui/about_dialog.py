from PySide6.QtWidgets import QDialog, QVBoxLayout, QLabel, QHBoxLayout, QPushButton
from PySide6.QtCore import Qt

from ..core.version import APP_NAME, get_about_text


class AboutDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"About {APP_NAME}")
        self.setFixedSize(520, 440)

        self.setStyleSheet("""
            QDialog {
                background-color: #1a1a1a;
                color: #e0e0e0;
            }
            QLabel#title {
                color: #ffffff;
                font-size: 16px;
                font-weight: bold;
            }
            QLabel#body {
                color: #cccccc;
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
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 16)
        layout.setSpacing(12)

        title = QLabel(APP_NAME)
        title.setObjectName("title")
        title.setAlignment(Qt.AlignCenter)
        layout.addWidget(title)

        body = QLabel(get_about_text())
        body.setObjectName("body")
        body.setWordWrap(True)
        body.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        body.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(body, stretch=1)

        btn_layout = QHBoxLayout()
        btn_layout.addStretch()

        btn_close = QPushButton("Close")
        btn_close.clicked.connect(self.accept)
        btn_layout.addWidget(btn_close)

        layout.addLayout(btn_layout)
