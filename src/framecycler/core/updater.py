"""In-app update checks via Velopack + GitHub Releases."""

from __future__ import annotations

import sys
from typing import Callable

import velopack
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QMessageBox, QProgressDialog, QWidget

GITHUB_REPO_URL = "https://github.com/bernhardkimbacher/Framecycler"
PACK_ID = "com.bernhardkimbacher.framecycler-reboot"


class UpdateUnavailableError(RuntimeError):
    """Raised when updates cannot run in the current environment."""


def _platform_channel() -> str:
    if sys.platform == "win32":
        return "win"
    if sys.platform == "darwin":
        return "osx"
    return "linux"


def _create_update_manager() -> velopack.UpdateManager:
    source = velopack.GithubSource(GITHUB_REPO_URL)
    options = velopack.UpdateOptions(
        AllowVersionDowngrade=False,
        MaximumDeltasBeforeFallback=10,
        ExplicitChannel=_platform_channel(),
    )
    try:
        return velopack.UpdateManager(source, options)
    except RuntimeError as exc:
        raise UpdateUnavailableError(
            "Updates are only available in packaged Framecycler builds installed via "
            "the Velopack installer or portable bundle."
        ) from exc


def _apply_progress(progress: QProgressDialog, value) -> None:
    if isinstance(value, (int, float)):
        progress.setRange(0, 100)
        progress.setValue(max(0, min(100, int(value))))
    elif isinstance(value, tuple) and len(value) >= 2:
        current, total = value[0], value[1]
        if total:
            progress.setRange(0, 100)
            progress.setValue(max(0, min(100, int(current * 100 / total))))
    QApplication.processEvents()


def _run_with_indeterminate_progress(
    parent: QWidget | None,
    label: str,
    operation: Callable[[], object],
) -> object:
    progress = QProgressDialog(label, None, 0, 0, parent)
    progress.setWindowTitle("Check for Updates")
    progress.setWindowModality(Qt.WindowModal)
    progress.setMinimumDuration(0)
    progress.setCancelButton(None)
    progress.show()
    QApplication.processEvents()
    try:
        return operation()
    finally:
        progress.close()


def check_for_updates_interactive(parent: QWidget | None = None) -> None:
    """Manual update flow triggered from Help > Check for Updates…"""
    try:
        manager = _create_update_manager()
    except UpdateUnavailableError as exc:
        QMessageBox.information(parent, "Check for Updates", str(exc))
        return

    try:
        update_info = _run_with_indeterminate_progress(
            parent,
            "Checking for updates…",
            manager.check_for_updates,
        )
    except Exception as exc:
        QMessageBox.warning(
            parent,
            "Check for Updates",
            f"Could not check for updates:\n{exc}",
        )
        return

    if update_info is None:
        current = manager.get_current_version()
        QMessageBox.information(
            parent,
            "Check for Updates",
            f"You are running the latest version ({current}).",
        )
        return

    target = update_info.TargetFullRelease
    new_version = str(target.Version)
    notes = (target.NotesMarkdown or "").strip()
    notes_block = f"\n\nRelease notes:\n{notes}" if notes else ""

    reply = QMessageBox.question(
        parent,
        "Update Available",
        f"A new version is available: {new_version}{notes_block}\n\n"
        "Download and install it now? The application will restart.",
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        QMessageBox.StandardButton.Yes,
    )
    if reply != QMessageBox.StandardButton.Yes:
        return

    progress = QProgressDialog("Downloading update…", "Cancel", 0, 100, parent)
    progress.setWindowTitle("Check for Updates")
    progress.setWindowModality(Qt.WindowModal)
    progress.setMinimumDuration(0)
    progress.show()
    QApplication.processEvents()

    def on_progress(value) -> None:
        if progress.wasCanceled():
            return
        _apply_progress(progress, value)

    try:
        manager.download_updates(update_info, on_progress)
    except Exception as exc:
        progress.close()
        QMessageBox.warning(
            parent,
            "Check for Updates",
            f"Download failed:\n{exc}",
        )
        return

    if progress.wasCanceled():
        progress.close()
        return

    progress.close()

    try:
        manager.apply_updates_and_restart(update_info)
    except Exception as exc:
        QMessageBox.warning(
            parent,
            "Check for Updates",
            f"Could not apply the update:\n{exc}",
        )
