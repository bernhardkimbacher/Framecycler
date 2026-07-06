import sys
from pathlib import Path

from PySide6.QtGui import QIcon

ICON_FILES = (
    "app_icon_128.png",
    "app_icon_256.png",
    "app_icon_512.png",
    "app_icon.png",
)


def _icons_dir() -> Path:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        bundled = Path(sys._MEIPASS) / "assets" / "icons"
        if bundled.is_dir():
            return bundled

    return Path(__file__).resolve().parents[3] / "assets" / "icons"


def load_app_icon() -> QIcon:
    icons_dir = _icons_dir()
    icon = QIcon()

    for filename in ICON_FILES:
        path = icons_dir / filename
        if path.is_file():
            icon.addFile(str(path))

    return icon
