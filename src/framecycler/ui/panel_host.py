"""QDockWidget host for built-in and package panels."""

from __future__ import annotations

import base64
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

from PySide6.QtCore import QByteArray, QRect, Qt
from PySide6.QtGui import QAction, QGuiApplication
from PySide6.QtWidgets import QDockWidget, QMainWindow, QMenu, QWidget

from ..packages.api import PanelSpec

if TYPE_CHECKING:
    from ..core.settings import Settings

logger = logging.getLogger(__name__)

AREA_MAP = {
    "left": Qt.DockWidgetArea.LeftDockWidgetArea,
    "right": Qt.DockWidgetArea.RightDockWidgetArea,
    "top": Qt.DockWidgetArea.TopDockWidgetArea,
    "bottom": Qt.DockWidgetArea.BottomDockWidgetArea,
}

# Re-export for callers that import from panel_host.
__all__ = ["AREA_MAP", "PanelDockWidget", "PanelHost", "PanelSpec"]


class PanelDockWidget(QDockWidget):
    """Dock that hides on close instead of destroying content."""

    def closeEvent(self, event) -> None:  # noqa: N802
        self.hide()
        event.accept()


class PanelHost:
    """Owns dock widgets, View → Panels toggles, and layout persistence."""

    def __init__(self) -> None:
        self._specs: dict[str, PanelSpec] = {}
        self._docks: dict[str, PanelDockWidget] = {}
        self._widgets: dict[str, QWidget] = {}
        self._actions: dict[str, QAction] = {}
        self._main_window: QMainWindow | None = None
        self._panels_menu: QMenu | None = None
        self._finalized = False
        self._syncing_visibility = False

    @property
    def finalized(self) -> bool:
        return self._finalized

    def registered_ids(self) -> list[str]:
        return list(self._specs.keys())

    def register(self, spec: PanelSpec) -> None:
        area = (spec.default_area or "right").lower()
        if area not in AREA_MAP:
            raise ValueError(
                f"Invalid default_area {spec.default_area!r} for panel {spec.panel_id!r}; "
                f"expected one of {sorted(AREA_MAP)}"
            )
        if not spec.panel_id:
            raise ValueError("panel_id must be non-empty")
        if spec.panel_id in self._specs:
            raise ValueError(f"Duplicate panel id: {spec.panel_id}")
        normalized = PanelSpec(
            panel_id=spec.panel_id,
            title=spec.title,
            factory=spec.factory,
            default_area=area,
            visible_by_default=bool(spec.visible_by_default),
            eager=bool(spec.eager),
            on_created=spec.on_created,
        )
        self._specs[normalized.panel_id] = normalized
        if self._finalized and self._main_window is not None:
            self._create_dock(normalized)
            self._add_view_action(normalized)
            if normalized.eager or normalized.visible_by_default:
                self.ensure_widget(normalized.panel_id)
            self.set_visible(normalized.panel_id, normalized.visible_by_default)

    def register_builtin(
        self,
        panel_id: str,
        *,
        title: str,
        factory: Callable[[QWidget], QWidget],
        default_area: str = "left",
        visible_by_default: bool = False,
        eager: bool = True,
        on_created: Callable[[QWidget], None] | None = None,
    ) -> None:
        full_id = panel_id if panel_id.startswith("builtin.") else f"builtin.{panel_id}"
        self.register(
            PanelSpec(
                panel_id=full_id,
                title=title,
                factory=factory,
                default_area=default_area,
                visible_by_default=visible_by_default,
                eager=eager,
                on_created=on_created,
            )
        )

    def finalize(
        self,
        main_window: QMainWindow,
        view_menu: QMenu,
        settings: "Settings",
        *,
        panels_menu: QMenu | None = None,
    ) -> None:
        if self._finalized:
            return
        self._main_window = main_window
        main_window.setDockOptions(
            QMainWindow.DockOption.AllowTabbedDocks
            | QMainWindow.DockOption.AnimatedDocks
            | QMainWindow.DockOption.AllowNestedDocks
        )

        self._panels_menu = panels_menu if panels_menu is not None else view_menu.addMenu("Panels")

        for spec in self._specs.values():
            self._create_dock(spec)
            self._add_view_action(spec)
            if spec.eager or spec.visible_by_default:
                self.ensure_widget(spec.panel_id)
            if not self._has_saved_state(settings):
                self.set_visible(spec.panel_id, spec.visible_by_default)

        self._restore_layout(settings)
        self._clamp_floating_docks()
        self._sync_all_actions()
        self._finalized = True

    def ensure_widget(self, panel_id: str) -> QWidget | None:
        if panel_id in self._widgets:
            return self._widgets[panel_id]
        spec = self._specs.get(panel_id)
        dock = self._docks.get(panel_id)
        if spec is None or dock is None:
            return None
        try:
            widget = spec.factory(dock)
        except Exception:
            logger.exception("Panel factory failed for %s", panel_id)
            return None
        self._widgets[panel_id] = widget
        dock.setWidget(widget)
        if spec.on_created is not None:
            try:
                spec.on_created(widget)
            except Exception:
                logger.exception("Panel on_created failed for %s", panel_id)
        return widget

    def widget(self, panel_id: str) -> QWidget | None:
        return self.ensure_widget(panel_id)

    def dock(self, panel_id: str) -> QDockWidget | None:
        return self._docks.get(panel_id)

    def action(self, panel_id: str) -> QAction | None:
        return self._actions.get(panel_id)

    def is_visible(self, panel_id: str) -> bool:
        dock = self._docks.get(panel_id)
        if dock is None:
            return False
        return not dock.isHidden()

    def set_visible(self, panel_id: str, visible: bool) -> None:
        dock = self._docks.get(panel_id)
        if dock is None:
            return
        if visible:
            self.ensure_widget(panel_id)
            dock.show()
            dock.raise_()
        else:
            dock.hide()
        self._sync_action(panel_id, visible)

    def remove_package_panels(self, package_id: str) -> None:
        """Remove docks registered under ``{package_id}.*``."""
        prefix = f"{package_id}."
        to_remove = [pid for pid in self._specs if pid.startswith(prefix)]
        for panel_id in to_remove:
            self._remove_panel(panel_id)

    def save_layout(self, settings: "Settings") -> None:
        if self._main_window is None:
            return
        state = bytes(self._main_window.saveState())
        settings.main_window_state = base64.b64encode(state).decode("ascii")
        geom = bytes(self._main_window.saveGeometry())
        settings.main_window_geometry = base64.b64encode(geom).decode("ascii")

    def _has_saved_state(self, settings: "Settings") -> bool:
        raw = getattr(settings, "main_window_state", None) or ""
        return bool(str(raw).strip())

    def _restore_layout(self, settings: "Settings") -> None:
        if self._main_window is None:
            return
        geom_b64 = getattr(settings, "main_window_geometry", None) or ""
        if geom_b64:
            try:
                geom = QByteArray(base64.b64decode(str(geom_b64)))
                self._main_window.restoreGeometry(geom)
            except Exception:
                logger.exception("Failed to restore main window geometry")
        state_b64 = getattr(settings, "main_window_state", None) or ""
        if state_b64:
            try:
                state = QByteArray(base64.b64decode(str(state_b64)))
                self._main_window.restoreState(state)
            except Exception:
                logger.exception("Failed to restore main window dock state")

    def _clamp_floating_docks(self) -> None:
        screens = QGuiApplication.screens()
        if not screens:
            return
        for dock in self._docks.values():
            if not dock.isFloating() or dock.isHidden():
                continue
            geo = dock.frameGeometry()
            if any(screen.availableGeometry().intersects(geo) for screen in screens):
                continue
            # Off-screen: park on the primary screen.
            primary = QGuiApplication.primaryScreen()
            if primary is None:
                continue
            avail: QRect = primary.availableGeometry()
            w = min(geo.width(), avail.width())
            h = min(geo.height(), avail.height())
            dock.setGeometry(
                avail.x() + max(0, (avail.width() - w) // 2),
                avail.y() + max(0, (avail.height() - h) // 2),
                w,
                h,
            )

    def _create_dock(self, spec: PanelSpec) -> None:
        assert self._main_window is not None
        if spec.panel_id in self._docks:
            return
        dock = PanelDockWidget(spec.title, self._main_window)
        dock.setObjectName(spec.panel_id)
        dock.setAllowedAreas(Qt.DockWidgetArea.AllDockWidgetAreas)
        dock.setFeatures(
            QDockWidget.DockWidgetFeature.DockWidgetClosable
            | QDockWidget.DockWidgetFeature.DockWidgetMovable
            | QDockWidget.DockWidgetFeature.DockWidgetFloatable
        )
        # Placeholder until factory runs (lazy first show).
        dock.setWidget(QWidget(dock))
        area = AREA_MAP[spec.default_area]
        self._main_window.addDockWidget(area, dock)
        dock.hide()
        dock.visibilityChanged.connect(
            lambda visible, pid=spec.panel_id: self._on_dock_visibility(pid, visible)
        )
        self._docks[spec.panel_id] = dock

    def _add_view_action(self, spec: PanelSpec) -> None:
        if self._panels_menu is None or spec.panel_id in self._actions:
            return
        action = QAction(spec.title, self._main_window)
        action.setCheckable(True)
        action.setChecked(False)
        action.toggled.connect(
            lambda checked, pid=spec.panel_id: self._on_action_toggled(pid, checked)
        )
        self._panels_menu.addAction(action)
        self._actions[spec.panel_id] = action

    def _on_action_toggled(self, panel_id: str, checked: bool) -> None:
        if self._syncing_visibility:
            return
        self.set_visible(panel_id, checked)

    def _on_dock_visibility(self, panel_id: str, visible: bool) -> None:
        if self._syncing_visibility:
            return
        if visible:
            self.ensure_widget(panel_id)
        self._sync_action(panel_id, visible and self.is_visible(panel_id))

    def _sync_action(self, panel_id: str, visible: bool) -> None:
        action = self._actions.get(panel_id)
        if action is None:
            return
        self._syncing_visibility = True
        try:
            action.setChecked(visible)
        finally:
            self._syncing_visibility = False

    def _sync_all_actions(self) -> None:
        for panel_id in self._docks:
            self._sync_action(panel_id, self.is_visible(panel_id))

    def _remove_panel(self, panel_id: str) -> None:
        action = self._actions.pop(panel_id, None)
        if action is not None and self._panels_menu is not None:
            self._panels_menu.removeAction(action)
        dock = self._docks.pop(panel_id, None)
        if dock is not None and self._main_window is not None:
            self._main_window.removeDockWidget(dock)
            dock.deleteLater()
        self._widgets.pop(panel_id, None)
        self._specs.pop(panel_id, None)
