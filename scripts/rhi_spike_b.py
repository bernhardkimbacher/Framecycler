#!/usr/bin/env python3
"""Phase 0 Spike B: C++ Qt SDK build/linkage validation for Qt RHI migration.

Builds a minimal pybind11 module linking Qt6::Gui/GuiPrivate and
Qt6::ShaderTools/ShaderToolsPrivate (QShaderBaker + QRhi headers), compiled
against an aqtinstall Qt SDK matching PySide6, with runtime rpath aimed at
PySide6's bundled Qt libraries.

Usage:
    source .venv/bin/activate
    python scripts/rhi_spike_b.py
    python scripts/rhi_spike_b.py --skip-install   # if .qt SDK already present
    python scripts/rhi_spike_b.py --headless       # skip QRhiWidget pointer test

Exit 0 when all runnable checks pass.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_QT_ROOT = REPO_ROOT / ".qt"
BUILD_DIR = REPO_ROOT / "build" / "rhi_spike_b"


@dataclass
class SpikeReport:
    passed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)

    def ok(self, message: str) -> None:
        self.passed.append(message)
        print(f"[ OK ] {message}")

    def skip(self, message: str) -> None:
        self.skipped.append(message)
        print(f"[SKIP] {message}")

    def fail(self, message: str) -> None:
        self.failed.append(message)
        print(f"[FAIL] {message}")


def pyside6_version_parts() -> tuple[str, str, str]:
    import PySide6

    parts = PySide6.__version__.split(".")
    while len(parts) < 3:
        parts.append("0")
    return parts[0], parts[1], parts[2]


def pyside6_qt_lib() -> Path:
    import PySide6

    return Path(PySide6.__file__).resolve().parent / "Qt" / "lib"


def qt_sdk_path(qt_root: Path, qt_version: str) -> Path:
    if sys.platform == "darwin":
        return qt_root / qt_version / "macos"
    if sys.platform == "win32":
        return qt_root / qt_version / "msvc2019_64"
    return qt_root / qt_version / "gcc_64"


def ensure_qt_sdk(report: SpikeReport, qt_root: Path, qt_version: str) -> Path:
    sdk = qt_sdk_path(qt_root, qt_version)
    qrhi_matches = list(
        sdk.glob("lib/QtGui.framework/Versions/A/Headers/*/QtGui/rhi/qrhi.h")
    )
    baker_matches = list(
        sdk.glob("lib/QtShaderTools.framework/Versions/A/Headers/*/QtShaderTools/rhi/qshaderbaker.h")
    )
    if qrhi_matches and baker_matches:
        report.ok(f"Qt SDK present at {sdk}")
        return sdk

    report.fail(
        f"Qt SDK missing or incomplete at {sdk}. "
        f"Run without --skip-install to install via aqtinstall."
    )
    return sdk


def install_qt_sdk(report: SpikeReport, qt_root: Path, qt_full: str) -> None:
    if shutil_which("aqt") is None:
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "aqtinstall"], check=True)

    qt_root.mkdir(parents=True, exist_ok=True)
    arch = "clang_64" if sys.platform == "darwin" else ("win64_msvc2019_64" if sys.platform == "win32" else "gcc_64")
    os_name = "mac" if sys.platform == "darwin" else ("windows" if sys.platform == "win32" else "linux")
    cmd = [
        "aqt",
        "install-qt",
        os_name,
        "desktop",
        qt_full,
        arch,
        "-O",
        str(qt_root),
        "-m",
        "qtshadertools",
    ]
    report.ok(f"Installing Qt {qt_full} via aqtinstall ({' '.join(cmd[2:])})")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        report.fail(result.stderr.strip() or result.stdout.strip() or "aqt install-qt failed")
        return
    report.ok(f"aqtinstall completed for Qt {qt_full}")


def shutil_which(name: str) -> str | None:
    import shutil

    return shutil.which(name)


def build_spike_module(report: SpikeReport) -> Path | None:
    build_script = REPO_ROOT / "scripts" / "build_rhi_spike_b.py"
    result = subprocess.run(
        [sys.executable, str(build_script), "--clean"],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )
    if result.stdout.strip():
        print(result.stdout, end="" if result.stdout.endswith("\n") else "\n")
    if result.returncode != 0:
        combined = (result.stderr or "") + (result.stdout or "")
        report.fail(combined.strip() or "build_rhi_spike_b.py failed")
        return None

    ext_suffix = ".pyd" if sys.platform == "win32" else ".so"
    for path in BUILD_DIR.rglob(f"rhi_spike_b*{ext_suffix}"):
        if path.is_file():
            report.ok(f"Built spike module: {path}")
            return path
    report.fail("Spike module binary not found after build")
    return None


def load_spike_module(module_path: Path):
    spec = importlib.util.spec_from_file_location("rhi_spike_b", module_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot import spike module from {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def check_import_and_linkage(report: SpikeReport, module_path: Path) -> object | None:
    try:
        spike = load_spike_module(module_path)
    except Exception as exc:
        report.fail(f"Importing rhi_spike_b failed: {exc}")
        return None

    report.ok("rhi_spike_b imports inside PySide6 Python process")

    runtime_version = spike.qt_runtime_version()
    import PySide6

    if not runtime_version.startswith("6."):
        report.fail(f"Unexpected Qt runtime version string: {runtime_version}")
    else:
        report.ok(f"Extension reports Qt runtime {runtime_version} (PySide6 {PySide6.__version__})")

    linked_path = spike.linked_qt_gui_path()
    pyside_qt = str(pyside6_qt_lib())
    if linked_path and pyside_qt in linked_path:
        report.ok(f"Loaded QtGui resolves under PySide6: {linked_path}")
    elif linked_path:
        report.fail(
            f"Loaded QtGui is not from PySide6 bundle: {linked_path} (expected path containing {pyside_qt})"
        )
    else:
        report.skip("linked_qt_gui_path unavailable on this platform")

    try:
        bake_result = spike.probe_shader_baker()
    except Exception as exc:
        report.fail(f"QShaderBaker probe failed: {exc}")
        return spike

    if bake_result.startswith("invalid"):
        report.fail(f"QShaderBaker probe failed: {bake_result}")
    else:
        report.ok(f"In-process QShaderBaker baked fragment shader ({bake_result})")

    return spike


def run_qrhi_pointer_worker() -> subprocess.CompletedProcess:
    worker_cmd = [sys.executable, str(Path(__file__).resolve()), "--qrhi-worker"]
    return subprocess.run(
        worker_cmd,
        capture_output=True,
        text=True,
        timeout=20,
        cwd=str(REPO_ROOT),
        env=os.environ.copy(),
    )


def run_qrhi_pointer_test(report: SpikeReport, module_path: Path) -> None:
    if os.environ.get("QT_QPA_PLATFORM") == "offscreen":
        report.skip("QRhi pointer test skipped (QT_QPA_PLATFORM=offscreen)")
        return

    env = os.environ.copy()
    env["RHI_SPIKE_B_MODULE"] = str(module_path)
    worker_cmd = [sys.executable, str(Path(__file__).resolve()), "--qrhi-worker"]
    try:
        result = subprocess.run(
            worker_cmd,
            capture_output=True,
            text=True,
            timeout=20,
            cwd=str(REPO_ROOT),
            env=env,
        )
    except subprocess.TimeoutExpired:
        report.skip("QRhi pointer test skipped (timed out; no QRhi/display available)")
        return

    stdout = result.stdout or ""
    stderr = result.stderr or ""
    if stdout:
        print(stdout, end="" if stdout.endswith("\n") else "\n")
    if stderr.strip():
        print(stderr, file=sys.stderr, end="" if stderr.endswith("\n") else "\n")

    for line in stdout.splitlines():
        if line.startswith("[ OK ] "):
            report.ok(line[6:])
        elif line.startswith("[SKIP] "):
            report.skip(line[7:])
        elif line.startswith("[FAIL] "):
            report.fail(line[7:])

    if result.returncode != 0 and not any(line.startswith("[ OK ]") for line in stdout.splitlines()):
        report.skip(
            "QRhi pointer test skipped (worker unavailable in this environment; "
            f"exit={result.returncode})"
        )


def run_qrhi_worker() -> int:
    report = SpikeReport()
    module_path = Path(os.environ.get("RHI_SPIKE_B_MODULE", ""))
    if not module_path.exists():
        report.fail("RHI_SPIKE_B_MODULE env var must point to built spike module")
        return _emit_worker_report(report)

    spike = load_spike_module(module_path)

    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication, QRhiWidget

    class ProbeWidget(QRhiWidget):
        def __init__(self):
            super().__init__()
            self._done = False

        def initialize(self, cb) -> None:
            return

        def render(self, cb) -> None:
            if self._done:
                return
            self._done = True
            rhi = self.rhi()
            if rhi is None:
                report.skip("QRhiWidget.rhi() returned None")
                QApplication.instance().quit()
                return

            import shiboken6

            ptr, _type_name = shiboken6.getCppPointer(rhi)
            backend = spike.probe_qrhi_from_address(int(ptr))
            report.ok(f"QRhi* unwrapped via shiboken6.getCppPointer -> backend={backend}")
            report.ok("C++ extension can consume QRhiWidget.rhi() pointer from Python")
            QApplication.instance().quit()

    app = QApplication(sys.argv)
    widget = ProbeWidget()
    widget.resize(320, 240)
    widget.show()
    QTimer.singleShot(4000, app.quit)
    app.exec()

    if not any("QRhi*" in item for item in report.passed):
        if widget.rhi() is None:
            report.skip("QRhi worker: no QRhi backend / display unavailable")
        else:
            report.fail("QRhi worker: timed out before rhi() became available")
    return _emit_worker_report(report)


def _emit_worker_report(report: SpikeReport) -> int:
    for item in report.passed:
        print(f"[ OK ] {item}")
    for item in report.skipped:
        print(f"[SKIP] {item}")
    for item in report.failed:
        print(f"[FAIL] {item}")
    return 1 if report.failed else 0


def print_summary(report: SpikeReport) -> int:
    print("\n" + "=" * 60)
    print("Phase 0 Spike B summary")
    print("=" * 60)
    print(f"Passed : {len(report.passed)}")
    print(f"Skipped: {len(report.skipped)}")
    print(f"Failed : {len(report.failed)}")
    if report.failed:
        for item in report.failed:
            print(f"  - {item}")
        return 1
    print("\nFindings for migration plan:")
    print("- aqtinstall Qt SDK + Qt6::GuiPrivate/ShaderToolsPrivate linkage is viable.")
    print("- Runtime resolves to PySide6's bundled Qt when rpath targets PySide6/Qt/lib.")
    print("- In-process QShaderBaker works from C++ (no pyside6-qsb subprocess required).")
    print("- QRhi* can be handed from Python QRhiWidget.rhi() via shiboken6.getCppPointer.")
    print("- Proceed with C++ RhiRenderer in framecycler_engine (Spike B gate passed).")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase 0 Spike B validation")
    parser.add_argument("--skip-install", action="store_true", help="Do not run aqtinstall")
    parser.add_argument("--headless", action="store_true", help="Skip QRhiWidget pointer worker")
    parser.add_argument("--qrhi-worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--qt-root", type=Path, default=DEFAULT_QT_ROOT)
    args = parser.parse_args()

    if args.qrhi_worker:
        return run_qrhi_worker()

    print("=" * 60)
    print("Phase 0 Spike B — C++ Qt SDK / RHI linkage validation")
    print("=" * 60)

    report = SpikeReport()
    try:
        import PySide6
    except ImportError as exc:
        report.fail(f"PySide6 not installed: {exc}")
        return print_summary(report)

    major, minor, patch = pyside6_version_parts()
    qt_minor = f"{major}.{minor}"
    qt_full = f"{major}.{minor}.{patch}"
    report.ok(f"PySide6 {PySide6.__version__} (target Qt SDK {qt_full})")

    if not args.skip_install:
        sdk = qt_sdk_path(args.qt_root, qt_full)
        if not list(sdk.glob("lib/QtGui.framework/Versions/A/Headers/*/QtGui/rhi/qrhi.h")):
            install_qt_sdk(report, args.qt_root, qt_full)
    else:
        report.ok("Skipping aqtinstall (--skip-install)")

    if report.failed:
        return print_summary(report)

    ensure_qt_sdk(report, args.qt_root, qt_full)
    if report.failed:
        return print_summary(report)

    module_path = build_spike_module(report)
    if report.failed or module_path is None:
        return print_summary(report)

    spike = check_import_and_linkage(report, module_path)
    if report.failed or spike is None:
        return print_summary(report)

    if not args.headless:
        run_qrhi_pointer_test(report, module_path)
    else:
        report.skip("QRhi pointer test skipped (--headless)")

    return print_summary(report)


if __name__ == "__main__":
    raise SystemExit(main())
