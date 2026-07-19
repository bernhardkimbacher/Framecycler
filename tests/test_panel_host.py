"""Unit and Qt smoke tests for dockable PanelHost / register_panel."""

from __future__ import annotations

import base64
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from PySide6.QtWidgets import QApplication, QLabel, QMainWindow, QMenuBar, QWidget

from src.framecycler.core.settings import Settings
from src.framecycler.packages.api import EventBus, PackageContext, PanelSpec
from src.framecycler.packages.manager import PackageManager
from src.framecycler.ui.panel_host import PanelHost
from src.framecycler.ui.source_list_panel import SourceListPanel


def _app() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


class TestRegisterPanelApi(unittest.TestCase):
    def test_register_panel_records_spec(self):
        specs: list[PanelSpec] = []
        ctx = PackageContext(
            package_id="studio.notes",
            package_dir=Path("."),
            logger=MagicMock(),
            host=MagicMock(),
            event_bus=EventBus(),
            menu_actions=[],
            panel_specs=specs,
        )
        ctx.register_panel(
            "board",
            title="Notes",
            factory=lambda parent: QLabel("hi", parent),
            default_area="left",
            visible_by_default=True,
        )
        self.assertEqual(len(specs), 1)
        self.assertEqual(specs[0].panel_id, "studio.notes.board")
        self.assertEqual(specs[0].title, "Notes")
        self.assertEqual(specs[0].default_area, "left")
        self.assertTrue(specs[0].visible_by_default)

    def test_register_panel_rejects_duplicate_and_dotted_local_id(self):
        specs: list[PanelSpec] = []
        ctx = PackageContext(
            package_id="studio.notes",
            package_dir=Path("."),
            logger=MagicMock(),
            host=MagicMock(),
            event_bus=EventBus(),
            menu_actions=[],
            panel_specs=specs,
        )
        ctx.register_panel("board", title="Notes", factory=lambda p: QWidget(p))
        with self.assertRaises(ValueError):
            ctx.register_panel("board", title="Dup", factory=lambda p: QWidget(p))
        with self.assertRaises(ValueError):
            ctx.register_panel("a.b", title="Bad", factory=lambda p: QWidget(p))

    def test_builtin_shots_id(self):
        _app()
        host = PanelHost()
        host.register_builtin(
            "shots",
            title="Shots",
            factory=lambda parent: QLabel("shots", parent),
        )
        self.assertEqual(host.registered_ids(), ["builtin.shots"])
        with self.assertRaises(ValueError):
            host.register_builtin(
                "shots",
                title="Shots",
                factory=lambda parent: QLabel("dup", parent),
            )


class TestPanelHostQt(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = _app()

    def test_shots_dock_toggle_float_and_state_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(config_dir=tmp)
            window = QMainWindow()
            window.setCentralWidget(QWidget(window))
            menubar = QMenuBar(window)
            window.setMenuBar(menubar)
            view_menu = menubar.addMenu("View")
            panels_menu = view_menu.addMenu("Panels")

            host = PanelHost()
            host.register_builtin(
                "shots",
                title="Shots",
                factory=lambda parent: SourceListPanel(None, parent),
                default_area="left",
                visible_by_default=False,
                eager=True,
            )
            host.finalize(window, view_menu, settings, panels_menu=panels_menu)

            dock = host.dock("builtin.shots")
            self.assertIsNotNone(dock)
            self.assertEqual(dock.objectName(), "builtin.shots")
            self.assertFalse(host.is_visible("builtin.shots"))

            action = host.action("builtin.shots")
            self.assertIsNotNone(action)
            action.setChecked(True)
            self.assertTrue(host.is_visible("builtin.shots"))
            self.assertIsInstance(host.widget("builtin.shots"), SourceListPanel)

            dock.setFloating(True)
            self.assertTrue(dock.isFloating())

            host.save_layout(settings)
            self.assertTrue(settings.main_window_state)
            # Decode must be valid base64 bytes.
            raw = base64.b64decode(settings.main_window_state)
            self.assertGreater(len(raw), 0)

            host.set_visible("builtin.shots", False)
            self.assertFalse(host.is_visible("builtin.shots"))

            # New host restores visibility from saved state.
            window2 = QMainWindow()
            window2.setCentralWidget(QWidget(window2))
            menubar2 = QMenuBar(window2)
            window2.setMenuBar(menubar2)
            view2 = menubar2.addMenu("View")
            panels2 = view2.addMenu("Panels")
            host2 = PanelHost()
            host2.register_builtin(
                "shots",
                title="Shots",
                factory=lambda parent: SourceListPanel(None, parent),
                default_area="left",
                visible_by_default=False,
                eager=True,
            )
            # Persist visible=True into settings for restore.
            host.set_visible("builtin.shots", True)
            host.save_layout(settings)
            host2.finalize(window2, view2, settings, panels_menu=panels2)
            self.assertTrue(host2.is_visible("builtin.shots"))

            window.close()
            window2.close()


class TestPackageRegistersPanel(unittest.TestCase):
    def test_fake_package_panel_appears_in_host(self):
        _app()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "apps"
            root.mkdir()
            pkg_dir = root / "panel_pkg"
            pkg_dir.mkdir()
            (pkg_dir / "package.toml").write_text(
                'id = "test.panel"\nname = "Panel"\n'
                'entry = "package:PanelPackage"\nenabled_by_default = true\n',
                encoding="utf-8",
            )
            (pkg_dir / "package.py").write_text(
                "\n".join(
                    [
                        "from PySide6.QtWidgets import QLabel",
                        "try:",
                        "    from src.framecycler.packages.api import Package, PackageContext",
                        "except ImportError:",
                        "    from framecycler.packages.api import Package, PackageContext",
                        "class PanelPackage(Package):",
                        "    def activate(self, ctx: PackageContext) -> None:",
                        "        ctx.register_panel(",
                        "            'info',",
                        "            title='Info',",
                        "            factory=lambda parent: QLabel('pkg', parent),",
                        "            default_area='right',",
                        "        )",
                    ]
                ),
                encoding="utf-8",
            )

            mw = MagicMock()
            mw.settings.package_enabled = {}
            mw.settings.config_dir = tmp

            with patch(
                "src.framecycler.packages.manifest.package_search_roots",
                return_value=[("shipped", root)],
            ):
                manager = PackageManager(mw, config_dir=tmp)
                manager.load_enabled()

            self.assertEqual(len(manager.panel_specs), 1)
            self.assertEqual(manager.panel_specs[0].panel_id, "test.panel.info")

            window = QMainWindow()
            window.setCentralWidget(QWidget(window))
            menubar = QMenuBar(window)
            window.setMenuBar(menubar)
            view_menu = menubar.addMenu("View")
            panels_menu = view_menu.addMenu("Panels")
            settings = Settings(config_dir=tmp)

            host = PanelHost()
            host.register_builtin(
                "shots",
                title="Shots",
                factory=lambda parent: QLabel("shots", parent),
                eager=True,
            )
            for spec in manager.panel_specs:
                host.register(spec)
            host.finalize(window, view_menu, settings, panels_menu=panels_menu)

            self.assertIn("test.panel.info", host.registered_ids())
            self.assertIn("builtin.shots", host.registered_ids())
            host.set_visible("test.panel.info", True)
            widget = host.widget("test.panel.info")
            self.assertIsInstance(widget, QLabel)
            self.assertEqual(widget.text(), "pkg")
            window.close()


if __name__ == "__main__":
    unittest.main()
