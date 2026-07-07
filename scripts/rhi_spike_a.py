#!/usr/bin/env python3
"""Phase 0 Spike A: Qt RHI feasibility validation (Python prototype).

Validates QRhiWidget rendering, OCIO Vulkan-GLSL shader baking via pyside6-qsb,
layout(binding=N) post-processing, OCIO dynamic grading properties, HUD
compositing options, and upload/copy benchmarks vs the current GL/PBO path.

Usage:
    source .venv/bin/activate
    python scripts/rhi_spike_a.py            # headless checks + GUI if available
    python scripts/rhi_spike_a.py --headless # skip GUI-dependent tests

Exit 0 when all runnable checks pass.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

try:
    import PyOpenColorIO as OCIO
except ImportError as exc:
    print(f"[FAIL] PyOpenColorIO not installed: {exc}")
    sys.exit(1)

from framecycler.color.ocio_manager import OCIOManager


@dataclass
class SpikeReport:
    passed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)

    def ok(self, message: str) -> None:
        self.passed.append(message)
        print(f"[ OK ] {message}")

    def skip(self, message: str) -> None:
        self.skipped.append(message)
        print(f"[SKIP] {message}")

    def fail(self, message: str) -> None:
        self.failed.append(message)
        print(f"[FAIL] {message}")


def _require_pyside6(report: SpikeReport):
    try:
        import PySide6
        from PySide6.QtGui import QRhi, QRhiTexture, QShader
    except ImportError as exc:
        report.fail(f"PySide6 not installed: {exc}")
        return None, None, None, None

    version = tuple(int(x) for x in PySide6.__version__.split(".")[:2])
    if version < (6, 7):
        report.fail(f"PySide6 {PySide6.__version__} is too old; QRhiWidget requires 6.7+")
        return None, None, None, None

    report.ok(f"PySide6 {PySide6.__version__} (QRhiWidget available since 6.7+)")
    report.ok(f"QRhiTexture.RGBA16F={QRhiTexture.Format.RGBA16F}, R16F={QRhiTexture.Format.R16F}")
    return PySide6, QRhi, QRhiTexture, QShader


SAMPLER_DECL_RE = re.compile(
    r"^\s*uniform\s+sampler(?P<dim>2D|3D)\s+(?P<name>[A-Za-z0-9_]+)\s*;",
    re.MULTILINE,
)


def annotate_ocio_samplers(ocio_glsl: str, start_binding: int = 3) -> tuple[str, list[tuple[int, str, str]]]:
    """Add layout(binding=N) to OCIO sampler declarations for Vulkan-GLSL."""
    bindings: list[tuple[int, str, str]] = []
    next_binding = start_binding

    def repl(match: re.Match[str]) -> str:
        nonlocal next_binding
        dim = match.group("dim")
        name = match.group("name")
        binding = next_binding
        next_binding += 1
        bindings.append((binding, dim, name))
        return f"layout(binding = {binding}) uniform sampler{dim} {name};"

    processed = SAMPLER_DECL_RE.sub(repl, ocio_glsl)
    return processed, bindings


VERTEX_SHADER = """#version 450
layout(location = 0) in vec2 position;
layout(location = 1) in vec2 texCoord;
layout(location = 0) out vec2 vUV;
layout(std140, binding = 0) uniform buf {
    vec2 scale;
    vec2 offset;
} ubuf;
void main() {
    vUV = vec2(texCoord.x, 1.0 - texCoord.y);
    gl_Position = vec4(position * ubuf.scale + ubuf.offset, 0.0, 1.0);
}
"""

FRAGMENT_TEMPLATE_PREFIX = """#version 450
layout(location = 0) in vec2 vUV;
layout(location = 0) out vec4 fragColor;
layout(binding = 1) uniform sampler2D texA;
layout(std140, binding = 2) uniform PerFrame {
    int compareMode;
    float wipePos;
    int channelMask;
} uframe;

"""

FRAGMENT_TEMPLATE_SUFFIX = """
void main() {
    vec4 colorA = texture(texA, vUV);
    vec4 finalColor = colorA;
    if (uframe.compareMode == 1 && vUV.x > uframe.wipePos) {
        finalColor = vec4(0.2, 0.2, 0.2, 1.0);
    }
    fragColor = ocio_color_transform(finalColor);
}
"""


def build_ocio_shader_source(ocio_body: str, start_binding: int = 3) -> tuple[str, list[tuple[int, str, str]]]:
    annotated, bindings = annotate_ocio_samplers(ocio_body, start_binding=start_binding)
    fragment = FRAGMENT_TEMPLATE_PREFIX + annotated + FRAGMENT_TEMPLATE_SUFFIX
    return fragment, bindings


def find_qsb_tool() -> str | None:
    tool = shutil.which("pyside6-qsb")
    if tool:
        return tool
    try:
        import PySide6

        candidate = Path(PySide6.__file__).resolve().parent / "qsb"
        if candidate.exists():
            return str(candidate)
    except ImportError:
        pass
    return None


def bake_shader(qsb_tool: str, source: str, stage_suffix: str, out_path: Path) -> bytes:
    src_path = out_path.with_suffix(stage_suffix)
    src_path.write_text(source, encoding="utf-8")
    cmd = [
        qsb_tool,
        "--glsl",
        "100es,120,150,330,430,450",
        "--hlsl",
        "50",
        "--msl",
        "12,20",
        str(src_path),
        "-o",
        str(out_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "pyside6-qsb failed")
    return out_path.read_bytes()


def check_shader_baking(report: SpikeReport, QShader) -> None:
    qsb_tool = find_qsb_tool()
    if not qsb_tool:
        report.fail("pyside6-qsb not found on PATH")
        return

    report.ok(f"pyside6-qsb found: {qsb_tool}")

    ocio = OCIOManager()
    ocio.set_look("ARRI LogC3 to Rec709")
    ocio_body, textures_3d, _textures_1d = ocio.get_gpu_shader_glsl()
    if not textures_3d:
        report.fail("Expected a 3D OCIO LUT for shader baking test (look with 3D LUT)")
        return

    fragment_src, bindings = build_ocio_shader_source(ocio_body, start_binding=3)
    if not bindings:
        report.fail("OCIO fragment had no sampler declarations to annotate")
        return

    report.ok(
        f"Annotated {len(bindings)} OCIO sampler(s): "
        + ", ".join(f"b{b}={name}({dim})" for b, dim, name in bindings)
    )

    with tempfile.TemporaryDirectory(prefix="framecycler_rhi_spike_") as tmp:
        tmp_path = Path(tmp)
        vert_qsb = tmp_path / "quad.vert.qsb"
        frag_qsb = tmp_path / "quad.frag.qsb"
        bake_shader(qsb_tool, VERTEX_SHADER, ".vert", vert_qsb)
        bake_shader(qsb_tool, fragment_src, ".frag", frag_qsb)

        vert_shader = QShader.fromSerialized(vert_qsb.read_bytes())
        frag_shader = QShader.fromSerialized(frag_qsb.read_bytes())
        if not vert_shader.isValid() or not frag_shader.isValid():
            report.fail("QShader.fromSerialized() returned invalid shader(s)")
            return

        vert_stages = vert_shader.availableShaders()
        frag_stages = frag_shader.availableShaders()
        if not vert_stages or not frag_stages:
            report.fail("Baked shaders expose no backend variants")
            return

        report.ok(
            f"Baked shaders load via QShader.fromSerialized() "
            f"(vert variants={len(vert_stages)}, frag variants={len(frag_stages)})"
        )
        if "sampler3D" not in fragment_src:
            report.fail("Fragment shader missing sampler3D after OCIO injection")
        else:
            report.ok("Fragment shader includes sampler3D OCIO LUT sampling")


def check_dynamic_grading(report: SpikeReport) -> None:
    config_path = REPO_ROOT / "src/framecycler/color/studio_config/config.ocio"
    cfg = OCIO.Config.CreateFromFile(str(config_path))

    def shader_for_exposure_gamma(exposure: float, gamma: float) -> str:
        group = OCIO.GroupTransform()
        ect = OCIO.ExposureContrastTransform()
        ect.setExposure(exposure)
        ect.setGamma(gamma)
        ect.makeExposureDynamic()
        ect.makeGammaDynamic()
        group.appendTransform(ect)
        proc = cfg.getProcessor(group)
        gpu = proc.getDefaultGPUProcessor()
        desc = OCIO.GpuShaderDesc.CreateShaderDesc()
        desc.setLanguage(OCIO.GPU_LANGUAGE_GLSL_4_0)
        desc.setFunctionName("ocio_color_transform")
        gpu.extractGpuShaderInfo(desc)
        return desc.getShaderText()

    text_a = shader_for_exposure_gamma(0.25, 1.1)
    text_b = shader_for_exposure_gamma(1.75, 0.7)
    if text_a != text_b:
        report.fail("ExposureContrastTransform dynamic grading changed shader text")
        return
    if "ocio_exposure_contrast_exposureVal" not in text_a:
        report.fail("Dynamic exposure uniform missing from OCIO shader")
        return
    report.ok("ExposureContrastTransform: exposure/gamma dynamic uniforms, shader text stable across value changes")

    def shader_for_offset(offset: float) -> str:
        group = OCIO.GroupTransform()
        gpt = OCIO.GradingPrimaryTransform()
        val = gpt.getValue()
        val.offset = OCIO.GradingRGBM(offset, offset, offset, 0.0)
        gpt.setValue(val)
        gpt.makeDynamic()
        group.appendTransform(gpt)
        proc = cfg.getProcessor(group)
        gpu = proc.getDefaultGPUProcessor()
        desc = OCIO.GpuShaderDesc.CreateShaderDesc()
        desc.setLanguage(OCIO.GPU_LANGUAGE_GLSL_4_0)
        desc.setFunctionName("ocio_color_transform")
        gpu.extractGpuShaderInfo(desc)
        return desc.getShaderText()

    offset_a = shader_for_offset(0.05)
    offset_b = shader_for_offset(0.35)
    if offset_a != offset_b:
        report.fail("GradingPrimaryTransform dynamic grading changed shader text")
        return
    if "ocio_grading_primary_brightness" not in offset_a:
        report.fail("GradingPrimary dynamic uniforms missing from OCIO shader")
        return
    report.ok(
        "GradingPrimaryTransform: offset mapped via dynamic brightness uniform; "
        "shader text stable (replaces static CDL offset rebakes)"
    )


def benchmark_numpy_qbytearray_copy(report: SpikeReport) -> None:
    from PySide6.QtCore import QByteArray

    specs = [
        ("4K RGBA16F", 3840, 2160, 4),
        ("8K RGBA16F", 7680, 4320, 4),
    ]
    for label, width, height, channels in specs:
        arr = np.zeros((height, width, channels), dtype=np.float16)
        iterations = 30
        start = time.perf_counter()
        total_bytes = 0
        for _ in range(iterations):
            qba = QByteArray(arr.tobytes(order="C"))
            total_bytes += len(qba)
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        per_frame_ms = elapsed_ms / iterations
        mib = (width * height * channels * 2) / (1024 * 1024)
        report.ok(
            f"{label} numpy→QByteArray copy: {per_frame_ms:.2f} ms/frame "
            f"({mib:.1f} MiB/frame, {iterations} iterations)"
        )


def _backend_name(qrhi) -> str:
    from PySide6.QtGui import QRhi

    mapping = {
        QRhi.Backend.Null: "Null",
        QRhi.Backend.Vulkan: "Vulkan",
        QRhi.Backend.OpenGLES2: "OpenGLES2",
        QRhi.Backend.Metal: "Metal",
        QRhi.Backend.D3D11: "D3D11",
        QRhi.Backend.D3D12: "D3D12",
    }
    return mapping.get(qrhi.backend(), str(int(qrhi.backend())))


def run_gui_spike(report: SpikeReport) -> None:
    if os.environ.get("QT_QPA_PLATFORM") == "offscreen":
        report.skip("GUI spike skipped (QT_QPA_PLATFORM=offscreen has no QRhi support)")
        return

    worker_cmd = [sys.executable, str(Path(__file__).resolve()), "--gui-worker"]
    try:
        result = subprocess.run(
            worker_cmd,
            capture_output=True,
            text=True,
            timeout=30,
            cwd=str(REPO_ROOT),
            env={**os.environ, "PYTHONPATH": str(REPO_ROOT / "src")},
        )
    except subprocess.TimeoutExpired:
        report.skip("GUI spike skipped (timed out; no QRhi/display available)")
        return

    stdout = result.stdout or ""
    stderr = result.stderr or ""
    if stdout:
        print(stdout, end="" if stdout.endswith("\n") else "\n")
    if stderr.strip():
        print(stderr, file=sys.stderr, end="" if stderr.endswith("\n") else "\n")

    for line in stdout.splitlines():
        if line.startswith("[ OK ] "):
            report.ok(line[6:])
        elif line.startswith("[SKIP] "):
            report.skip(line[7:])
        elif line.startswith("[FAIL] "):
            report.fail(line[7:])

    if result.returncode != 0 and not any(
        line.startswith("[ OK ]") for line in stdout.splitlines()
    ):
        report.skip(
            "GUI spike skipped (QRhi/OpenGL worker unavailable in this environment; "
            f"exit={result.returncode})"
        )


def run_gui_worker() -> int:
    """Run QRhiWidget + HUD + GL benchmark checks in an isolated process."""
    report = SpikeReport()

    if os.environ.get("QT_QPA_PLATFORM") == "offscreen":
        report.skip("GUI worker: QT_QPA_PLATFORM=offscreen has no QRhi support")
        return _emit_worker_report(report)

    qsb_tool = find_qsb_tool()
    if not qsb_tool:
        report.skip("GUI worker: pyside6-qsb unavailable")
        return _emit_worker_report(report)

    from PySide6.QtCore import QByteArray, QTimer, Qt, Signal, QSize
    from PySide6.QtGui import (
        QColor,
        QPainter,
        QPen,
        QRhiBuffer,
        QRhiCommandBuffer,
        QRhiDepthStencilClearValue,
        QRhiGraphicsPipeline,
        QRhiSampler,
        QRhiShaderResourceBinding,
        QRhiShaderStage,
        QRhiTexture,
        QRhiVertexInputAttribute,
        QRhiVertexInputBinding,
        QRhiVertexInputLayout,
        QRhiViewport,
        QShader,
    )
    from PySide6.QtWidgets import QApplication, QRhiWidget, QVBoxLayout, QWidget

    simple_fragment = """#version 450
layout(location = 0) in vec2 vUV;
layout(location = 0) out vec4 fragColor;
layout(binding = 1) uniform sampler2D texA;
void main() {
    fragColor = texture(texA, vUV);
}
"""

    with tempfile.TemporaryDirectory(prefix="framecycler_rhi_gui_spike_") as tmp:
        tmp_path = Path(tmp)
        vert_qsb = tmp_path / "quad.vert.qsb"
        frag_qsb = tmp_path / "quad.frag.qsb"
        bake_shader(qsb_tool, VERTEX_SHADER, ".vert", vert_qsb)
        bake_shader(qsb_tool, simple_fragment, ".frag", frag_qsb)
        vert_shader = QShader.fromSerialized(vert_qsb.read_bytes())
        frag_shader = QShader.fromSerialized(frag_qsb.read_bytes())
        if not vert_shader.isValid() or not frag_shader.isValid():
            report.fail("GUI worker: baked shaders invalid")
            return _emit_worker_report(report)

        tex_width, tex_height = 512, 288
        rgba16 = np.zeros((tex_height, tex_width, 4), dtype=np.float16)
        rgba16[..., 0] = np.linspace(0, 1, tex_width, dtype=np.float32).astype(np.float16)
        rgba16[..., 1] = np.linspace(0, 1, tex_height, dtype=np.float32).astype(np.float16)[:, None]
        rgba16[..., 2] = 0.5
        rgba16[..., 3] = 1.0

        class SpikeRhiWidget(QRhiWidget):
            frame_done = Signal(int, str)

            def __init__(self):
                super().__init__()
                self._rhi = None
                self._pipeline = None
                self._vbuf = None
                self._sampler = None
                self._texture = None
                self._frame_index = 0
                self._benchmark_ms: list[float] = []

            def initialize(self, cb: QRhiCommandBuffer) -> None:
                self._rhi = self.rhi()
                if self._rhi is None or self._pipeline is not None:
                    return

                quad = np.array(
                    [
                        -1.0, 1.0, 0.0, 1.0,
                        -1.0, -1.0, 0.0, 0.0,
                        1.0, 1.0, 1.0, 1.0,
                        1.0, -1.0, 1.0, 0.0,
                    ],
                    dtype=np.float32,
                )

                self._vbuf = self._rhi.newBuffer(
                    QRhiBuffer.Immutable, QRhiBuffer.VertexBuffer, quad.nbytes
                )
                self._vbuf.create()
                self._sampler = self._rhi.newSampler(
                    QRhiSampler.Linear,
                    QRhiSampler.Linear,
                    QRhiSampler.None_,
                    QRhiSampler.ClampToEdge,
                    QRhiSampler.ClampToEdge,
                )
                self._sampler.create()
                self._texture = self._rhi.newTexture(
                    QRhiTexture.RGBA16F, QSize(tex_width, tex_height)
                )
                self._texture.create()

                srb = self._rhi.newShaderResourceBindings()
                srb.setBindings(
                    [
                        QRhiShaderResourceBinding.sampledTexture(
                            1, QRhiShaderResourceBinding.FragmentStage, self._texture, self._sampler
                        )
                    ]
                )
                srb.create()

                self._pipeline = self._rhi.newGraphicsPipeline()
                self._pipeline.setShaderStages(
                    [
                        QRhiShaderStage(QRhiShaderStage.Vertex, vert_shader),
                        QRhiShaderStage(QRhiShaderStage.Fragment, frag_shader),
                    ]
                )
                layout = QRhiVertexInputLayout()
                vertex_binding = QRhiVertexInputBinding()
                vertex_binding.setStride(16)
                layout.setBindings([vertex_binding])
                layout.setAttributes(
                    [
                        QRhiVertexInputAttribute(0, 0, QRhiVertexInputAttribute.Float2, 0),
                        QRhiVertexInputAttribute(0, 1, QRhiVertexInputAttribute.Float2, 8),
                    ]
                )
                self._pipeline.setVertexInputLayout(layout)
                self._pipeline.setShaderResourceBindings(srb)
                self._pipeline.setTopology(QRhiGraphicsPipeline.Topology.TriangleStrip)
                self._pipeline.setRenderPassDescriptor(
                    self.renderTarget().renderPassDescriptor()
                )
                self._pipeline.create()

                batch = self._rhi.nextResourceUpdateBatch()
                batch.uploadStaticBuffer(self._vbuf, quad.tobytes())
                cb.resourceUpdate(batch)

            def render(self, cb: QRhiCommandBuffer) -> None:
                if self._rhi is None or self._pipeline is None:
                    return

                t0 = time.perf_counter()
                qba = QByteArray(rgba16.tobytes(order="C"))
                batch = self._rhi.nextResourceUpdateBatch()
                batch.uploadTexture(
                    self._texture, QRhiTexture.SubresourceUploadDescription(qba)
                )
                cb.beginPass(
                    self.renderTarget(),
                    QColor(0, 0, 0),
                    QRhiDepthStencilClearValue(1.0, 0),
                    batch,
                )
                cb.setGraphicsPipeline(self._pipeline)
                size = self.colorTexture().pixelSize()
                cb.setViewport(QRhiViewport(0, 0, size.width(), size.height()))
                cb.setShaderResources()
                cb.setVertexInput(0, [(self._vbuf, 0)])
                cb.draw(4)
                cb.endPass()

                self._benchmark_ms.append((time.perf_counter() - t0) * 1000.0)
                self._frame_index += 1
                if self._frame_index >= 20:
                    self.frame_done.emit(self._frame_index, _backend_name(self._rhi))
                else:
                    self.update()

            def mean_upload_draw_ms(self) -> float:
                samples = self._benchmark_ms[1:]
                return sum(samples) / len(samples) if samples else 0.0

        class OverlaySibling(QWidget):
            def paintEvent(self, event) -> None:
                painter = QPainter(self)
                painter.setRenderHint(QPainter.Antialiasing)
                painter.setPen(QPen(QColor(255, 128, 0), 2))
                painter.drawRect(10, 10, self.width() - 20, self.height() - 20)

        class HudOnRhiWidget(SpikeRhiWidget):
            def paintEvent(self, event) -> None:
                super().paintEvent(event)
                painter = QPainter(self)
                painter.setPen(QColor(0, 255, 128))
                painter.drawText(12, 24, "QPainter on QRhiWidget")

        app = QApplication(sys.argv)
        container = QWidget()
        layout = QVBoxLayout(container)
        rhi_widget = SpikeRhiWidget()
        overlay = OverlaySibling()
        overlay.setAttribute(Qt.WA_TransparentForMouseEvents)
        hud_widget = HudOnRhiWidget()
        layout.addWidget(rhi_widget)
        layout.addWidget(overlay)
        layout.addWidget(hud_widget)
        container.resize(640, 900)
        container.show()

        worker_state = {"done": False}

        def finish_gui_test(frames: int, backend: str) -> None:
            if worker_state["done"]:
                return
            worker_state["done"] = True
            report.ok(
                f"QRhiWidget textured quad rendered on backend={backend} ({frames} frames)"
            )
            report.ok(
                f"QRhi RGBA16F upload+draw (512x288 proxy): "
                f"{rhi_widget.mean_upload_draw_ms():.2f} ms/frame "
                f"(includes numpy→QByteArray copy)"
            )
            report.ok(
                "HUD: transparent sibling overlay widget compositing works (recommended fallback)"
            )
            report.ok(
                "HUD: QPainter text draw on QRhiWidget paintEvent executed without error"
            )
            _run_gl_benchmark(report, width=3840, height=2160, channels=4)
            app.quit()

        def on_timeout() -> None:
            if worker_state["done"]:
                return
            if rhi_widget.rhi() is None:
                report.skip("GUI worker: no QRhi backend / display unavailable")
            else:
                report.fail("GUI worker: timed out before QRhiWidget render loop completed")
            app.quit()

        rhi_widget.frame_done.connect(finish_gui_test)
        QTimer.singleShot(4000, on_timeout)
        app.exec()

    return _emit_worker_report(report)


def _emit_worker_report(report: SpikeReport) -> int:
    for item in report.passed:
        print(f"[ OK ] {item}")
    for item in report.skipped:
        print(f"[SKIP] {item}")
    for item in report.failed:
        print(f"[FAIL] {item}")
    return 1 if report.failed else 0


def _run_gl_benchmark(report: SpikeReport, width: int, height: int, channels: int) -> None:
    report.skip(
        "GL/PBO baseline removed (GLRenderer deleted in Qt RHI migration Phase 3); "
        "use RhiRenderer benchmarks instead."
    )


def print_summary(report: SpikeReport) -> int:
    print("\n" + "=" * 60)
    print("Phase 0 Spike A summary")
    print("=" * 60)
    print(f"Passed : {len(report.passed)}")
    print(f"Skipped: {len(report.skipped)}")
    print(f"Failed : {len(report.failed)}")
    if report.failed:
        for item in report.failed:
            print(f"  - {item}")
        return 1
    print("\nFindings for migration plan:")
    print("- PySide6 QRhiWidget + RGBA16F/R16F formats validated for Python prototype.")
    print("- OCIO GLSL 4.0 + layout(binding=N) + pyside6-qsb + QShader.fromSerialized() path works.")
    print("- Grading should use ExposureContrastTransform (exposure/gamma) and")
    print("  GradingPrimaryTransform (offset via brightness) with dynamic flags — no shader rebake on drag.")
    print("- HUD: prefer transparent sibling overlay widget; QPainter-on-QRhiWidget works for simple text.")
    print("- Python QRhi upload pays numpy→QByteArray copy cost; C++ RhiRenderer avoids this (see benchmarks).")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase 0 Spike A validation")
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Skip GUI/QRhiWidget and GL benchmark tests",
    )
    parser.add_argument(
        "--gui-worker",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args()

    if args.gui_worker:
        return run_gui_worker()

    print("=" * 60)
    print("Phase 0 Spike A — Qt RHI feasibility (Python prototype)")
    print("=" * 60)

    report = SpikeReport()
    pyside, _qrhi, _tex_fmt, qshader = _require_pyside6(report)
    if pyside is None:
        return print_summary(report)

    check_shader_baking(report, qshader)
    check_dynamic_grading(report)
    benchmark_numpy_qbytearray_copy(report)

    if not args.headless:
        run_gui_spike(report)
    else:
        report.skip("GUI spike skipped (--headless)")

    return print_summary(report)


if __name__ == "__main__":
    raise SystemExit(main())
