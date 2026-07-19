"""Example package: dockable panel subscribed to SESSION_CHANGED."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

try:
    from src.framecycler.packages.api import Package, PackageContext, PackageEvents
except ImportError:
    from framecycler.packages.api import Package, PackageContext, PackageEvents


class _SessionPanel(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        self._label = QLabel("Waiting for session_changed…")
        self._label.setWordWrap(True)
        self._label.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(self._label)
        layout.addStretch()
        self._count = 0

    def on_session_changed(self) -> None:
        self._count += 1
        self._label.setText(
            f"SESSION_CHANGED received ×{self._count}\n"
            "(example_session_panel)"
        )


class ExampleSessionPanelPackage(Package):
    def activate(self, ctx: PackageContext) -> None:
        panel_holder: dict[str, _SessionPanel | None] = {"widget": None}

        def factory(parent: QWidget) -> QWidget:
            panel = _SessionPanel(parent)
            panel_holder["widget"] = panel
            return panel

        ctx.register_panel(
            "session",
            title="Session Events",
            factory=factory,
            default_area="right",
            visible_by_default=False,
        )

        def on_session_changed() -> None:
            panel = panel_holder["widget"]
            if panel is not None:
                panel.on_session_changed()

        ctx.subscribe(PackageEvents.SESSION_CHANGED, on_session_changed)
