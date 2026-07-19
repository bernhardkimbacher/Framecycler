import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.framecycler.packages.manifest import (
    discover_packages,
    is_package_enabled,
    parse_manifest,
)
from src.framecycler.packages.manager import PackageManager
from src.framecycler.packages.paths import ENV_APPS, package_search_roots


class TestPackageManifest(unittest.TestCase):
    def test_parse_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            pkg_dir = Path(tmp) / "demo"
            pkg_dir.mkdir()
            (pkg_dir / "package.toml").write_text(
                "\n".join(
                    [
                        'id = "demo.pkg"',
                        'name = "Demo"',
                        'version = "1.2.3"',
                        'description = "A demo package"',
                        'entry = "package:DemoPackage"',
                        "enabled_by_default = true",
                    ]
                ),
                encoding="utf-8",
            )
            manifest = parse_manifest(pkg_dir, "user")
            self.assertIsNotNone(manifest)
            self.assertEqual(manifest.id, "demo.pkg")
            self.assertEqual(manifest.name, "Demo")
            self.assertEqual(manifest.version, "1.2.3")
            self.assertTrue(manifest.enabled_by_default)
            self.assertEqual(manifest.entry_module, "package")
            self.assertEqual(manifest.entry_class, "DemoPackage")
            self.assertEqual(manifest.source, "user")

    def test_is_package_enabled_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            pkg_dir = Path(tmp) / "demo"
            pkg_dir.mkdir()
            (pkg_dir / "package.toml").write_text(
                'id = "demo.pkg"\nname = "Demo"\nentry = "package:DemoPackage"\n'
                "enabled_by_default = false\n",
                encoding="utf-8",
            )
            manifest = parse_manifest(pkg_dir, "shipped")
            self.assertFalse(is_package_enabled(manifest, {}))
            self.assertTrue(is_package_enabled(manifest, {"demo.pkg": True}))
            self.assertFalse(is_package_enabled(manifest, {"demo.pkg": False}))


class TestPackageDiscovery(unittest.TestCase):
    def test_discover_from_env_and_first_id_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shipped = root / "shipped"
            user = root / "user"
            env = root / "env"
            for path in (shipped, user, env):
                path.mkdir()

            def write_pkg(parent: Path, pkg_id: str, name: str) -> None:
                d = parent / name
                d.mkdir()
                (d / "package.toml").write_text(
                    f'id = "{pkg_id}"\nname = "{name}"\nentry = "package:P"\n',
                    encoding="utf-8",
                )

            write_pkg(shipped, "shared.id", "shipped_pkg")
            write_pkg(user, "shared.id", "user_pkg")
            write_pkg(env, "env.only", "env_pkg")

            with patch(
                "src.framecycler.packages.manifest.package_search_roots",
                return_value=[("shipped", shipped), ("user", user), ("env", env)],
            ):
                found = discover_packages()

            ids = [m.id for m in found]
            self.assertEqual(ids.count("shared.id"), 1)
            shared = next(m for m in found if m.id == "shared.id")
            self.assertEqual(shared.source, "shipped")
            self.assertIn("env.only", ids)

    def test_env_apps_root_included(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_root = Path(tmp) / "extra"
            env_root.mkdir()
            with patch.dict(os.environ, {ENV_APPS: str(env_root)}, clear=False):
                roots = package_search_roots(config_dir=tmp)
            labels = [label for label, _ in roots]
            self.assertIn("env", labels)
            self.assertEqual(roots[-1][1], env_root)


class TestPackageManager(unittest.TestCase):
    def test_loads_only_enabled_packages(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "apps"
            root.mkdir()

            enabled_dir = root / "enabled_pkg"
            enabled_dir.mkdir()
            (enabled_dir / "package.toml").write_text(
                'id = "test.enabled"\nname = "Enabled"\n'
                'entry = "package:EnabledPackage"\nenabled_by_default = true\n',
                encoding="utf-8",
            )
            (enabled_dir / "package.py").write_text(
                "\n".join(
                    [
                        "try:",
                        "    from src.framecycler.packages.api import Package, PackageContext",
                        "except ImportError:",
                        "    from framecycler.packages.api import Package, PackageContext",
                        "class EnabledPackage(Package):",
                        "    def activate(self, ctx: PackageContext) -> None:",
                        "        ctx.logger.info('enabled activated')",
                    ]
                ),
                encoding="utf-8",
            )

            disabled_dir = root / "disabled_pkg"
            disabled_dir.mkdir()
            (disabled_dir / "package.toml").write_text(
                'id = "test.disabled"\nname = "Disabled"\n'
                'entry = "package:DisabledPackage"\nenabled_by_default = false\n',
                encoding="utf-8",
            )
            (disabled_dir / "package.py").write_text(
                "\n".join(
                    [
                        "try:",
                        "    from src.framecycler.packages.api import Package, PackageContext",
                        "except ImportError:",
                        "    from framecycler.packages.api import Package, PackageContext",
                        "class DisabledPackage(Package):",
                        "    def activate(self, ctx: PackageContext) -> None:",
                        "        raise AssertionError('disabled package should not activate')",
                    ]
                ),
                encoding="utf-8",
            )

            host = MagicMock()
            host.settings.package_enabled = {}
            host.settings.config_dir = tmp

            with patch(
                "src.framecycler.packages.manifest.package_search_roots",
                return_value=[("shipped", root)],
            ):
                manager = PackageManager(host, config_dir=tmp)
                manager.discover()
                manager.load_enabled()

            active_ids = [m.id for m, _, _ in manager._active]
            self.assertEqual(active_ids, ["test.enabled"])

    def test_override_enables_disabled_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "apps"
            root.mkdir()
            pkg_dir = root / "example"
            pkg_dir.mkdir()
            (pkg_dir / "package.toml").write_text(
                'id = "test.example"\nname = "Example"\n'
                'entry = "package:ExamplePackage"\nenabled_by_default = false\n',
                encoding="utf-8",
            )
            (pkg_dir / "package.py").write_text(
                "\n".join(
                    [
                        "try:",
                        "    from src.framecycler.packages.api import Package, PackageContext",
                        "except ImportError:",
                        "    from framecycler.packages.api import Package, PackageContext",
                        "class ExamplePackage(Package):",
                        "    activated = False",
                        "    def activate(self, ctx: PackageContext) -> None:",
                        "        ExamplePackage.activated = True",
                    ]
                ),
                encoding="utf-8",
            )

            host = MagicMock()
            host.settings.package_enabled = {"test.example": True}
            host.settings.config_dir = tmp

            with patch(
                "src.framecycler.packages.manifest.package_search_roots",
                return_value=[("shipped", root)],
            ):
                manager = PackageManager(host, config_dir=tmp)
                manager.load_enabled()

            self.assertEqual(len(manager._active), 1)
            self.assertTrue(manager._active[0][1].__class__.activated)


class TestShippedExamplePackages(unittest.TestCase):
    def test_shipped_examples_disabled_by_default(self):
        repo_apps = Path(__file__).resolve().parents[1] / "apps"
        if not repo_apps.is_dir():
            self.skipTest("apps/ directory missing")

        with patch(
            "src.framecycler.packages.manifest.package_search_roots",
            return_value=[("shipped", repo_apps)],
        ):
            found = {m.id: m for m in discover_packages()}

        self.assertIn("framecycler.example_apply_cdl", found)
        self.assertIn("framecycler.example_add_version", found)
        self.assertIn("framecycler.example_per_stack_cdl", found)
        self.assertIn("framecycler.example_session_panel", found)
        self.assertFalse(found["framecycler.example_apply_cdl"].enabled_by_default)
        self.assertFalse(found["framecycler.example_add_version"].enabled_by_default)
        self.assertFalse(found["framecycler.example_per_stack_cdl"].enabled_by_default)
        self.assertFalse(found["framecycler.example_session_panel"].enabled_by_default)
        self.assertTrue(found["framecycler.ocio_api_loader"].enabled_by_default)


class TestExamplePerStackCdl(unittest.TestCase):
    def test_assign_stack_cdls_alternates_rgb(self):
        from src.framecycler.core import otio_model
        from src.framecycler.core.session import Session
        from src.framecycler.core.settings import Settings
        from src.framecycler.packages.api import PackageContext, EventBus

        # Import assign helper from shipped package module
        import importlib.util

        pkg_path = Path(__file__).resolve().parents[1] / "apps" / "example_per_stack_cdl" / "package.py"
        spec = importlib.util.spec_from_file_location("example_per_stack_cdl_pkg", pkg_path)
        self.assertIsNotNone(spec)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(config_dir=os.path.join(tmp, "cfg"))
            session = Session(settings)
            for name in ("a.exr", "b.exr", "c.exr"):
                path = os.path.join(tmp, name)
                open(path, "wb").close()
                clip = otio_model.clip_from_media(
                    path,
                    {
                        "fps": 24.0,
                        "frame_count": 3,
                        "start_frame": 1,
                        "width": 8,
                        "height": 8,
                    },
                )
                otio_model.append_shot(session.timeline, clip)
            session._notify()

            host = MagicMock()
            host.session = session
            host.settings = settings
            host.ocio_manager = MagicMock()
            host.viewport = MagicMock()
            host._apply_resolved_cdl = MagicMock()
            host.statusBar.return_value = MagicMock()

            ctx = PackageContext(
                package_id="framecycler.example_per_stack_cdl",
                package_dir=pkg_path.parent,
                logger=MagicMock(),
                host=host,
                event_bus=EventBus(),
                menu_actions=[],
            )
            count = module.assign_stack_cdls(ctx)
            self.assertEqual(count, 3)

            stacks = otio_model.shot_stacks(session.timeline)
            expected = [
                [1.45, 0.65, 0.65],
                [0.65, 1.45, 0.65],
                [0.65, 0.65, 1.45],
            ]
            for stack, slope in zip(stacks, expected):
                cdl = otio_model.get_cdl(stack)
                self.assertIsNotNone(cdl)
                self.assertEqual(cdl["slope"], slope)
            host._apply_resolved_cdl.assert_called_with(force=True)


if __name__ == "__main__":
    unittest.main()
