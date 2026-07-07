from __future__ import annotations

import struct
import math
from dataclasses import dataclass, field

import numpy as np
from PySide6.QtCore import QByteArray, QSize
from PySide6.QtGui import (
    QColor,
    QRhi,
    QRhiBuffer,
    QRhiCommandBuffer,
    QRhiDepthStencilClearValue,
    QRhiGraphicsPipeline,
    QRhiRenderTarget,
    QRhiSampler,
    QRhiShaderResourceBinding,
    QRhiShaderStage,
    QRhiTexture,
    QRhiTextureSubresourceUploadDescription,
    QRhiTextureUploadDescription,
    QRhiTextureUploadEntry,
    QRhiVertexInputAttribute,
    QRhiVertexInputBinding,
    QRhiVertexInputLayout,
    QRhiViewport,
    QShader,
)

from .shader_baker import ShaderBaker
from .shader_pipeline import OcioUniformMember, parse_ocio_ubo_layout

PER_FRAME_UBO_SIZE = 32
DEFAULT_DEPTH_STENCIL_CLEAR = QRhiDepthStencilClearValue(1.0, 0)
QUAD_VERTICES = np.array(
    [
        -1.0,
        1.0,
        0.0,
        1.0,
        -1.0,
        -1.0,
        0.0,
        0.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        -1.0,
        1.0,
        0.0,
    ],
    dtype=np.float32,
)


@dataclass
class _TextureUploadState:
    texture: QRhiTexture | None = None
    last_upload_token: int = 0
    last_w: int = 0
    last_h: int = 0
    last_channels: int = 0


@dataclass
class _OcioLut3D:
    texture: QRhiTexture | None = None
    size: int = 0
    rgba_data: list[float] = field(default_factory=list)
    dirty: bool = False


def _new_texture_2d(rhi: QRhi, texture_format: QRhiTexture.Format, width: int, height: int) -> QRhiTexture:
    return rhi.newTexture(texture_format, QSize(width, height))


def _destroy_resource(resource) -> None:
    if resource is not None:
        resource.destroy()


def _texture_format_for_channels(channels: int) -> QRhiTexture.Format:
    if channels == 1:
        return QRhiTexture.R16F
    return QRhiTexture.RGBA16F


def _upload_texture_bytes(
    batch,
    texture: QRhiTexture,
    data: bytes,
    *,
    row_stride_bytes: int = 0,
) -> None:
    subres = QRhiTextureSubresourceUploadDescription(QByteArray(data))
    if row_stride_bytes > 0:
        subres.setDataStride(row_stride_bytes)
    batch.uploadTexture(
        texture,
        QRhiTextureUploadDescription(QRhiTextureUploadEntry(0, 0, subres)),
    )


class RhiViewportRenderer:
    """PySide6-only QRhi renderer for the viewport (avoids dual-Qt C++ linking)."""

    def __init__(self) -> None:
        self._rhi: QRhi | None = None
        self._initialized_rhi: QRhi | None = None
        self._initialized = False
        self._static_resources_uploaded = False

        self._vertex_buffer: QRhiBuffer | None = None
        self._per_frame_ubo: QRhiBuffer | None = None
        self._ocio_ubo: QRhiBuffer | None = None
        self._sampler: QRhiSampler | None = None
        self._lut_sampler: QRhiSampler | None = None
        self._srb: QRhiShaderResourceBindings | None = None
        self._pipeline: QRhiGraphicsPipeline | None = None

        self._tex_a_state = _TextureUploadState()
        self._tex_b_state = _TextureUploadState()
        self._ocio_luts_3d: list[_OcioLut3D] = []

        self._vertex_shader: QShader | None = None
        self._fragment_shader: QShader | None = None
        self._pipeline_key = ""
        self._cached_shader_key = ""
        self._pending_pipeline_key = ""
        self._pending_vertex_source = ""
        self._pending_fragment_source = ""

        self._grading_floats: dict[str, float] = {}
        self._grading_vec3s: dict[str, tuple[float, float, float]] = {}
        self._ocio_uniform_members: dict[str, OcioUniformMember] = {}
        self._ocio_ubo_size = 0
        self._ocio_ubo_binding = -1
        self._cached_render_pass_desc = None

        self._shader_baker = ShaderBaker()

    def initialize(self, rhi: QRhi | None) -> None:
        if rhi is None:
            return

        if self._initialized_rhi is not rhi:
            self._release_gpu_resources()
            self._initialized_rhi = rhi
            self._initialized = False
            self._static_resources_uploaded = False

        self._rhi = rhi
        if self._initialized:
            return

        if self._pending_vertex_source and self._pending_fragment_source:
            self.set_shader_sources(
                self._pending_pipeline_key,
                self._pending_vertex_source,
                self._pending_fragment_source,
            )

        self._initialized = True

    def cleanup(self) -> None:
        self._release_gpu_resources()
        self._vertex_shader = None
        self._fragment_shader = None
        self._cached_shader_key = ""
        self._pipeline_key = ""
        self._pending_pipeline_key = ""
        self._pending_vertex_source = ""
        self._pending_fragment_source = ""
        self._ocio_uniform_members.clear()
        self._ocio_ubo_size = 0
        self._ocio_ubo_binding = -1
        self._cached_render_pass_desc = None
        self._grading_floats.clear()
        self._grading_vec3s.clear()
        self._initialized = False
        self._static_resources_uploaded = False
        self._initialized_rhi = None
        self._rhi = None

    def set_shader_sources(
        self,
        pipeline_key: str,
        vertex_source: str,
        fragment_source: str,
    ) -> None:
        self._pipeline_key = pipeline_key
        if self._rhi is None:
            self._pending_pipeline_key = pipeline_key
            self._pending_vertex_source = vertex_source
            self._pending_fragment_source = fragment_source
            return

        if pipeline_key == self._cached_shader_key and self._pipeline and self._vertex_shader and self._fragment_shader:
            return

        if pipeline_key != self._cached_shader_key:
            self._clear_ocio_luts_internal()

        if not self._bake_shaders(vertex_source, fragment_source):
            self._cached_shader_key = ""
            self._invalidate_pipeline()
            return

        self._cached_shader_key = pipeline_key
        self._pending_vertex_source = ""
        self._pending_fragment_source = ""
        self._invalidate_pipeline()

    def upload_ocio_lut_3d(self, index: int, size: int, data: np.ndarray) -> None:
        if self._rhi is None or size <= 0 or data is None:
            return

        while len(self._ocio_luts_3d) <= index:
            self._ocio_luts_3d.append(_OcioLut3D())

        slot = self._ocio_luts_3d[index]
        _destroy_resource(slot.texture)
        slot.texture = self._rhi.newTexture(
            QRhiTexture.RGBA32F,
            size,
            size,
            size,
            flags=QRhiTexture.ThreeDimensional,
        )
        slot.texture.create()
        slot.size = size

        flat = np.asarray(data, dtype=np.float32).reshape(-1)
        rgba = np.empty((flat.size // 3, 4), dtype=np.float32)
        rgba[:, :3] = flat.reshape(-1, 3)
        rgba[:, 3] = 1.0
        slot.rgba_data = rgba.reshape(-1).tolist()
        slot.dirty = True
        self._invalidate_pipeline()

    def set_grading_uniform(self, name: str, value: float) -> None:
        self._grading_floats[name] = float(value)

    def set_grading_uniform_vec3(self, name: str, x: float, y: float, z: float) -> None:
        self._grading_vec3s[name] = (float(x), float(y), float(z))

    def clear_grading_uniforms(self) -> None:
        self._grading_floats.clear()
        self._grading_vec3s.clear()

    def clear_ocio_luts(self) -> None:
        if self._rhi is None:
            self._ocio_luts_3d.clear()
            return
        self._clear_ocio_luts_internal()

    def reset_frame_textures(self) -> None:
        self._release_texture_state(self._tex_a_state)
        self._release_texture_state(self._tex_b_state)
        self._invalidate_pipeline()

    def render(
        self,
        cb: QRhiCommandBuffer,
        render_target: QRhiRenderTarget,
        data_a: np.ndarray,
        width_a: int,
        height_a: int,
        channels_a: int,
        upload_token_a: int,
        data_b: np.ndarray | None,
        width_b: int,
        height_b: int,
        channels_b: int,
        upload_token_b: int,
        compare_mode: int,
        wipe_pos: float,
        channel_mask: int,
        scale_x: float,
        scale_y: float,
        pan_x: float,
        pan_y: float,
    ) -> None:
        if not self._initialized or self._rhi is None or cb is None or render_target is None or data_a is None:
            return

        self._ensure_static_resources_created()
        if not self._vertex_shader or not self._fragment_shader:
            return

        batch = self._rhi.nextResourceUpdateBatch()
        self._upload_static_resources(batch)
        self._upload_texture(
            self._tex_a_state,
            width_a,
            height_a,
            channels_a,
            data_a,
            upload_token_a,
            batch,
        )
        if data_b is not None:
            self._upload_texture(
                self._tex_b_state,
                width_b,
                height_b,
                channels_b,
                data_b,
                upload_token_b,
                batch,
            )

        for lut in self._ocio_luts_3d:
            if lut.dirty and lut.texture is not None and lut.rgba_data:
                rgba_bytes = np.asarray(lut.rgba_data, dtype=np.float32).tobytes()
                _upload_texture_bytes(batch, lut.texture, rgba_bytes)
                lut.dirty = False

        self._ensure_pipeline(render_target)
        if not self._pipeline or self._tex_a_state.texture is None:
            cb.resourceUpdate(batch)
            return

        per_frame = struct.pack(
            "<ffffifii",
            scale_x,
            scale_y,
            pan_x,
            pan_y,
            compare_mode,
            wipe_pos,
            channel_mask,
            0,
        )
        batch.updateDynamicBuffer(self._per_frame_ubo, 0, PER_FRAME_UBO_SIZE, per_frame)

        if self._ocio_ubo is not None and self._ocio_ubo_size > 0:
            batch.updateDynamicBuffer(
                self._ocio_ubo,
                0,
                self._ocio_ubo_size,
                self._fill_ocio_ubo(),
            )

        cb.beginPass(render_target, QColor(0, 0, 0), DEFAULT_DEPTH_STENCIL_CLEAR, batch)
        cb.setGraphicsPipeline(self._pipeline)
        output_size = render_target.pixelSize()
        cb.setViewport(QRhiViewport(0, 0, output_size.width(), output_size.height()))
        cb.setShaderResources()
        cb.setVertexInput(0, [(self._vertex_buffer, 0)])
        cb.draw(4)
        cb.endPass()

    def _invalidate_pipeline(self) -> None:
        _destroy_resource(self._pipeline)
        _destroy_resource(self._srb)
        self._pipeline = None
        self._srb = None
        self._cached_render_pass_desc = None

    def _clear_ocio_luts_internal(self) -> None:
        for lut in self._ocio_luts_3d:
            _destroy_resource(lut.texture)
            lut.texture = None
            lut.size = 0
            lut.rgba_data.clear()
            lut.dirty = False
        self._ocio_luts_3d.clear()
        self._invalidate_pipeline()

    def _release_texture_state(self, state: _TextureUploadState) -> None:
        _destroy_resource(state.texture)
        state.texture = None
        state.last_upload_token = 0
        state.last_w = 0
        state.last_h = 0
        state.last_channels = 0

    def _release_gpu_resources(self) -> None:
        self._release_texture_state(self._tex_a_state)
        self._release_texture_state(self._tex_b_state)
        self._clear_ocio_luts_internal()
        self._invalidate_pipeline()
        _destroy_resource(self._sampler)
        _destroy_resource(self._lut_sampler)
        _destroy_resource(self._vertex_buffer)
        _destroy_resource(self._per_frame_ubo)
        _destroy_resource(self._ocio_ubo)
        self._sampler = None
        self._lut_sampler = None
        self._vertex_buffer = None
        self._per_frame_ubo = None
        self._ocio_ubo = None
        self._static_resources_uploaded = False

    def _ensure_static_resources_created(self) -> None:
        if self._rhi is None or self._vertex_buffer is not None:
            return

        self._vertex_buffer = self._rhi.newBuffer(
            QRhiBuffer.Immutable,
            QRhiBuffer.VertexBuffer,
            QUAD_VERTICES.nbytes,
        )
        self._vertex_buffer.create()

        self._per_frame_ubo = self._rhi.newBuffer(
            QRhiBuffer.Dynamic,
            QRhiBuffer.UniformBuffer,
            PER_FRAME_UBO_SIZE,
        )
        self._per_frame_ubo.create()

        self._sampler = self._rhi.newSampler(
            QRhiSampler.Linear,
            QRhiSampler.Linear,
            QRhiSampler.None_,
            QRhiSampler.ClampToEdge,
            QRhiSampler.ClampToEdge,
        )
        self._sampler.create()

        self._lut_sampler = self._rhi.newSampler(
            QRhiSampler.Linear,
            QRhiSampler.Linear,
            QRhiSampler.None_,
            QRhiSampler.ClampToEdge,
            QRhiSampler.ClampToEdge,
        )
        self._lut_sampler.create()

        self._tex_b_state.texture = _new_texture_2d(self._rhi, QRhiTexture.RGBA16F, 1, 1)
        self._tex_b_state.texture.create()
        self._tex_b_state.last_w = 1
        self._tex_b_state.last_h = 1
        self._tex_b_state.last_channels = 4

    def _upload_static_resources(self, batch) -> None:
        if self._static_resources_uploaded or self._rhi is None or self._vertex_buffer is None or batch is None:
            return

        batch.uploadStaticBuffer(self._vertex_buffer, QUAD_VERTICES.tobytes())
        black_pixel = np.zeros((1, 1, 4), dtype=np.float16).tobytes()
        _upload_texture_bytes(batch, self._tex_b_state.texture, black_pixel)
        self._static_resources_uploaded = True

    def _bake_shaders(self, vertex_source: str, fragment_source: str) -> bool:
        if not self._shader_baker.available:
            print("RhiViewportRenderer: pyside6-qsb not found")
            return False

        try:
            vert_bytes, frag_bytes = self._shader_baker.bake_sync(vertex_source, fragment_source)
        except Exception as exc:
            print(f"RhiViewportRenderer: shader bake failed: {exc}")
            return False

        vert_shader = QShader.fromSerialized(vert_bytes)
        frag_shader = QShader.fromSerialized(frag_bytes)
        if not vert_shader.isValid() or not frag_shader.isValid():
            print("RhiViewportRenderer: baked shaders are invalid")
            return False

        self._vertex_shader = vert_shader
        self._fragment_shader = frag_shader
        self._cache_ocio_uniform_layout(fragment_source)
        return True

    def _cache_ocio_uniform_layout(self, fragment_source: str) -> None:
        self._ocio_uniform_members.clear()
        self._ocio_ubo_size = 0
        self._ocio_ubo_binding = -1
        _destroy_resource(self._ocio_ubo)
        self._ocio_ubo = None

        layout = parse_ocio_ubo_layout(fragment_source)
        if layout is None or self._rhi is None:
            return

        self._ocio_uniform_members = layout.members
        self._ocio_ubo_size = layout.size
        self._ocio_ubo_binding = layout.binding
        self._ocio_ubo = self._rhi.newBuffer(
            QRhiBuffer.Dynamic,
            QRhiBuffer.UniformBuffer,
            self._ocio_ubo_size,
        )
        self._ocio_ubo.create()

    def _fill_ocio_ubo(self) -> bytes:
        buf = bytearray(self._ocio_ubo_size)
        for name, value in self._grading_floats.items():
            member = self._ocio_uniform_members.get(name)
            if member is None or member.is_vec3:
                continue
            packed = float(value)
            if not math.isfinite(packed):
                packed = 0.0
            packed = max(-3.402823466e38, min(3.402823466e38, packed))
            struct.pack_into("<f", buf, member.offset, packed)

        for name, value in self._grading_vec3s.items():
            member = self._ocio_uniform_members.get(name)
            if member is None or not member.is_vec3:
                continue
            struct.pack_into("<fff", buf, member.offset, value[0], value[1], value[2])
        return bytes(buf)

    def _ensure_pipeline(self, render_target: QRhiRenderTarget) -> None:
        if (
            self._rhi is None
            or render_target is None
            or not self._vertex_shader
            or not self._fragment_shader
        ):
            return

        render_pass = render_target.renderPassDescriptor()
        if self._pipeline and self._cached_render_pass_desc is not render_pass:
            self._invalidate_pipeline()
        if self._pipeline or self._tex_a_state.texture is None:
            return

        bindings = [
            QRhiShaderResourceBinding.uniformBuffer(
                0,
                QRhiShaderResourceBinding.VertexStage | QRhiShaderResourceBinding.FragmentStage,
                self._per_frame_ubo,
            ),
            QRhiShaderResourceBinding.sampledTexture(
                1,
                QRhiShaderResourceBinding.FragmentStage,
                self._tex_a_state.texture,
                self._sampler,
            ),
            QRhiShaderResourceBinding.sampledTexture(
                2,
                QRhiShaderResourceBinding.FragmentStage,
                self._tex_b_state.texture,
                self._sampler,
            ),
        ]

        lut_binding = 3
        for lut in self._ocio_luts_3d:
            if lut.texture is None:
                continue
            bindings.append(
                QRhiShaderResourceBinding.sampledTexture(
                    lut_binding,
                    QRhiShaderResourceBinding.FragmentStage,
                    lut.texture,
                    self._lut_sampler,
                )
            )
            lut_binding += 1

        if self._ocio_ubo is not None and self._ocio_ubo_binding >= 0:
            bindings.append(
                QRhiShaderResourceBinding.uniformBuffer(
                    self._ocio_ubo_binding,
                    QRhiShaderResourceBinding.FragmentStage,
                    self._ocio_ubo,
                )
            )

        self._srb = self._rhi.newShaderResourceBindings()
        self._srb.setBindings(bindings)
        self._srb.create()

        self._pipeline = self._rhi.newGraphicsPipeline()
        self._pipeline.setShaderStages(
            [
                QRhiShaderStage(QRhiShaderStage.Vertex, self._vertex_shader),
                QRhiShaderStage(QRhiShaderStage.Fragment, self._fragment_shader),
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
        self._pipeline.setShaderResourceBindings(self._srb)
        self._pipeline.setTopology(QRhiGraphicsPipeline.Topology.TriangleStrip)
        self._pipeline.setRenderPassDescriptor(render_pass)
        if not self._pipeline.create():
            print("RhiViewportRenderer: pipeline creation failed")
            self._invalidate_pipeline()
            return

        self._cached_render_pass_desc = render_pass

    def _upload_texture(
        self,
        state: _TextureUploadState,
        width: int,
        height: int,
        channels: int,
        data: np.ndarray,
        upload_token: int,
        batch,
    ) -> None:
        if self._rhi is None or data is None or width <= 0 or height <= 0 or channels <= 0 or batch is None:
            return

        if (
            state.last_upload_token == upload_token
            and state.last_w == width
            and state.last_h == height
            and state.last_channels == channels
            and state.texture is not None
        ):
            return

        texture_format = _texture_format_for_channels(channels)
        size_changed = (
            state.texture is None
            or state.last_w != width
            or state.last_h != height
            or state.last_channels != channels
        )
        if size_changed:
            _destroy_resource(state.texture)
            state.texture = _new_texture_2d(self._rhi, texture_format, width, height)
            state.texture.create()
            self._invalidate_pipeline()

        pixel_bytes = np.asarray(data, dtype=np.float16).tobytes()
        row_stride = width * channels * 2
        _upload_texture_bytes(batch, state.texture, pixel_bytes, row_stride_bytes=row_stride)

        state.last_upload_token = upload_token
        state.last_w = width
        state.last_h = height
        state.last_channels = channels
