"""Ensure a Qt SDK matching PySide6 is available for building framecycler_engine."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_QT_ROOT = REPO_ROOT / ".qt"


def pyside6_version() -> str:
    import PySide6

    return PySide6.__version__


def pyside6_qt_lib() -> Path:
    import PySide6

    return Path(PySide6.__file__).resolve().parent / "Qt" / "lib"


def qt_sdk_path(qt_root: Path, qt_version: str) -> Path:
    if sys.platform == "darwin":
        return qt_root / qt_version / "macos"
    if sys.platform == "win32":
        return qt_root / qt_version / "msvc2019_64"
    return qt_root / qt_version / "gcc_64"


def sdk_is_complete(sdk: Path) -> bool:
    qrhi_matches = list(sdk.glob("lib/QtGui.framework/Versions/A/Headers/*/QtGui/rhi/qrhi.h"))
    if qrhi_matches:
        baker_matches = list(
            sdk.glob("lib/QtShaderTools.framework/Versions/A/Headers/*/QtShaderTools/rhi/qshaderbaker.h")
        )
        if baker_matches:
            return True

    qrhi_linux = sdk / "include" / "QtGui" / "rhi" / "qrhi.h"
    baker_linux = sdk / "include" / "QtShaderTools" / "rhi" / "qshaderbaker.h"
    if qrhi_linux.exists() and baker_linux.exists():
        return True

    qrhi_win = list(sdk.glob("include/QtGui/*/QtGui/rhi/qrhi.h"))
    baker_win = list(sdk.glob("include/QtShaderTools/*/QtShaderTools/rhi/qshaderbaker.h"))
    return bool(qrhi_win and baker_win)


def install_qt_sdk(qt_root: Path, qt_version: str) -> None:
    # Always upgrade aqtinstall to ensure compatibility with latest Qt releases
    subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-U", "aqtinstall"], check=True)

    qt_root.mkdir(parents=True, exist_ok=True)
    arch = "clang_64" if sys.platform == "darwin" else ("win64_msvc2019_64" if sys.platform == "win32" else "gcc_64")
    os_name = "mac" if sys.platform == "darwin" else ("windows" if sys.platform == "win32" else "linux")
    cmd = [
        "aqt",
        "install-qt",
        os_name,
        "desktop",
        qt_version,
        arch,
        "-O",
        str(qt_root),
        "-m",
        "qtshadertools",
    ]
    print(f"Installing Qt {qt_version} via aqtinstall...")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "aqt install-qt failed"
        raise RuntimeError(detail)


def ensure_qt_sdk(qt_root: Path | None = None, *, install: bool = True) -> Path:
    root = qt_root or DEFAULT_QT_ROOT
    version = pyside6_version()
    sdk = qt_sdk_path(root, version)
    if sdk_is_complete(sdk):
        return sdk
    if not install:
        raise FileNotFoundError(
            f"Qt SDK not found at {sdk}. Install with:\n"
            f"  python scripts/qt_sdk.py --install\n"
            f"or re-run build.py (auto-installs when missing)."
        )

    # 1. Attempt primary installation
    try:
        install_qt_sdk(root, version)
        if sdk_is_complete(sdk):
            return sdk
    except RuntimeError as primary_exc:
        print(f"Warning: Failed to install primary Qt SDK {version}: {primary_exc}", file=sys.stderr)

    # 2. Loop through stable fallbacks
    fallbacks = ["6.8.1", "6.8.0", "6.7.3"]
    for fallback in fallbacks:
        print(f"Attempting fallback stable Qt SDK {fallback}...", file=sys.stderr)
        sdk = qt_sdk_path(root, fallback)
        if sdk_is_complete(sdk):
            return sdk
        try:
            install_qt_sdk(root, fallback)
            if sdk_is_complete(sdk):
                return sdk
        except RuntimeError as fallback_exc:
            print(f"Warning: Fallback Qt SDK {fallback} install failed: {fallback_exc}", file=sys.stderr)

    # 3. Query available versions from aqt list-qt to assist debugging on final failure
    arch = "clang_64" if sys.platform == "darwin" else ("win64_msvc2019_64" if sys.platform == "win32" else "gcc_64")
    os_name = "mac" if sys.platform == "darwin" else ("windows" if sys.platform == "win32" else "linux")
    try:
        print("Debugging Qt repository: Querying available versions via 'aqt list-qt'...", file=sys.stderr)
        avail = subprocess.check_output(["aqt", "list-qt", os_name, "desktop"], text=True)
        print(f"Available Qt versions for {os_name} desktop:\n{avail}", file=sys.stderr)
    except Exception as list_exc:
        print(f"Failed to query available Qt versions: {list_exc}", file=sys.stderr)

    raise RuntimeError(
        f"Failed to resolve any functional Qt SDK. Checked primary '{version}' and fallbacks {fallbacks}."
    )


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Ensure Qt SDK for framecycler_engine build")
    parser.add_argument("--install", action="store_true", help="Install Qt SDK if missing")
    parser.add_argument("--qt-root", type=Path, default=DEFAULT_QT_ROOT, help="aqtinstall output root")
    args = parser.parse_args()

    try:
        sdk = ensure_qt_sdk(args.qt_root, install=args.install)
    except (FileNotFoundError, RuntimeError) as exc:
        print(exc, file=sys.stderr)
        return 1

    print(sdk)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
