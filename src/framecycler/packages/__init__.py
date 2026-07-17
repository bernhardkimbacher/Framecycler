"""Discoverable Framecycler packages (plugins)."""

from .api import Package, PackageContext, PackageEvents
from .manager import PackageManager
from .manifest import PackageManifest, discover_packages

__all__ = [
    "Package",
    "PackageContext",
    "PackageEvents",
    "PackageManager",
    "PackageManifest",
    "discover_packages",
]
