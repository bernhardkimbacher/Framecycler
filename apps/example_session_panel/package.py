"""Example package demonstrating the expanded Package API surface."""

from __future__ import annotations

import struct
import tempfile
from pathlib import Path

import numpy as np
from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QAction, QColor, QFont, QPainter
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

try:
    # Prefer the same import root the host uses. ``src.framecycler`` succeeds when
    # the repo root is on sys.path and creates a *different* Package class object,
    # so isinstance(..., Package) fails in PackageManager.
    from framecycler.decoders.base import BaseDecoder
    from framecycler.packages.api import Package, PackageContext, PackageEvents
except ImportError:
    from src.framecycler.decoders.base import BaseDecoder
    from src.framecycler.packages.api import Package, PackageContext, PackageEvents


class FcPanelDecoder(BaseDecoder):
    """Stub still decoder for unique ``.fcpanel`` files (RGBA float16)."""

    def __init__(self, path: str):
        self.path = path
        self._width = 64
        self._height = 48
        self._label = Path(path).stem

    def get_metadata(self):
        return {
            "width": self._width,
            "height": self._height,
            "fps": 24.0,
            "frame_count": 1,
            "start_frame": 1,
            "timecode_start": "01:00:00:00",
            "channels": ["R", "G", "B", "A"],
            "has_alpha": True,
            "pixel_aspect_ratio": 1.0,
            "decoder": "FcPanelDecoder",
        }

    def read_frame(self, frame_index: int, resolution_scale: float = 1.0):
        scale = max(0.01, min(1.0, float(resolution_scale)))
        w = max(1, int(round(self._width * scale)))
        h = max(1, int(round(self._height * scale)))
        rgb = np.zeros((h, w, 4), dtype=np.float16)
        rgb[..., 0] = 0.2
        rgb[..., 1] = 0.55
        rgb[..., 2] = 0.85
        rgb[..., 3] = 1.0
        return {
            "data": rgb,
            "channels": ["R", "G", "B", "A"],
            "frame_index": frame_index,
            "timecode": "01:00:00:00",
        }

    def get_file_path(self, frame_index: int, fallback_nearest: bool = False) -> str | None:
        return self.path

    def close(self) -> None:
        pass


def write_fcpanel_stub(path: Path) -> None:
    """Write a tiny marker file recognized by extension (decoder ignores contents)."""
    path.write_bytes(b"FCPANEL\0" + struct.pack("<II", 64, 48))


class _SessionPanel(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        self._label = QLabel("Waiting for events…")
        self._label.setWordWrap(True)
        self._label.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(self._label)
        layout.addStretch()
        self.session_count = 0
        self.frame_updates = 0
        self.last_frame: int | None = None
        self.last_timecode: str = ""
        self.last_decoder_note: str = ""
        self.prefix: str = "FC"
        self._refresh()

    def set_prefix(self, prefix: str) -> None:
        self.prefix = prefix or "FC"
        self._refresh()

    def on_session_changed(self) -> None:
        self.session_count += 1
        self._refresh()

    def on_frame_changed(self, frame: int, timecode: str) -> None:
        self.frame_updates += 1
        self.last_frame = int(frame)
        self.last_timecode = str(timecode)
        self._refresh()

    def note_decoder_load(self, path: str) -> None:
        self.last_decoder_note = f"Loaded via FcPanelDecoder: {Path(path).name}"
        self._refresh()

    def _refresh(self) -> None:
        frame_line = (
            f"FRAME_CHANGED updates ×{self.frame_updates}\n"
            f"Last: frame={self.last_frame}  tc={self.last_timecode or '—'}"
        )
        self._label.setText(
            f"{self.prefix} Session Events\n"
            f"SESSION_CHANGED ×{self.session_count}\n"
            f"{frame_line}\n"
            f"Keybind: Ctrl+Shift+S → status bar\n"
            f"{self.last_decoder_note or '(no .fcpanel load yet)'}"
        )


class ExampleSessionPanelPackage(Package):
    def activate(self, ctx: PackageContext) -> None:
        self._ctx = ctx
        self._panel_holder: dict[str, _SessionPanel | None] = {"widget": None}
        self._last_frame = 0
        self._last_timecode = ""

        ctx.define_settings_schema(
            [
                {
                    "key": "show_badge",
                    "type": "bool",
                    "label": "Show HUD badge",
                    "default": True,
                },
                {
                    "key": "prefix",
                    "type": "string",
                    "label": "Label prefix",
                    "default": "FC",
                },
            ]
        )

        def factory(parent: QWidget) -> QWidget:
            panel = _SessionPanel(parent)
            panel.set_prefix(str(ctx.get_setting("prefix", "FC")))
            self._panel_holder["widget"] = panel
            return panel

        ctx.register_panel(
            "session",
            title="Session Events",
            factory=factory,
            default_area="right",
            visible_by_default=False,
        )

        def on_session_changed() -> None:
            panel = self._panel_holder["widget"]
            if panel is not None:
                panel.set_prefix(str(ctx.get_setting("prefix", "FC")))
                panel.on_session_changed()

        def on_frame_changed(frame: int, timecode: str) -> None:
            self._last_frame = int(frame)
            self._last_timecode = str(timecode)
            panel = self._panel_holder["widget"]
            if panel is not None:
                panel.set_prefix(str(ctx.get_setting("prefix", "FC")))
                panel.on_frame_changed(frame, timecode)

        ctx.subscribe(PackageEvents.SESSION_CHANGED, on_session_changed)
        ctx.subscribe(PackageEvents.FRAME_CHANGED, on_frame_changed)

        def status_snapshot() -> None:
            if self._last_timecode or self._last_frame:
                ctx.status(
                    f"{ctx.get_setting('prefix', 'FC')} frame={self._last_frame} "
                    f"tc={self._last_timecode}"
                )
            else:
                ctx.status(f"{ctx.get_setting('prefix', 'FC')}: no frame yet")

        ctx.register_keybind(
            "status_snapshot",
            sequence="Ctrl+Shift+S",
            callback=status_snapshot,
            context="app",
        )

        def paint_badge(painter: QPainter, rect: QRect, frame: int) -> None:
            if not ctx.get_setting("show_badge", True):
                return
            prefix = str(ctx.get_setting("prefix", "FC"))
            text = f"{prefix} {frame}"
            painter.save()
            font = QFont("Helvetica", 11)
            font.setBold(True)
            painter.setFont(font)
            fm = painter.fontMetrics()
            # Sit below the built-in FR/TC HUD strip.
            box = QRect(8, 36, fm.horizontalAdvance(text) + 16, fm.height() + 10)
            painter.fillRect(box, QColor(0, 0, 0, 160))
            painter.setPen(QColor(120, 220, 255))
            painter.drawText(
                box.adjusted(8, 5, -8, -5),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                text,
            )
            painter.restore()

        ctx.register_hud_painter("badge", paint=paint_badge, z=10)

        ctx.register_decoder(
            "fcpanel",
            extensions=[".fcpanel"],
            factory=lambda path: FcPanelDecoder(path),
            priority=50,
        )

        action = QAction("Add Example .fcpanel Still", ctx.parent_widget())
        action.triggered.connect(lambda: self._add_fcpanel_still(ctx))
        ctx.add_menu_actions([action])

    def _add_fcpanel_still(self, ctx: PackageContext) -> None:
        try:
            temp_dir = Path(tempfile.mkdtemp(prefix="fc_example_panel_"))
            path = temp_dir / "example.fcpanel"
            write_fcpanel_stub(path)
            loaded = ctx.add_media([str(path)], mode="sequence")
            if loaded > 0:
                ctx.status(f"Added .fcpanel still: {path.name}")
                panel = self._panel_holder.get("widget") if hasattr(self, "_panel_holder") else None
                if panel is not None:
                    panel.note_decoder_load(str(path))
            else:
                ctx.status("Could not add .fcpanel still")
        except Exception as exc:
            ctx.logger.exception("Failed to add .fcpanel still")
            ctx.status(f".fcpanel still failed: {exc}")
