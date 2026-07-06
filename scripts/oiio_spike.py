#!/usr/bin/env python3
"""OpenImageIO install/read spike for Framecycler.

Verifies that OpenImageIO is importable, can write/read EXR and DPX into float32
RGB arrays, and exposes channel metadata for layered EXR.

Usage:
    pip install OpenImageIO
    python scripts/oiio_spike.py

Exit 0 on success, 1 on failure.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import numpy as np


def _fail(message: str) -> None:
    print(f"[FAIL] {message}")
    sys.exit(1)


def _ok(message: str) -> None:
    print(f"[ OK ] {message}")


def main() -> int:
    print("=" * 60)
    print("OpenImageIO spike")
    print("=" * 60)

    try:
        import OpenImageIO as oiio
    except ImportError as exc:
        _fail(f"OpenImageIO not installed: {exc}\n  Try: pip install OpenImageIO")

    _ok(f"OpenImageIO {oiio.VERSION_STRING} imported")

    with tempfile.TemporaryDirectory(prefix="framecycler_oiio_spike_") as tmp:
        tmp_path = Path(tmp)
        exr_path = tmp_path / "test.exr"
        layered_exr_path = tmp_path / "layered.exr"
        dpx_path = tmp_path / "test.dpx"
        png_path = tmp_path / "test.png"

        height, width = 64, 128
        rgb = (np.linspace(0.0, 1.0, width * height * 3, dtype=np.float32).reshape(height, width, 3))

        # --- Write fixtures ---
        spec = oiio.ImageSpec(width, height, 3, oiio.FLOAT)
        spec.channelnames = ("R", "G", "B")
        out = oiio.ImageOutput.create(str(exr_path))
        if out is None or not out.open(str(exr_path), spec) or not out.write_image(rgb):
            _fail(f"EXR write failed: {oiio.geterror()}")
        out.close()
        _ok(f"Wrote float EXR ({exr_path.stat().st_size} bytes)")

        layered_spec = oiio.ImageSpec(width, height, 3, oiio.FLOAT)
        layered_spec.channelnames = ("beauty.R", "beauty.G", "beauty.B")
        layered_out = oiio.ImageOutput.create(str(layered_exr_path))
        if (
            layered_out is None
            or not layered_out.open(str(layered_exr_path), layered_spec)
            or not layered_out.write_image(np.full((height, width, 3), 0.42, dtype=np.float32))
        ):
            _fail(f"Layered EXR write failed: {oiio.geterror()}")
        layered_out.close()
        _ok("Wrote layered EXR (beauty.R/G/B channel names)")

        dpx_spec = oiio.ImageSpec(width, height, 3, oiio.UINT16)
        dpx_out = oiio.ImageOutput.create(str(dpx_path))
        u16 = np.clip(rgb * 65535.0, 0, 65535).astype(np.uint16)
        if dpx_out is None or not dpx_out.open(str(dpx_path), dpx_spec) or not dpx_out.write_image(u16):
            _fail(f"DPX write failed: {oiio.geterror()}")
        dpx_out.close()
        _ok(f"Wrote 16-bit DPX ({dpx_path.stat().st_size} bytes)")

        png_spec = oiio.ImageSpec(width, height, 3, oiio.UINT8)
        png_out = oiio.ImageOutput.create(str(png_path))
        u8 = np.clip(rgb * 255.0, 0, 255).astype(np.uint8)
        if png_out is None or not png_out.open(str(png_path), png_spec) or not png_out.write_image(u8):
            _fail(f"PNG write failed: {oiio.geterror()}")
        png_out.close()
        _ok("Wrote PNG (same code path as TIFF/JPEG would use)")

        # --- Read back ---
        for label, path, expected_channels in (
            ("EXR", exr_path, ("R", "G", "B")),
            ("Layered EXR", layered_exr_path, ("beauty.R", "beauty.G", "beauty.B")),
            ("DPX", dpx_path, None),
            ("PNG", png_path, None),
        ):
            buf = oiio.ImageBuf(str(path))
            if buf.has_error:
                _fail(f"{label} read error: {buf.geterror()}")

            spec_in = buf.spec()
            arr = np.asarray(buf.get_pixels(oiio.FLOAT), dtype=np.float32)

            if arr.shape != (height, width, 3):
                _fail(f"{label} shape {arr.shape}, expected {(height, width, 3)}")
            if arr.dtype != np.float32:
                _fail(f"{label} dtype {arr.dtype}, expected float32")
            if not np.isfinite(arr).all():
                _fail(f"{label} contains non-finite values")

            _ok(
                f"{label}: {spec_in.width}x{spec_in.height} "
                f"channels={tuple(spec_in.channelnames)} "
                f"range=[{arr.min():.4f}, {arr.max():.4f}]"
            )

            if expected_channels is not None and tuple(spec_in.channelnames) != expected_channels:
                _fail(
                    f"{label} channel names {tuple(spec_in.channelnames)}, "
                    f"expected {expected_channels}"
                )

        inp = oiio.ImageInput.open(str(layered_exr_path))
        if inp is None:
            _fail(f"ImageInput.open failed: {oiio.geterror()}")
        layers = set(name.split(".")[0] for name in inp.spec().channelnames)
        inp.close()
        if layers != {"beauty"}:
            _fail(f"Layer parse got {layers}, expected {{'beauty'}}")
        _ok("Layer prefix extraction: beauty")

    print("=" * 60)
    print("Spike passed — OIIO is viable for EXR/DPX/still sequences on this platform.")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
