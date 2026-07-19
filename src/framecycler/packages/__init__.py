"""Discoverable Framecycler packages (plugins)."""

from .api import (
    HudPainterSpec,
    KeybindSpec,
    Package,
    PackageContext,
    PackageEvents,
    PanelSpec,
    SettingsField,
)
from .manager import PackageManager
from .manifest import PackageManifest, discover_packages

__all__ = [
    "Package",
    "PackageContext",
    "PackageEvents",
    "PanelSpec",
    "KeybindSpec",
    "HudPainterSpec",
    "SettingsField",
    "PackageManager",
    "PackageManifest",
    "discover_packages",
]
