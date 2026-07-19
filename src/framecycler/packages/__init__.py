"""Discoverable Framecycler packages (plugins)."""

from .api import Package, PackageContext, PackageEvents, PanelSpec
from .manager import PackageManager
from .manifest import PackageManifest, discover_packages

__all__ = [
    "Package",
    "PackageContext",
    "PackageEvents",
    "PanelSpec",
    "PackageManager",
    "PackageManifest",
    "discover_packages",
]
