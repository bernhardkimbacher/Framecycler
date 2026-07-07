import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
GENERATE_VERSION = ROOT / "scripts" / "generate_version.py"


class TestGenerateVersion(unittest.TestCase):
    def tearDown(self):
        subprocess.run(
            [sys.executable, str(GENERATE_VERSION)],
            cwd=ROOT,
            check=False,
            capture_output=True,
        )

    def test_env_version_is_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "_version.py"
            env = os.environ.copy()
            env["APP_VERSION"] = "1.2.3"
            result = subprocess.run(
                [sys.executable, str(GENERATE_VERSION)],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            generated = ROOT / "src" / "framecycler" / "_version.py"
            self.assertTrue(generated.exists())
            content = generated.read_text(encoding="utf-8")
            self.assertIn('__version__ = "1.2.3"', content)

    def test_cli_version_strips_v_prefix(self):
        result = subprocess.run(
            [sys.executable, str(GENERATE_VERSION), "--version", "v9.8.7"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        content = (ROOT / "src" / "framecycler" / "_version.py").read_text(encoding="utf-8")
        self.assertIn('__version__ = "9.8.7"', content)


class TestUpdater(unittest.TestCase):
    def test_create_update_manager_requires_packaged_build(self):
        from src.framecycler.core.updater import (
            UpdateUnavailableError,
            _create_update_manager,
        )

        with self.assertRaises(UpdateUnavailableError):
            _create_update_manager()

    @patch("src.framecycler.core.updater.velopack.UpdateManager")
    @patch("src.framecycler.core.updater.velopack.GithubSource")
    def test_check_for_updates_interactive_when_up_to_date(self, mock_source, mock_manager_cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication, QMessageBox

        app = QApplication.instance() or QApplication([])

        manager = MagicMock()
        manager.check_for_updates.return_value = None
        manager.get_current_version.return_value = "1.0.0"
        mock_manager_cls.return_value = manager

        with patch.object(QMessageBox, "information") as info_box:
            from src.framecycler.core.updater import check_for_updates_interactive

            check_for_updates_interactive()
            info_box.assert_called_once()
            self.assertIn("latest version", info_box.call_args[0][2])

        del app


if __name__ == "__main__":
    unittest.main()
