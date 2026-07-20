# -*- mode: python ; coding: utf-8 -*-

import os
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_dynamic_libs

# Ensure packaging/ is importable when PyInstaller runs this spec.
sys.path.insert(0, str(Path(SPECPATH).resolve()))
from bundle_filter import PYINSTALLER_EXCLUDES, filter_binaries  # noqa: E402

block_cipher = None
root = Path(SPECPATH).resolve().parent


def icon_path() -> str:
    icons = root / "assets" / "icons"
    if sys.platform == "darwin" and (icons / "app_icon.icns").is_file():
        return str(icons / "app_icon.icns")
    if sys.platform == "win32" and (icons / "app_icon.ico").is_file():
        return str(icons / "app_icon.ico")
    return str(icons / "app_icon_512.png")


engine_ext = ".pyd" if sys.platform == "win32" else ".so"
engine_bins = [
    (str(path), "framecycler")
    for path in (root / "src" / "framecycler").glob(f"framecycler_engine*{engine_ext}")
]


def pyside6_qsb_binaries() -> list[tuple[str, str]]:
    try:
        import PySide6
    except ImportError:
        return []
    pkg = Path(PySide6.__file__).resolve().parent
    for name in ("qsb.exe", "qsb"):
        candidate = pkg / name
        if candidate.is_file():
            return [(str(candidate), "PySide6")]
    return []


# macOS: optional Developer ID from env (release package.yml). Leave unset for local.
_macos_identity = os.environ.get("MACOS_CODESIGN_IDENTITY") or None
_macos_entitlements = os.environ.get("MACOS_ENTITLEMENTS") or None
if _macos_entitlements and not Path(_macos_entitlements).is_file():
    _macos_entitlements = None
if sys.platform == "darwin" and _macos_entitlements is None:
    _default_ent = root / "packaging" / "macos" / "entitlements.plist"
    if _macos_identity and _default_ent.is_file():
        _macos_entitlements = str(_default_ent)

a = Analysis(
    [str(root / "src" / "framecycler" / "__main__.py")],
    pathex=[str(root / "src")],
    binaries=engine_bins + pyside6_qsb_binaries() + collect_dynamic_libs("OpenImageIO"),
    datas=[
        (str(root / "src" / "framecycler" / "color" / "studio_config"), "framecycler/color/studio_config"),
        (str(root / "src" / "framecycler" / "render" / "shaders"), "framecycler/render/shaders"),
        (str(root / "assets" / "icons"), "assets/icons"),
        (str(root / "apps"), "apps"),
    ],
    hiddenimports=["OpenImageIO"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=PYINSTALLER_EXCLUDES,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

a.binaries = filter_binaries(a.binaries)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

# TODO(Authenticode): After Azure Trusted Signing / org code-signing cert is
# provisioned, sign Framecycler.exe (and shipped DLLs) post-COLLECT / pre-vpk.
# Not implemented yet — needs account signup.

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Framecycler",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=_macos_identity if sys.platform == "darwin" else None,
    entitlements_file=_macos_entitlements if sys.platform == "darwin" else None,
    icon=icon_path(),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="Framecycler",
)

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="Framecycler-Reboot.app",
        icon=icon_path(),
        bundle_identifier="com.bernhardkimbacher.framecycler-reboot",
        info_plist={
            "CFBundleDisplayName": "Framecycler Reboot",
            "CFBundleName": "Framecycler Reboot",
            "CFBundleShortVersionString": "0.2.3",
            "NSHighResolutionCapable": True,
        },
    )
