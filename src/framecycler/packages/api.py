"""Package base class and host context API."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PySide6.QtGui import QAction, QPainter
from PySide6.QtCore import QRect
from PySide6.QtWidgets import QWidget

from .decoder_registry import DecoderFactory


class PackageEvents:
    MEDIA_LOADED = "media_loaded"
    # During playback, FRAME_CHANGED is coalesced (latest-wins per event-loop turn).
    # Scrub / seek-while-paused delivers every frame immediately.
    FRAME_CHANGED = "frame_changed"
    SESSION_CHANGED = "session_changed"


@dataclass
class PanelSpec:
    """Registration for one dockable panel (built-in or package)."""

    panel_id: str
    title: str
    factory: Callable[[QWidget], QWidget]
    default_area: str = "right"
    visible_by_default: bool = False
    eager: bool = False
    on_created: Callable[[QWidget], None] | None = field(default=None)


@dataclass
class KeybindSpec:
    keybind_id: str
    sequence: str
    callback: Callable[[], Any]
    context: str = "app"  # app | viewer
    package_id: str = ""


@dataclass
class HudPainterSpec:
    painter_id: str
    paint: Callable[[QPainter, QRect, int], None]
    z: int = 0
    package_id: str = ""


@dataclass
class SettingsField:
    key: str
    type: str  # bool | int | float | string | enum
    label: str
    default: Any = None
    choices: list[Any] | None = None


class PackageContext:
    """Host services injected into packages. Prefer this over MainWindow internals."""

    def __init__(
        self,
        *,
        package_id: str,
        package_dir: Path,
        logger: logging.Logger,
        host: Any,
        event_bus: "EventBus",
        menu_actions: list[QAction],
        panel_specs: list[PanelSpec] | None = None,
        keybind_specs: list[KeybindSpec] | None = None,
        hud_painter_specs: list[HudPainterSpec] | None = None,
        settings_schemas: dict[str, list[SettingsField]] | None = None,
        decoder_registrations: list[tuple[str, str, list[str], DecoderFactory, int]] | None = None,
    ):
        self.package_id = package_id
        self.package_dir = Path(package_dir)
        self.logger = logger
        self._host = host
        self._event_bus = event_bus
        self._menu_actions = menu_actions
        self._panel_specs = panel_specs if panel_specs is not None else []
        self._keybind_specs = keybind_specs if keybind_specs is not None else []
        self._hud_painter_specs = hud_painter_specs if hud_painter_specs is not None else []
        self._settings_schemas = settings_schemas if settings_schemas is not None else {}
        self._decoder_registrations = (
            decoder_registrations if decoder_registrations is not None else []
        )
        self._subscriptions: list[tuple[str, Callable[..., Any]]] = []

    @property
    def session(self):
        return self._host.session

    @property
    def ocio(self):
        return self._host.ocio_manager

    @property
    def settings(self):
        return self._host.settings

    def add_media(self, paths: list[str], mode: str = "sequence") -> int:
        return int(self._host._add_media(list(paths), mode=mode) or 0)

    def add_menu_actions(self, actions: list[QAction]) -> None:
        for action in actions:
            if action is not None:
                self._menu_actions.append(action)

    def register_panel(
        self,
        panel_id: str,
        *,
        title: str,
        factory: Callable[[QWidget], QWidget],
        default_area: str = "right",
        visible_by_default: bool = False,
    ) -> None:
        """Register a dockable panel. Stable id becomes ``{package_id}.{panel_id}``."""
        if not panel_id or "." in panel_id:
            raise ValueError(
                f"panel_id must be a non-empty local name without dots; got {panel_id!r}"
            )
        full_id = f"{self.package_id}.{panel_id}"
        if any(spec.panel_id == full_id for spec in self._panel_specs):
            raise ValueError(f"Duplicate panel id: {full_id}")
        self._panel_specs.append(
            PanelSpec(
                panel_id=full_id,
                title=title,
                factory=factory,
                default_area=default_area,
                visible_by_default=visible_by_default,
                eager=False,
            )
        )

    def register_keybind(
        self,
        keybind_id: str,
        *,
        sequence: str,
        callback: Callable[[], Any],
        context: str = "app",
    ) -> None:
        if not keybind_id or "." in keybind_id:
            raise ValueError(
                f"keybind_id must be a non-empty local name without dots; got {keybind_id!r}"
            )
        if context not in ("app", "viewer"):
            raise ValueError(f"context must be 'app' or 'viewer'; got {context!r}")
        full_id = f"{self.package_id}.{keybind_id}"
        if any(spec.keybind_id == full_id for spec in self._keybind_specs):
            raise ValueError(f"Duplicate keybind id: {full_id}")
        self._keybind_specs.append(
            KeybindSpec(
                keybind_id=full_id,
                sequence=sequence,
                callback=callback,
                context=context,
                package_id=self.package_id,
            )
        )

    def register_hud_painter(
        self,
        painter_id: str,
        *,
        paint: Callable[[QPainter, QRect, int], None],
        z: int = 0,
    ) -> None:
        if not painter_id or "." in painter_id:
            raise ValueError(
                f"painter_id must be a non-empty local name without dots; got {painter_id!r}"
            )
        full_id = f"{self.package_id}.{painter_id}"
        if any(spec.painter_id == full_id for spec in self._hud_painter_specs):
            raise ValueError(f"Duplicate HUD painter id: {full_id}")
        self._hud_painter_specs.append(
            HudPainterSpec(
                painter_id=full_id,
                paint=paint,
                z=int(z),
                package_id=self.package_id,
            )
        )

    def define_settings_schema(self, fields: list[dict[str, Any] | SettingsField]) -> None:
        parsed: list[SettingsField] = []
        for raw in fields:
            if isinstance(raw, SettingsField):
                parsed.append(raw)
                continue
            key = str(raw.get("key", ""))
            ftype = str(raw.get("type", "string"))
            if not key or ftype not in ("bool", "int", "float", "string", "enum"):
                raise ValueError(f"Invalid settings field: {raw!r}")
            if ftype == "enum" and not raw.get("choices"):
                raise ValueError(f"enum field {key!r} requires choices")
            parsed.append(
                SettingsField(
                    key=key,
                    type=ftype,
                    label=str(raw.get("label", key)),
                    default=raw.get("default"),
                    choices=list(raw["choices"]) if raw.get("choices") is not None else None,
                )
            )
        self._settings_schemas[self.package_id] = parsed
        # Ensure defaults exist in settings store.
        store = getattr(self.settings, "package_settings", None)
        if isinstance(store, dict):
            pkg_vals = store.setdefault(self.package_id, {})
            for field_spec in parsed:
                if field_spec.key not in pkg_vals and field_spec.default is not None:
                    pkg_vals[field_spec.key] = field_spec.default

    def get_setting(self, key: str, default: Any = None) -> Any:
        store = getattr(self.settings, "package_settings", {}) or {}
        pkg_vals = store.get(self.package_id, {})
        if key in pkg_vals:
            return pkg_vals[key]
        schema = self._settings_schemas.get(self.package_id, [])
        for field_spec in schema:
            if field_spec.key == key:
                return field_spec.default if field_spec.default is not None else default
        return default

    def set_setting(self, key: str, value: Any) -> None:
        store = getattr(self.settings, "package_settings", None)
        if not isinstance(store, dict):
            return
        pkg_vals = store.setdefault(self.package_id, {})
        pkg_vals[key] = value

    def register_decoder(
        self,
        decoder_id: str,
        *,
        extensions: list[str],
        factory: DecoderFactory,
        priority: int = 0,
    ) -> None:
        if not decoder_id or "." in decoder_id:
            raise ValueError(
                f"decoder_id must be a non-empty local name without dots; got {decoder_id!r}"
            )
        full_id = f"{self.package_id}.{decoder_id}"
        self._decoder_registrations.append(
            (full_id, self.package_id, list(extensions), factory, int(priority))
        )

    def status(self, message: str) -> None:
        bar = getattr(self._host, "statusBar", None)
        if callable(bar):
            bar().showMessage(message)

    def parent_widget(self) -> QWidget:
        return self._host

    def update_ocio_pipeline(self) -> None:
        viewport = getattr(self._host, "viewport", None)
        if viewport is not None and hasattr(viewport, "update_ocio_pipeline"):
            viewport.update_ocio_pipeline()
        update_label = getattr(self._host, "_update_ocio_info_label", None)
        if callable(update_label):
            update_label()

    def apply_cdl(
        self,
        *,
        slope: tuple[float, float, float] | None = None,
        offset: tuple[float, float, float] | None = None,
        power: tuple[float, float, float] | None = None,
        saturation: float | None = None,
    ) -> None:
        """Apply ASC CDL to the viewer only (does not persist to OTIO)."""
        self.ocio.set_cdl_values(
            slope=slope,
            offset=offset,
            power=power,
            saturation=saturation,
        )
        self.update_ocio_pipeline()

    def _playhead_frame(self) -> int | None:
        frame = getattr(self._host, "current_frame", None)
        return int(frame) if frame is not None else None

    def _reapply_resolved_cdl(self) -> None:
        apply = getattr(self._host, "_apply_resolved_cdl", None)
        if callable(apply):
            apply(force=True)
        else:
            self.update_ocio_pipeline()

    def apply_resolved_cdl(self) -> None:
        """Re-apply Clip > Stack > Timeline CDL for the current playhead to the viewer."""
        self._reapply_resolved_cdl()

    def set_cdl_on_active_version(
        self,
        *,
        slope: tuple[float, float, float] | None = None,
        offset: tuple[float, float, float] | None = None,
        power: tuple[float, float, float] | None = None,
        saturation: float | None = None,
        style: str | None = None,
    ) -> bool:
        """Persist CDL on the playhead active version clip and apply to the viewer."""
        loc = self.session.playhead_shot_version(self._playhead_frame())
        if loc is None:
            return False
        result = self.session.set_clip_cdl(
            loc[0],
            loc[1],
            slope=slope,
            offset=offset,
            power=power,
            saturation=saturation,
            style=style,
        )
        if result is None:
            return False
        self._reapply_resolved_cdl()
        return True

    def set_cdl_on_active_shot(
        self,
        *,
        slope: tuple[float, float, float] | None = None,
        offset: tuple[float, float, float] | None = None,
        power: tuple[float, float, float] | None = None,
        saturation: float | None = None,
        style: str | None = None,
    ) -> bool:
        """Persist CDL on the playhead shot stack and apply to the viewer."""
        loc = self.session.playhead_shot_version(self._playhead_frame())
        if loc is None:
            return False
        result = self.session.set_stack_cdl(
            loc[0],
            slope=slope,
            offset=offset,
            power=power,
            saturation=saturation,
            style=style,
        )
        if result is None:
            return False
        self._reapply_resolved_cdl()
        return True

    def set_cdl_on_timeline(
        self,
        *,
        slope: tuple[float, float, float] | None = None,
        offset: tuple[float, float, float] | None = None,
        power: tuple[float, float, float] | None = None,
        saturation: float | None = None,
        style: str | None = None,
    ) -> None:
        """Persist CDL on the timeline and apply to the viewer."""
        self.session.set_timeline_cdl(
            slope=slope,
            offset=offset,
            power=power,
            saturation=saturation,
            style=style,
        )
        self._reapply_resolved_cdl()

    def clear_cdl_on_active_version(self) -> bool:
        loc = self.session.playhead_shot_version(self._playhead_frame())
        if loc is None:
            return False
        ok = self.session.clear_clip_cdl(loc[0], loc[1])
        if ok:
            self._reapply_resolved_cdl()
        return ok

    def clear_cdl_on_active_shot(self) -> bool:
        loc = self.session.playhead_shot_version(self._playhead_frame())
        if loc is None:
            return False
        ok = self.session.clear_stack_cdl(loc[0])
        if ok:
            self._reapply_resolved_cdl()
        return ok

    def clear_cdl_on_timeline(self) -> None:
        self.session.clear_timeline_cdl()
        self._reapply_resolved_cdl()

    def subscribe(self, event: str, callback: Callable[..., Any]) -> None:
        self._event_bus.subscribe(event, callback)
        self._subscriptions.append((event, callback))

    def unsubscribe(self, event: str, callback: Callable[..., Any]) -> None:
        self._event_bus.unsubscribe(event, callback)
        self._subscriptions = [
            pair for pair in self._subscriptions if pair != (event, callback)
        ]

    def _unsubscribe_all(self) -> None:
        for event, callback in list(self._subscriptions):
            self._event_bus.unsubscribe(event, callback)
        self._subscriptions.clear()


class Package(ABC):
    """Minimal package contract; contribute menus/hooks from activate()."""

    @abstractmethod
    def activate(self, ctx: PackageContext) -> None:
        raise NotImplementedError

    def deactivate(self, ctx: PackageContext) -> None:
        pass


class EventBus:
    def __init__(self) -> None:
        self._subscribers: dict[str, list[Callable[..., Any]]] = {}

    def subscribe(self, event: str, callback: Callable[..., Any]) -> None:
        self._subscribers.setdefault(event, []).append(callback)

    def unsubscribe(self, event: str, callback: Callable[..., Any]) -> None:
        callbacks = self._subscribers.get(event)
        if not callbacks:
            return
        try:
            callbacks.remove(callback)
        except ValueError:
            pass

    def emit(self, event: str, *args: Any, **kwargs: Any) -> None:
        for callback in list(self._subscribers.get(event, [])):
            try:
                callback(*args, **kwargs)
            except Exception:
                logging.getLogger(__name__).exception(
                    "Package event handler failed for %s", event
                )
