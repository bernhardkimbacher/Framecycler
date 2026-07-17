"""Package base class and host context API."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Callable
from pathlib import Path
from typing import Any

from PySide6.QtGui import QAction
from PySide6.QtWidgets import QWidget


class PackageEvents:
    MEDIA_LOADED = "media_loaded"
    FRAME_CHANGED = "frame_changed"
    SESSION_CHANGED = "session_changed"


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
    ):
        self.package_id = package_id
        self.package_dir = Path(package_dir)
        self.logger = logger
        self._host = host
        self._event_bus = event_bus
        self._menu_actions = menu_actions

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

    def unsubscribe(self, event: str, callback: Callable[..., Any]) -> None:
        self._event_bus.unsubscribe(event, callback)


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
