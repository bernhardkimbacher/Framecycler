"""Tests for remaining Package API expansion (#7) features."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QPainter, QImage
from PySide6.QtWidgets import QApplication, QLabel, QMainWindow, QWidget

from src.framecycler.core.media_pool import MediaPool
from src.framecycler.core.settings import Settings
from src.framecycler.packages.api import EventBus, Package, PackageContext, PackageEvents
from src.framecycler.packages.manager import PackageManager
from src.framecycler.ui.keybind_registry import KeybindRegistry
from src.framecycler.ui.panel_host import PanelHost


def _app() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


class TestFrameChangedCoalesce(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = _app()

    def test_coalesce_delivers_latest_only(self):
        host = MagicMock()
        host.settings.package_enabled = {}
        host.settings.config_dir = tempfile.mkdtemp()
        manager = PackageManager(host, config_dir=host.settings.config_dir)
        received = []

        def handler(frame, tc):
            received.append((frame, tc))

        manager._event_bus.subscribe(PackageEvents.FRAME_CHANGED, handler)
        for i in range(10):
            manager.emit_frame_changed(i, f"tc{i}", coalesce=True)
        self.app.processEvents()
        self.assertEqual(received, [(9, "tc9")])

    def test_immediate_emit_without_coalesce(self):
        host = MagicMock()
        host.settings.package_enabled = {}
        host.settings.config_dir = tempfile.mkdtemp()
        manager = PackageManager(host, config_dir=host.settings.config_dir)
        received = []
        manager._event_bus.subscribe(
            PackageEvents.FRAME_CHANGED, lambda f, t: received.append((f, t))
        )
        manager.emit_frame_changed(1, "a", coalesce=False)
        manager.emit_frame_changed(2, "b", coalesce=False)
        self.assertEqual(received, [(1, "a"), (2, "b")])


class TestKeybindRegistry(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = _app()

    def test_conflict_with_reserved_rejected(self):
        window = QMainWindow()
        registry = KeybindRegistry(window)
        registry.reserve("Space")
        from src.framecycler.packages.api import KeybindSpec

        called = []
        ok = registry.register_package_keybind(
            KeybindSpec(
                keybind_id="pkg.space",
                sequence="Space",
                callback=lambda: called.append(1),
                package_id="pkg",
            )
        )
        self.assertFalse(ok)
        self.assertEqual(called, [])
        window.close()

    def test_register_and_unregister_package(self):
        window = QMainWindow()
        registry = KeybindRegistry(window)
        from src.framecycler.packages.api import KeybindSpec

        called = []
        ok = registry.register_package_keybind(
            KeybindSpec(
                keybind_id="pkg.snap",
                sequence="Ctrl+Shift+S",
                callback=lambda: called.append(1),
                package_id="pkg",
            )
        )
        self.assertTrue(ok)
        registry.unregister_package("pkg")
        self.assertNotIn("pkg.snap", registry._package_actions)
        window.close()


class TestHudPainterApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = _app()

    def test_register_hud_painter_records_spec(self):
        specs = []
        ctx = PackageContext(
            package_id="studio.hud",
            package_dir=Path("."),
            logger=MagicMock(),
            host=MagicMock(),
            event_bus=EventBus(),
            menu_actions=[],
            hud_painter_specs=specs,
        )

        def paint(painter, rect, frame):
            pass

        ctx.register_hud_painter("badge", paint=paint, z=5)
        self.assertEqual(len(specs), 1)
        self.assertEqual(specs[0].painter_id, "studio.hud.badge")
        self.assertEqual(specs[0].z, 5)

    def test_viewport_hud_overlay_is_floating_tool_window(self):
        """HUD must be a floating Tool window so Metal createWindowContainer cannot bury it."""
        from src.framecycler.color.ocio_manager import OCIOManager
        from src.framecycler.ui.viewport import ViewportContainer

        host = QMainWindow()
        container = ViewportContainer(OCIOManager(), main_window=host, parent=host)
        host.setCentralWidget(container)
        host.resize(640, 360)
        host.show()
        self.app.processEvents()
        hud = container._hud_overlay
        self.assertTrue(hud._floating)
        self.assertTrue(hud.isVisible())
        self.assertTrue(bool(hud.windowFlags() & Qt.WindowType.Tool))
        container.viewport.hud_visible = True
        container.viewport.current_frame = 42
        container.viewport.current_timecode = "01:00:00:12"
        hud.update()
        self.app.processEvents()
        img = hud.grab().toImage()
        # Built-in FR strip uses bright green; require some non-black green samples.
        greenish = 0
        for y in range(min(60, img.height())):
            for x in range(min(240, img.width())):
                c = img.pixelColor(x, y)
                if c.green() > 180 and c.red() < 100:
                    greenish += 1
        self.assertGreater(greenish, 50)
        host.close()


class TestPackageSettings(unittest.TestCase):
    def test_schema_defaults_and_get_set(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(config_dir=tmp)
            host = MagicMock()
            host.settings = settings
            schemas = {}
            ctx = PackageContext(
                package_id="studio.cfg",
                package_dir=Path("."),
                logger=MagicMock(),
                host=host,
                event_bus=EventBus(),
                menu_actions=[],
                settings_schemas=schemas,
            )
            ctx.define_settings_schema(
                [
                    {"key": "show_badge", "type": "bool", "label": "Badge", "default": True},
                    {"key": "prefix", "type": "string", "label": "Prefix", "default": "FC"},
                ]
            )
            self.assertTrue(ctx.get_setting("show_badge"))
            self.assertEqual(ctx.get_setting("prefix"), "FC")
            ctx.set_setting("prefix", "Show")
            self.assertEqual(ctx.get_setting("prefix"), "Show")
            self.assertIn("studio.cfg", settings.package_settings)


class TestDecoderRegistry(unittest.TestCase):
    def test_unique_extension_routes_to_package_factory(self):
        from src.framecycler.decoders.base import BaseDecoder
        import numpy as np

        class StubDecoder(BaseDecoder):
            def __init__(self, path: str):
                self.path = path

            def get_metadata(self):
                return {
                    "width": 8,
                    "height": 8,
                    "fps": 24.0,
                    "frame_count": 1,
                    "start_frame": 1,
                    "timecode_start": "01:00:00:00",
                    "channels": ["R", "G", "B", "A"],
                    "has_alpha": True,
                    "pixel_aspect_ratio": 1.0,
                }

            def read_frame(self, frame_index: int, resolution_scale: float = 1.0):
                return {
                    "data": np.zeros((8, 8, 4), dtype=np.float16),
                    "channels": ["R", "G", "B", "A"],
                    "frame_index": frame_index,
                    "timecode": "01:00:00:00",
                }

            def close(self):
                pass

        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(config_dir=tmp)
            pool = MediaPool(settings)
            host = MagicMock()
            host.settings = settings
            host.settings.config_dir = tmp
            manager = PackageManager(host, config_dir=tmp)

            ctx = PackageContext(
                package_id="test.dec",
                package_dir=Path("."),
                logger=MagicMock(),
                host=host,
                event_bus=EventBus(),
                menu_actions=[],
                decoder_registrations=manager._decoder_registrations,
            )
            ctx.register_decoder(
                "stub",
                extensions=[".fcpanel"],
                factory=lambda path: StubDecoder(path),
                priority=50,
            )
            manager._apply_decoder_registrations()
            pool.set_decoder_resolver(manager.resolve_decoder)

            path = Path(tmp) / "clip.fcpanel"
            path.write_bytes(b"stub")
            source = pool.acquire(str(path))
            self.assertIsInstance(source.decoder, StubDecoder)
            # release if available
            if hasattr(pool, "release"):
                pool.release(str(path))

    def test_builtin_exr_unchanged_without_override(self):
        manager = PackageManager(MagicMock(), config_dir=tempfile.mkdtemp())
        self.assertIsNone(manager.resolve_decoder(".exr"))


class TestPackageRegistersExpandedApis(unittest.TestCase):
    def test_fake_package_registers_all_surfaces(self):
        _app()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "apps"
            root.mkdir()
            pkg_dir = root / "full_pkg"
            pkg_dir.mkdir()
            (pkg_dir / "package.toml").write_text(
                'id = "test.full"\nname = "Full"\n'
                'entry = "package:FullPackage"\nenabled_by_default = true\n',
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
                        "class FullPackage(Package):",
                        "    def activate(self, ctx: PackageContext) -> None:",
                        "        ctx.register_panel('p', title='P', factory=lambda parent: QLabel('x', parent))",
                        "        ctx.register_keybind('k', sequence='Ctrl+Alt+K', callback=lambda: None)",
                        "        ctx.register_hud_painter('h', paint=lambda *a: None)",
                        "        ctx.define_settings_schema([{'key':'on','type':'bool','label':'On','default':True}])",
                    ]
                ),
                encoding="utf-8",
            )
            host = MagicMock()
            host.settings.package_enabled = {}
            host.settings.config_dir = tmp
            host.settings.package_settings = {}
            with patch(
                "src.framecycler.packages.manifest.package_search_roots",
                return_value=[("shipped", root)],
            ):
                manager = PackageManager(host, config_dir=tmp)
                manager.load_enabled()
            self.assertEqual(len(manager.panel_specs), 1)
            self.assertEqual(len(manager.keybind_specs), 1)
            self.assertEqual(len(manager.hud_painter_specs), 1)
            self.assertIn("test.full", manager.settings_schemas)


if __name__ == "__main__":
    unittest.main()
