# -*- mode: python ; coding: utf-8 -*-

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_dynamic_libs

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

a = Analysis(
    [str(root / "src" / "framecycler" / "__main__.py")],
    pathex=[str(root / "src")],
    binaries=engine_bins + pyside6_qsb_binaries() + collect_dynamic_libs("OpenImageIO"),
    datas=[
        (str(root / "src" / "framecycler" / "color" / "studio_config"), "framecycler/color/studio_config"),
        (str(root / "src" / "framecycler" / "render" / "shaders"), "framecycler/render/shaders"),
        (str(root / "assets" / "icons"), "assets/icons"),
    ],
    hiddenimports=["OpenImageIO"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

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
    codesign_identity=None,
    entitlements_file=None,
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
