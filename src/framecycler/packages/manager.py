"""Discover, enable, and activate Framecycler packages."""

from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path
from typing import Any

from PySide6.QtGui import QAction

from .api import EventBus, Package, PackageContext, PackageEvents, PanelSpec
from .manifest import PackageManifest, discover_packages, is_package_enabled

logger = logging.getLogger(__name__)


class PackageManager:
    def __init__(self, host: Any, *, config_dir: str | Path | None = None):
        self._host = host
        self._config_dir = config_dir
        self._event_bus = EventBus()
        self._manifests: list[PackageManifest] = []
        self._active: list[tuple[PackageManifest, Package, PackageContext]] = []
        self._menu_actions: list[QAction] = []
        self._panel_specs: list[PanelSpec] = []

    @property
    def manifests(self) -> list[PackageManifest]:
        return list(self._manifests)

    @property
    def menu_actions(self) -> list[QAction]:
        return list(self._menu_actions)

    @property
    def panel_specs(self) -> list[PanelSpec]:
        return list(self._panel_specs)

    def discover(self) -> list[PackageManifest]:
        self._manifests = discover_packages(self._config_dir)
        return self.manifests

    def is_enabled(self, manifest: PackageManifest) -> bool:
        overrides = getattr(self._host.settings, "package_enabled", {}) or {}
        return is_package_enabled(manifest, overrides)

    def load_enabled(self) -> None:
        """Import and activate packages that are effectively enabled."""
        if not self._manifests:
            self.discover()

        self._menu_actions.clear()
        self._panel_specs.clear()
        self._active.clear()

        for manifest in self._manifests:
            if not self.is_enabled(manifest):
                logger.info("Package disabled: %s", manifest.id)
                continue
            try:
                package = self._instantiate(manifest)
            except Exception:
                logger.exception("Failed to load package %s from %s", manifest.id, manifest.path)
                continue

            pkg_logger = logging.getLogger(f"framecycler.package.{manifest.id}")
            ctx = PackageContext(
                package_id=manifest.id,
                package_dir=manifest.path,
                logger=pkg_logger,
                host=self._host,
                event_bus=self._event_bus,
                menu_actions=self._menu_actions,
                panel_specs=self._panel_specs,
            )
            try:
                package.activate(ctx)
            except Exception:
                logger.exception("Failed to activate package %s", manifest.id)
                continue
            self._active.append((manifest, package, ctx))
            logger.info("Activated package %s (%s)", manifest.id, manifest.source)

    def emit(self, event: str, *args: Any, **kwargs: Any) -> None:
        self._event_bus.emit(event, *args, **kwargs)

    def emit_media_loaded(self, source_index: int, path: str, metadata: dict) -> None:
        self.emit(PackageEvents.MEDIA_LOADED, source_index, path, metadata)

    def emit_frame_changed(self, frame_index: int, timecode: str) -> None:
        self.emit(PackageEvents.FRAME_CHANGED, frame_index, timecode)

    def emit_session_changed(self) -> None:
        self.emit(PackageEvents.SESSION_CHANGED)

    def _instantiate(self, manifest: PackageManifest) -> Package:
        module_file = manifest.path / f"{manifest.entry_module}.py"
        if not module_file.is_file():
            raise FileNotFoundError(f"Package entry module not found: {module_file}")

        unique_name = f"framecycler_pkg_{manifest.id.replace('.', '_')}"
        spec = importlib.util.spec_from_file_location(unique_name, module_file)
        if spec is None or spec.loader is None:
            raise ImportError(f"Cannot create import spec for {module_file}")

        module = importlib.util.module_from_spec(spec)
        # Allow relative imports within the package directory if needed.
        sys.modules[unique_name] = module
        package_root = str(manifest.path)
        path_added = False
        if package_root not in sys.path:
            sys.path.insert(0, package_root)
            path_added = True
        try:
            spec.loader.exec_module(module)
        finally:
            if path_added:
                try:
                    sys.path.remove(package_root)
                except ValueError:
                    pass

        cls = getattr(module, manifest.entry_class, None)
        if cls is None:
            raise AttributeError(
                f"Package {manifest.id}: class {manifest.entry_class!r} not in {module_file}"
            )
        instance = cls()
        if not isinstance(instance, Package):
            raise TypeError(
                f"Package {manifest.id}: {manifest.entry_class} must subclass Package"
            )
        return instance
