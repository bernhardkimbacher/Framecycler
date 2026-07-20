"""Unit tests for packaging/bundle_filter.py."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packaging"))

from bundle_filter import (  # noqa: E402
    PYINSTALLER_EXCLUDES,
    filter_binaries,
    should_keep_binary,
)


class TestBundleFilter(unittest.TestCase):
    def test_denies_webengine(self):
        self.assertFalse(should_keep_binary(("/opt/QtWebEngineCore.so", ".")))

    def test_keeps_engine_and_oiio(self):
        self.assertTrue(
            should_keep_binary(("framecycler_engine.cpython-312.so", "framecycler"))
        )
        self.assertTrue(should_keep_binary(("/usr/lib/libOpenImageIO.2.5.dylib", ".")))

    def test_filter_binaries(self):
        entries = [
            ("/a/QtWebEngineCore.so", "."),
            ("/a/libOpenImageIO.so", "."),
            ("/a/framecycler_engine.so", "framecycler"),
        ]
        kept = filter_binaries(entries)
        paths = [e[0] for e in kept]
        self.assertNotIn("/a/QtWebEngineCore.so", paths)
        self.assertIn("/a/libOpenImageIO.so", paths)
        self.assertIn("/a/framecycler_engine.so", paths)

    def test_excludes_list_nonempty(self):
        self.assertIn("PySide6.QtWebEngineCore", PYINSTALLER_EXCLUDES)


if __name__ == "__main__":
    unittest.main()
