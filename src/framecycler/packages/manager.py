"""Discover, enable, and activate Framecycler packages."""

from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path
from typing import Any

from PySide6.QtCore import QTimer
from PySide6.QtGui import QAction

from .api import (
    EventBus,
    HudPainterSpec,
    KeybindSpec,
    Package,
    PackageContext,
    PackageEvents,
    PanelSpec,
    SettingsField,
)
from .decoder_registry import DecoderFactory, DecoderRegistry
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
        self._keybind_specs: list[KeybindSpec] = []
        self._hud_painter_specs: list[HudPainterSpec] = []
        self._settings_schemas: dict[str, list[SettingsField]] = {}
        self._decoder_registrations: list[
            tuple[str, str, list[str], DecoderFactory, int]
        ] = []
        self.decoder_registry = DecoderRegistry()

        self._pending_frame: tuple[int, str] | None = None
        self._frame_flush_armed = False

    @property
    def manifests(self) -> list[PackageManifest]:
        return list(self._manifests)

    @property
    def menu_actions(self) -> list[QAction]:
        return list(self._menu_actions)

    @property
    def panel_specs(self) -> list[PanelSpec]:
        return list(self._panel_specs)

    @property
    def keybind_specs(self) -> list[KeybindSpec]:
        return list(self._keybind_specs)

    @property
    def hud_painter_specs(self) -> list[HudPainterSpec]:
        return sorted(self._hud_painter_specs, key=lambda s: s.z)

    @property
    def settings_schemas(self) -> dict[str, list[SettingsField]]:
        return dict(self._settings_schemas)

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

        self._teardown_all_active()
        self._menu_actions.clear()
        self._panel_specs.clear()
        self._keybind_specs.clear()
        self._hud_painter_specs.clear()
        self._settings_schemas.clear()
        self._decoder_registrations.clear()
        self.decoder_registry.clear()
        self._active.clear()

        for manifest in self._manifests:
            if not self.is_enabled(manifest):
                logger.info("Package disabled: %s", manifest.id)
                continue
            self._activate_one(manifest)

        self._apply_decoder_registrations()

    def reload_enabled(self) -> None:
        """Re-run enable/disable without process restart (Settings OK)."""
        panel_host = getattr(self._host, "panel_host", None)
        keybind_registry = getattr(self._host, "keybind_registry", None)

        # Remove package panels/keybinds before reactivating.
        for manifest, _pkg, _ctx in list(self._active):
            if panel_host is not None:
                panel_host.remove_package_panels(manifest.id)
            if keybind_registry is not None:
                keybind_registry.unregister_package(manifest.id)

        self.load_enabled()

        if panel_host is not None and getattr(panel_host, "finalized", False):
            for spec in self._panel_specs:
                if spec.panel_id not in panel_host.registered_ids():
                    try:
                        panel_host.register(spec)
                    except ValueError:
                        pass

        if keybind_registry is not None:
            for spec in self._keybind_specs:
                keybind_registry.register_package_keybind(spec)

        rebuild_plugins = getattr(self._host, "_rebuild_plugins_menu", None)
        if callable(rebuild_plugins):
            rebuild_plugins()

        request_hud = getattr(self._host, "_request_package_hud_repaint", None)
        if callable(request_hud):
            request_hud()

    def _activate_one(self, manifest: PackageManifest) -> None:
        try:
            package = self._instantiate(manifest)
        except Exception:
            logger.exception("Failed to load package %s from %s", manifest.id, manifest.path)
            return

        pkg_logger = logging.getLogger(f"framecycler.package.{manifest.id}")
        ctx = PackageContext(
            package_id=manifest.id,
            package_dir=manifest.path,
            logger=pkg_logger,
            host=self._host,
            event_bus=self._event_bus,
            menu_actions=self._menu_actions,
            panel_specs=self._panel_specs,
            keybind_specs=self._keybind_specs,
            hud_painter_specs=self._hud_painter_specs,
            settings_schemas=self._settings_schemas,
            decoder_registrations=self._decoder_registrations,
        )
        try:
            package.activate(ctx)
        except Exception:
            logger.exception("Failed to activate package %s", manifest.id)
            return
        self._active.append((manifest, package, ctx))
        logger.info("Activated package %s (%s)", manifest.id, manifest.source)

    def _teardown_all_active(self) -> None:
        for manifest, package, ctx in reversed(list(self._active)):
            try:
                package.deactivate(ctx)
            except Exception:
                logger.exception("Failed to deactivate package %s", manifest.id)
            try:
                ctx._unsubscribe_all()
            except Exception:
                logger.exception("Failed to unsubscribe package %s", manifest.id)

    def _apply_decoder_registrations(self) -> None:
        for entry_id, package_id, extensions, factory, priority in self._decoder_registrations:
            try:
                self.decoder_registry.register(
                    entry_id,
                    package_id=package_id,
                    extensions=extensions,
                    factory=factory,
                    priority=priority,
                )
            except Exception:
                logger.exception("Failed to register decoder %s", entry_id)

    def resolve_decoder(self, extension: str) -> DecoderFactory | None:
        return self.decoder_registry.resolve(extension)

    def emit(self, event: str, *args: Any, **kwargs: Any) -> None:
        self._event_bus.emit(event, *args, **kwargs)

    def emit_media_loaded(self, source_index: int, path: str, metadata: dict) -> None:
        self.emit(PackageEvents.MEDIA_LOADED, source_index, path, metadata)

    def emit_frame_changed(
        self, frame_index: int, timecode: str, *, coalesce: bool = False
    ) -> None:
        if not coalesce:
            self._pending_frame = None
            self.emit(PackageEvents.FRAME_CHANGED, frame_index, timecode)
            return
        self._pending_frame = (int(frame_index), str(timecode))
        if not self._frame_flush_armed:
            self._frame_flush_armed = True
            QTimer.singleShot(0, self._flush_coalesced_frame)

    def _flush_coalesced_frame(self) -> None:
        self._frame_flush_armed = False
        pending = self._pending_frame
        self._pending_frame = None
        if pending is None:
            return
        self.emit(PackageEvents.FRAME_CHANGED, pending[0], pending[1])

    def emit_session_changed(self) -> None:
        self.emit(PackageEvents.SESSION_CHANGED)

    def _instantiate(self, manifest: PackageManifest) -> Package:
        module_file = manifest.path / f"{manifest.entry_module}.py"
        if not module_file.is_file():
            raise FileNotFoundError(f"Package entry module not found: {module_file}")

        unique_name = f"framecycler_pkg_{manifest.id.replace('.', '_')}"
        # Drop stale module so reload_enabled re-imports updated code when needed.
        sys.modules.pop(unique_name, None)
        spec = importlib.util.spec_from_file_location(unique_name, module_file)
        if spec is None or spec.loader is None:
            raise ImportError(f"Cannot create import spec for {module_file}")

        module = importlib.util.module_from_spec(spec)
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
        if not _is_package_instance(instance):
            raise TypeError(
                f"Package {manifest.id}: {manifest.entry_class} must subclass Package"
            )
        return instance


def _is_package_instance(instance: object) -> bool:
    """True if *instance* is a Package, tolerating dual ``framecycler`` / ``src.framecycler`` imports."""
    if isinstance(instance, Package):
        return True
    for base in type(instance).__mro__:
        if base is object:
            continue
        if base.__name__ == "Package" and str(getattr(base, "__module__", "")).endswith(
            "packages.api"
        ):
            return True
    return False
