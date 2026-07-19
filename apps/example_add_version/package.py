"""Example package: add a dummy grey version to the current shot stack."""

from __future__ import annotations

import tempfile
from pathlib import Path

import numpy as np
import OpenImageIO as oiio
from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QColor, QFont, QImage, QPainter

try:
    from framecycler.packages.api import Package, PackageContext
except ImportError:
    from src.framecycler.packages.api import Package, PackageContext


def _write_example_exr(path: Path, width: int = 1280, height: int = 720) -> None:
    image = QImage(width, height, QImage.Format.Format_RGB32)
    image.fill(QColor(80, 80, 80))

    painter = QPainter(image)
    painter.setPen(QColor(220, 220, 220))
    font = QFont("Helvetica", 48)
    font.setBold(True)
    painter.setFont(font)
    painter.drawText(image.rect(), Qt.AlignmentFlag.AlignCenter, "Framecycler Example")
    painter.end()

    # QImage is BGRA on little-endian; convert to float RGB for OIIO.
    ptr = image.bits()
    arr = np.frombuffer(ptr, dtype=np.uint8).reshape((height, width, 4)).copy()
    bgr = arr[:, :, :3].astype(np.float32) / 255.0
    rgb = bgr[:, :, ::-1].copy()

    spec = oiio.ImageSpec(width, height, 3, oiio.FLOAT)
    spec.channelnames = ("R", "G", "B")
    out = oiio.ImageOutput.create(str(path))
    if out is None:
        raise RuntimeError(f"OpenImageIO cannot create writer for {path}")
    if not out.open(str(path), spec):
        raise RuntimeError(f"Failed to open {path}: {oiio.geterror()}")
    try:
        if not out.write_image(rgb):
            raise RuntimeError(f"Failed to write {path}: {oiio.geterror()}")
    finally:
        out.close()


class ExampleAddVersionPackage(Package):
    def activate(self, ctx: PackageContext) -> None:
        action = QAction("Add Example Version to Stack", ctx.parent_widget())
        action.triggered.connect(lambda: self._add_example_version(ctx))
        ctx.add_menu_actions([action])

    def _add_example_version(self, ctx: PackageContext) -> None:
        # ---------------------------------------------------------------------------
        # Stub: replace this with loading a version path from another app
        # (ShotGrid / ftrack / RV / custom studio tool), then:
        #   ctx.add_media([resolved_path], mode="stack")
        # ---------------------------------------------------------------------------
        try:
            temp_dir = Path(tempfile.mkdtemp(prefix="fc_example_version_"))
            exr_path = temp_dir / "framecycler_example.exr"
            _write_example_exr(exr_path)
            loaded = ctx.add_media([str(exr_path)], mode="stack")
            if loaded > 0:
                ctx.status(f"Added example version: {exr_path.name}")
            else:
                ctx.status("Could not add example version (load a shot first for stack mode).")
        except Exception as exc:
            ctx.logger.exception("Failed to add example version")
            ctx.status(f"Example version failed: {exc}")
