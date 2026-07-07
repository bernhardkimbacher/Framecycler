import subprocess
from pathlib import Path

APP_NAME = "Framecycler Reboot"
__version__ = "0.2.1"
COPYRIGHT_HOLDER = "Bernie Kimbacher"
COPYRIGHT_YEAR = "2026"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _git_info() -> dict | None:
    root = _repo_root()
    try:
        commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=root, stderr=subprocess.DEVNULL
        ).decode().strip()
        commit_short = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], cwd=root, stderr=subprocess.DEVNULL
        ).decode().strip()
        branch = subprocess.check_output(
            ["git", "branch", "--show-current"], cwd=root, stderr=subprocess.DEVNULL
        ).decode().strip() or "detached"
        dirty = (
            subprocess.call(["git", "diff", "--quiet"], cwd=root) != 0
            or subprocess.call(["git", "diff", "--cached", "--quiet"], cwd=root) != 0
        )
        return {
            "version": __version__,
            "commit": commit,
            "commit_short": commit_short,
            "branch": branch,
            "dirty": dirty,
        }
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def get_build_info() -> dict:
    try:
        from .. import _version

        return {
            "version": getattr(_version, "__version__", __version__),
            "commit": _version.__commit__,
            "commit_short": _version.__commit_short__,
            "branch": _version.__branch__,
            "dirty": _version.__dirty__,
        }
    except ImportError:
        pass

    runtime = _git_info()
    if runtime:
        return runtime

    return {
        "version": __version__,
        "commit": "unknown",
        "commit_short": "unknown",
        "branch": "unknown",
        "dirty": False,
    }


def get_application_version() -> str:
    info = get_build_info()
    label = f"{info['version']} ({info['commit_short']})"
    if info["dirty"]:
        label += "-modified"
    return label


def get_about_text() -> str:
    info = get_build_info()
    lines = [
        f"Version {info['version']}",
        "",
        f"Build commit: {info['commit']}",
        f"Branch: {info['branch']}",
    ]
    if info["dirty"]:
        lines.append("Working tree: modified (uncommitted changes)")
    lines.extend(
        [
            "",
            f"Copyright © {COPYRIGHT_YEAR} {COPYRIGHT_HOLDER}",
            "",
            "This program is free software: you can redistribute it and/or modify "
            "it under the terms of the GNU Affero General Public License as published "
            "by the Free Software Foundation, either version 3 of the License, or "
            "(at your option) any later version.",
            "",
            "This program is distributed in the hope that it will be useful, "
            "but WITHOUT ANY WARRANTY; without even the implied warranty of "
            "MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the "
            "GNU Affero General Public License for more details.",
        ]
    )
    return "\n".join(lines)
