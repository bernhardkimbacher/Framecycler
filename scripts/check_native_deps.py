#!/usr/bin/env python3
"""Verify installed OpenImageIO / FFmpeg match packaging/native-deps.md pins.

Exit 0 when versions match expected prefixes (or when FRAMECYCLER_SKIP_NATIVE_DEP_CHECK=1).
Exit 1 on mismatch when --strict (used by package.yml).
"""

from __future__ import annotations

import argparse
import os
import platform
import re
import subprocess
import sys


# Expected version prefixes — keep in sync with packaging/native-deps.md
MACOS_EXPECTED = {
    "openimageio": "3.1.",
    "ffmpeg": "8.",
}


def _run(cmd: list[str]) -> str:
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    return (result.stdout or "") + (result.stderr or "")


def _brew_version(formula: str) -> str | None:
    out = _run(["brew", "info", "--json=v2", formula])
    # Prefer json when available; fall back to text scrape.
    m = re.search(r'"version"\s*:\s*"([^"]+)"', out)
    if m:
        return m.group(1)
    out = _run(["brew", "info", formula])
    m = re.search(r"stable\s+(\S+)", out)
    return m.group(1) if m else None


def _dpkg_version(package: str) -> str | None:
    out = _run(["dpkg-query", "-W", "-f=${Version}", package])
    ver = out.strip()
    return ver or None


def check_macos(strict: bool) -> int:
    errors: list[str] = []
    for formula, prefix in MACOS_EXPECTED.items():
        ver = _brew_version(formula)
        if ver is None:
            errors.append(f"brew formula not found: {formula}")
            continue
        if not ver.startswith(prefix):
            errors.append(
                f"{formula} version {ver!r} does not start with expected {prefix!r}"
            )
        else:
            print(f"OK {formula}={ver} (prefix {prefix})")
    if errors and strict:
        for e in errors:
            print(f"ERROR: {e}", file=sys.stderr)
        return 1
    for e in errors:
        print(f"WARNING: {e}", file=sys.stderr)
    return 0


def check_linux(strict: bool) -> int:
    # Ubuntu pins to the distro image; just confirm packages are installed.
    packages = ("libopenimageio-dev", "libavformat-dev")
    errors: list[str] = []
    for pkg in packages:
        ver = _dpkg_version(pkg)
        if not ver:
            errors.append(f"apt package missing: {pkg}")
        else:
            print(f"OK {pkg}={ver}")
    if errors and strict:
        for e in errors:
            print(f"ERROR: {e}", file=sys.stderr)
        return 1
    for e in errors:
        print(f"WARNING: {e}", file=sys.stderr)
    return 0


def check_windows() -> int:
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    manifest = os.path.join(root, "vcpkg.json")
    if not os.path.isfile(manifest):
        print("ERROR: vcpkg.json missing", file=sys.stderr)
        return 1
    print(f"OK vcpkg manifest present: {manifest}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Fail (exit 1) on version mismatch / missing packages",
    )
    args = parser.parse_args()

    if os.environ.get("FRAMECYCLER_SKIP_NATIVE_DEP_CHECK") == "1":
        print("Skipping native dep check (FRAMECYCLER_SKIP_NATIVE_DEP_CHECK=1)")
        return 0

    system = platform.system()
    if system == "Darwin":
        return check_macos(args.strict)
    if system == "Linux":
        return check_linux(args.strict)
    if system == "Windows":
        return check_windows()
    print(f"Unsupported platform: {system}", file=sys.stderr)
    return 1 if args.strict else 0


if __name__ == "__main__":
    raise SystemExit(main())
