import sys
import os
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QSurfaceFormat
from .ui.main_window import MainWindow

def main():
    # Configure surface format for OpenGL Core profile
    fmt = QSurfaceFormat()
    fmt.setVersion(3, 2)
    fmt.setProfile(QSurfaceFormat.CoreProfile)
    fmt.setDepthBufferSize(24)
    QSurfaceFormat.setDefaultFormat(fmt)
    
    app = QApplication(sys.argv)
    app.setApplicationName("Framecycler")
    app.setApplicationVersion("1.0.0")
    
    # Custom dark styling and dropdown color correction
    app.setStyleSheet("""
        QMainWindow {
            background-color: #1a1a1a;
            color: #e0e0e0;
        }
        QMenuBar {
            background-color: #252525;
            color: #e0e0e0;
            border-bottom: 1px solid #333333;
        }
        QMenuBar::item:selected {
            background-color: #3b3b3b;
        }
        QMenu {
            background-color: #252525;
            color: #e0e0e0;
            border: 1px solid #3d3d3d;
        }
        QMenu::item:selected {
            background-color: #3b3b3b;
        }
        QComboBox {
            background-color: #252525;
            color: #ffffff;
            border: 1px solid #3d3d3d;
            border-radius: 3px;
            padding: 3px 6px;
        }
        QComboBox::drop-down {
            border: none;
            width: 15px;
        }
        QComboBox QAbstractItemView {
            background-color: #252525;
            color: #ffffff;
            selection-background-color: #3b3b3b;
            selection-color: #ffffff;
            border: 1px solid #3d3d3d;
        }
        QPushButton {
            background-color: #252525;
            color: #ffffff;
            border: 1px solid #3d3d3d;
            border-radius: 3px;
            padding: 4px 8px;
        }
        QPushButton:hover {
            background-color: #333333;
        }
        QPushButton:checked {
            background-color: #007acc;
            border: 1px solid #0098ff;
            color: #ffffff;
        }
        QLabel {
            color: #cccccc;
        }
        QSlider::groove:horizontal {
            border: 1px solid #3d3d3d;
            height: 6px;
            background: #252525;
            border-radius: 3px;
        }
        QSlider::handle:horizontal {
            background: #e0e0e0;
            width: 14px;
            margin-top: -4px;
            margin-bottom: -4px;
            border-radius: 7px;
        }
    """)
    
    window = MainWindow()
    window.show()
    
    sys.exit(app.exec())

if __name__ == "__main__":
    main()
