"""Resolve package search roots: shipped apps/, user packages, FRAMECYCLER_APPS."""

from __future__ import annotations

import os
import sys
from pathlib import Path

ENV_APPS = "FRAMECYCLER_APPS"


def shipped_apps_dir() -> Path:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        bundled = Path(sys._MEIPASS) / "apps"
        if bundled.is_dir():
            return bundled
    return Path(__file__).resolve().parents[3] / "apps"


def user_packages_dir(config_dir: str | Path | None = None) -> Path:
    base = Path(config_dir) if config_dir else Path(os.path.expanduser("~/.framecycler"))
    return base / "packages"


def env_apps_dir() -> Path | None:
    value = os.environ.get(ENV_APPS, "").strip()
    if not value:
        return None
    return Path(value).expanduser()


def ensure_user_packages_dir(config_dir: str | Path | None = None) -> Path:
    path = user_packages_dir(config_dir)
    path.mkdir(parents=True, exist_ok=True)
    return path


def package_search_roots(config_dir: str | Path | None = None) -> list[tuple[str, Path]]:
    """Return (source_label, path) roots in discovery order (first id wins)."""
    roots: list[tuple[str, Path]] = [
        ("shipped", shipped_apps_dir()),
        ("user", user_packages_dir(config_dir)),
    ]
    env_root = env_apps_dir()
    if env_root is not None:
        roots.append(("env", env_root))
    return roots
