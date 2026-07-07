#!/usr/bin/env python3
"""Build the Phase 0 Spike B pybind11 probe against aqt Qt + PySide6 runtime rpath."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_QT_ROOT = REPO_ROOT / ".qt"
SPIKE_DIR = REPO_ROOT / "spikes" / "rhi_spike_b"
BUILD_DIR = REPO_ROOT / "build" / "rhi_spike_b"


def find_cmake() -> str:
    if shutil.which("cmake"):
        return "cmake"
    import cmake

    cmake_bin = Path(cmake.CMAKE_BIN_DIR) / ("cmake.exe" if sys.platform == "win32" else "cmake")
    if cmake_bin.exists():
        return str(cmake_bin)
    raise FileNotFoundError("cmake not found")


def pyside6_qt_lib() -> Path:
    import PySide6

    return Path(PySide6.__file__).resolve().parent / "Qt" / "lib"


def resolve_qt_sdk(qt_version: str, qt_root: Path) -> Path:
    if sys.platform == "darwin":
        candidate = qt_root / qt_version / "macos"
    elif sys.platform == "win32":
        candidate = qt_root / qt_version / "msvc2019_64"
    else:
        candidate = qt_root / qt_version / "gcc_64"
    if not candidate.exists():
        raise FileNotFoundError(
            f"Qt SDK not found at {candidate}. "
            f"Install with: aqt install-qt ... {qt_version} ..."
        )
    return candidate


def build_spike(*, qt_version: str, qt_root: Path, clean: bool) -> Path:
    import PySide6

    qt_sdk = resolve_qt_sdk(qt_version, qt_root)
    pyside_qt_lib = pyside6_qt_lib()

    if clean and BUILD_DIR.exists():
        shutil.rmtree(BUILD_DIR)
    BUILD_DIR.mkdir(parents=True, exist_ok=True)

    cmake = find_cmake()
    configure_cmd = [
        cmake,
        "-S",
        str(SPIKE_DIR),
        "-B",
        str(BUILD_DIR),
        f"-DPython_EXECUTABLE={sys.executable}",
        "-DCMAKE_BUILD_TYPE=Release",
        f"-DQT_SDK_PATH={qt_sdk}",
        f"-DPYSIDE6_QT_LIB={pyside_qt_lib}",
    ]
    subprocess.run(configure_cmd, check=True)
    subprocess.run([cmake, "--build", str(BUILD_DIR), "--config", "Release"], check=True)

    ext_suffix = ".pyd" if sys.platform == "win32" else ".so"
    for path in BUILD_DIR.rglob(f"rhi_spike_b*{ext_suffix}"):
        if path.is_file():
            if sys.platform == "darwin":
                sign = subprocess.run(
                    ["codesign", "--force", "--sign", "-", str(path)],
                    capture_output=True,
                    text=True,
                )
                if sign.returncode != 0:
                    print(f"Warning: codesign failed: {sign.stderr.strip()}")
            return path

    raise FileNotFoundError(f"Built rhi_spike_b module not found under {BUILD_DIR}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Build Spike B Qt RHI probe module")
    parser.add_argument("--qt-version", default=None, help="Qt minor version (default: PySide6 version)")
    parser.add_argument("--qt-root", type=Path, default=DEFAULT_QT_ROOT, help="aqtinstall output root")
    parser.add_argument("--clean", action="store_true", help="Remove build directory before configuring")
    args = parser.parse_args()

    import PySide6

    qt_version = args.qt_version or PySide6.__version__
    print(f"Building Spike B against Qt SDK {qt_version} (PySide6 {PySide6.__version__})")
    module_path = build_spike(qt_version=qt_version, qt_root=args.qt_root, clean=args.clean)
    print(f"Built: {module_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
