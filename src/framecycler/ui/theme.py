from .fonts import mono_family


def get_viewfinder_stylesheet() -> str:
    mono = mono_family()
    return f"""
    QMainWindow {{
        background-color: #121212;
    }}
    
    QMenuBar {{
        background-color: #1a1a1a;
        color: #aaaaaa;
        font-family: "{mono}", monospace;
        font-size: 11px;
        border-bottom: 1px solid #2d2d2d;
    }}
    
    QMenuBar::item:selected {{
        background-color: #333333;
        color: #00ff00; /* Camera green select */
    }}
    
    QMenu {{
        background-color: #1a1a1a;
        color: #cccccc;
        font-family: "{mono}", monospace;
        font-size: 11px;
        border: 1px solid #2d2d2d;
    }}
    
    QMenu::item:selected {{
        background-color: #00aa00;
        color: #ffffff;
    }}

    QMenu::item:disabled {{
        color: #777777;
        background: transparent;
        font-weight: bold;
        padding-top: 4px;
    }}
    
    QDialog {{
        background-color: #1c1c1c;
        color: #dddddd;
        font-family: "{mono}", monospace;
        font-size: 11px;
    }}
    
    QLabel {{
        color: #aaaaaa;
        font-family: "{mono}", monospace;
        font-size: 11px;
    }}
    
    QPushButton {{
        background-color: #262626;
        color: #aaaaaa;
        border: 1px solid #3d3d3d;
        border-radius: 2px;
        padding: 4px 8px;
        font-family: "{mono}", monospace;
        font-size: 11px;
        font-weight: bold;
    }}
    
    QPushButton:hover {{
        background-color: #333333;
        color: #00ff00;
        border-color: #00ff00;
    }}
    
    QPushButton:pressed {{
        background-color: #111111;
        color: #00ff00;
    }}
    
    QPushButton:checked {{
        background-color: #1e3f1e;
        color: #00ff00;
        border-color: #00ff00;
    }}
    
    QSlider::groove:horizontal {{
        height: 4px;
        background: #333333;
        border-radius: 2px;
    }}
    
    QSlider::handle:horizontal {{
        background: #ff9900;
        border: 1px solid #b36600;
        width: 10px;
        margin-top: -3px;
        margin-bottom: -3px;
        border-radius: 5px;
    }}
    
    QSlider::handle:horizontal:hover {{
        background: #ffb347;
    }}
    
    QComboBox {{
        background-color: #262626;
        color: #cccccc;
        border: 1px solid #3d3d3d;
        padding: 4px 8px;
        font-family: "{mono}", monospace;
        font-size: 12px;
        min-height: 22px;
    }}
    
    QComboBox::drop-down {{
        border: none;
        width: 18px;
    }}
    
    QComboBox QAbstractItemView {{
        background-color: #1a1a1a;
        color: #ffffff;
        selection-background-color: #333333;
        selection-color: #00ff00;
        border: 1px solid #2d2d2d;
        outline: none;
    }}
    
    QComboBox QAbstractItemView::item {{
        height: 22px;
        padding: 1px 12px;
        font-size: 12px;
    }}
    
    QStatusBar {{
        background-color: #1a1a1a;
        color: #777777;
        font-family: "{mono}", monospace;
        font-size: 9px;
        border-top: 1px solid #2d2d2d;
    }}

    QDockWidget {{
        color: #aaaaaa;
        font-family: "{mono}", monospace;
        font-size: 11px;
        titlebar-close-icon: none;
        titlebar-normal-icon: none;
    }}

    QDockWidget::title {{
        background-color: #1a1a1a;
        color: #aaaaaa;
        text-align: left;
        padding-left: 8px;
        padding-top: 3px;
        padding-bottom: 3px;
        border-bottom: 1px solid #2d2d2d;
    }}

    QDockWidget > QWidget {{
        background-color: #121212;
        border: 1px solid #2d2d2d;
    }}
    """
