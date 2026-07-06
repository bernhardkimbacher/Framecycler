#!/usr/bin/env python3
"""Generate platform bundle icons (.ico / .icns) from PNG sources."""

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ICONS = ROOT / "assets" / "icons"


def generate_ico() -> bool:
    try:
        from PIL import Image
    except ImportError:
        print("[WARN] Pillow not installed; skipping .ico generation.")
        return False

    images = []
    for filename in ("app_icon_128.png", "app_icon_256.png", "app_icon_512.png", "app_icon.png"):
        path = ICONS / filename
        if path.is_file():
            images.append(Image.open(path).convert("RGBA"))

    if not images:
        print("[WARN] No PNG icons found for .ico generation.")
        return False

    out = ICONS / "app_icon.ico"
    images[0].save(out, format="ICO", sizes=[(img.width, img.height) for img in images])
    print(f"Wrote {out}")
    return True


def generate_icns() -> bool:
    if sys.platform != "darwin":
        print("[INFO] Skipping .icns generation (not macOS).")
        return False

    iconset = ICONS / "app_icon.iconset"
    if iconset.exists():
        shutil.rmtree(iconset)
    iconset.mkdir(parents=True)

    mapping = {
        "app_icon_128.png": "icon_128x128.png",
        "app_icon_256.png": "icon_256x256.png",
        "app_icon_512.png": "icon_512x512.png",
        "app_icon_512.png@2x": "icon_256x256@2x.png",
        "app_icon.png@2x": "icon_512x512@2x.png",
    }

    sources = {
        "app_icon_128.png": ICONS / "app_icon_128.png",
        "app_icon_256.png": ICONS / "app_icon_256.png",
        "app_icon_512.png": ICONS / "app_icon_512.png",
        "app_icon_512.png@2x": ICONS / "app_icon_512.png",
        "app_icon.png@2x": ICONS / "app_icon.png",
    }

    for key, dest_name in mapping.items():
        src = sources[key]
        if not src.is_file():
            print(f"[WARN] Missing {src}; skipping .icns generation.")
            shutil.rmtree(iconset, ignore_errors=True)
            return False
        shutil.copy2(src, iconset / dest_name)

    out = ICONS / "app_icon.icns"
    subprocess.run(
        ["iconutil", "-c", "icns", str(iconset), "-o", str(out)],
        check=True,
    )
    shutil.rmtree(iconset)
    print(f"Wrote {out}")
    return True


def main() -> int:
    if sys.platform == "darwin":
        return 0 if generate_icns() else 1
    if sys.platform == "win32":
        return 0 if generate_ico() else 1
    return 0 if generate_ico() else 0


if __name__ == "__main__":
    sys.exit(main())
