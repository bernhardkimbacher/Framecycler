from __future__ import annotations

import struct
import math
import time
from dataclasses import dataclass

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


class _RateLimitedLogger:
    def __init__(self) -> None:
        self._last_messages: dict[str, float] = {}

    def warn(self, key: str, message: str, interval_sec: float = 5.0) -> None:
        now = time.monotonic()
        last = self._last_messages.get(key, 0.0)
        if now - last >= interval_sec:
            print(message)
            self._last_messages[key] = now


_RATE_LIMITER = _RateLimitedLogger()


@dataclass
class _TextureUploadState:
    texture: QRhiTexture | None = None
    last_upload_token: int = 0
    last_w: int = 0
    last_h: int = 0
    last_channels: int = 0


@dataclass
class FrameRenderSlot:
    data: np.ndarray | None = None
    width: int = 0
    height: int = 0
    channels: int = 4
    upload_token: int = 0
    upload_buffer: QByteArray | None = None
    frame_index: int = -1


@dataclass
class TileDrawParams:
    source_index: int
    scale_x: float
    scale_y: float
    offset_x: float
    offset_y: float


@dataclass
class _OcioLutSlot:
    texture: QRhiTexture | None = None
    is_3d: bool = True
    size: int = 0
    width: int = 0
    height: int = 0
    rgba_data: np.ndarray | None = None
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
    _upload_texture_qbytearray(
        batch,
        texture,
        QByteArray(data),
        row_stride_bytes=row_stride_bytes,
    )


def _upload_texture_qbytearray(
    batch,
    texture: QRhiTexture,
    data: QByteArray,
    *,
    row_stride_bytes: int = 0,
) -> None:
    subres = QRhiTextureSubresourceUploadDescription(data)
    if row_stride_bytes > 0:
        subres.setDataStride(row_stride_bytes)
    batch.uploadTexture(
        texture,
        QRhiTextureUploadDescription(QRhiTextureUploadEntry(0, 0, subres)),
    )


def _upload_texture_3d(batch, texture: QRhiTexture, rgba: np.ndarray) -> None:
    """Upload a 3D LUT volume slice-by-slice (required by QRhi on Metal/Vulkan)."""
    volume = np.ascontiguousarray(rgba, dtype=np.float32)
    if volume.ndim != 4 or volume.shape[-1] != 4:
        raise ValueError("3D LUT upload expects an (D, H, W, 4) RGBA32F array")

    depth, _height, width, _channels = volume.shape
    row_stride = width * 4 * volume.itemsize
    for layer in range(depth):
        slice_bytes = volume[layer].tobytes()
        subres = QRhiTextureSubresourceUploadDescription(QByteArray(slice_bytes))
        subres.setDataStride(row_stride)
        batch.uploadTexture(
            texture,
            QRhiTextureUploadDescription(QRhiTextureUploadEntry(layer, 0, subres)),
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
        self._texture_states: list[_TextureUploadState] = []
        self._ocio_luts: list[_OcioLutSlot] = []
        self._ocio_lut_slot_dims: list[str] = []

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

        while len(self._ocio_luts) <= index:
            self._ocio_luts.append(_OcioLutSlot())

        slot = self._ocio_luts[index]
        _destroy_resource(slot.texture)
        slot.texture = self._rhi.newTexture(
            QRhiTexture.RGBA32F,
            size,
            size,
            size,
            flags=QRhiTexture.ThreeDimensional,
        )
        slot.texture.create()
        slot.is_3d = True
        slot.size = size

        flat = np.asarray(data, dtype=np.float32).reshape(-1)
        side = int(size)
        rgb = flat.reshape(side, side, side, 3)
        rgba = np.empty((side, side, side, 4), dtype=np.float32)
        rgba[..., :3] = rgb
        rgba[..., 3] = 1.0
        slot.rgba_data = rgba
        slot.dirty = True
        self._invalidate_pipeline()

    def upload_ocio_lut_2d(
        self,
        index: int,
        width: int,
        height: int,
        channels: int,
        data: np.ndarray | list[float],
    ) -> None:
        if self._rhi is None or width <= 0 or height <= 0 or channels <= 0 or data is None:
            return

        while len(self._ocio_luts) <= index:
            self._ocio_luts.append(_OcioLutSlot())

        slot = self._ocio_luts[index]
        _destroy_resource(slot.texture)
        slot.texture = _new_texture_2d(self._rhi, QRhiTexture.RGBA32F, width, height)
        slot.texture.create()
        slot.is_3d = False
        slot.width = width
        slot.height = height
        slot.size = 0

        flat = np.asarray(data, dtype=np.float32).reshape(-1)
        pixels = width * height
        rgba = np.empty((height, width, 4), dtype=np.float32)
        if channels == 1:
            r = flat[:pixels].reshape(height, width)
            rgba[..., 0] = r
            rgba[..., 1] = r
            rgba[..., 2] = r
            rgba[..., 3] = 1.0
        elif channels == 3:
            rgb = flat[: pixels * 3].reshape(height, width, 3)
            rgba[..., :3] = rgb
            rgba[..., 3] = 1.0
        else:
            src = flat[: pixels * min(channels, 4)].reshape(height, width, min(channels, 4))
            rgba[..., : src.shape[-1]] = src
            if src.shape[-1] < 4:
                rgba[..., 3] = 1.0
        slot.rgba_data = rgba
        slot.dirty = True
        self._invalidate_pipeline()

    def set_ocio_lut_slot_dims(self, dims: list[str]) -> None:
        self._ocio_lut_slot_dims = list(dims)

    def set_grading_uniform(self, name: str, value: float) -> None:
        self._grading_floats[name] = float(value)

    def set_grading_uniform_vec3(self, name: str, x: float, y: float, z: float) -> None:
        self._grading_vec3s[name] = (float(x), float(y), float(z))

    def clear_grading_uniforms(self) -> None:
        self._grading_floats.clear()
        self._grading_vec3s.clear()

    def clear_ocio_luts(self) -> None:
        if self._rhi is None:
            self._ocio_luts.clear()
            self._ocio_lut_slot_dims.clear()
            return
        self._clear_ocio_luts_internal()

    def reset_frame_textures(self) -> None:
        self._release_texture_state(self._tex_a_state)
        self._release_texture_state(self._tex_b_state)
        for state in self._texture_states:
            self._release_texture_state(state)
        self._texture_states.clear()
        self._invalidate_pipeline()

    def ensure_texture_pool(self, count: int) -> None:
        while len(self._texture_states) < count:
            self._texture_states.append(_TextureUploadState())
        while len(self._texture_states) > count:
            state = self._texture_states.pop()
            self._release_texture_state(state)

    def render(
        self,
        cb: QRhiCommandBuffer,
        render_target: QRhiRenderTarget,
        sources: list[FrameRenderSlot],
        compare_mode: int,
        sequence_index: int,
        wipe_pos: float,
        channel_mask: int,
        scale_x: float,
        scale_y: float,
        pan_x: float,
        pan_y: float,
        tile_draws: list[TileDrawParams] | None = None,
    ) -> None:
        if not self._initialized or self._rhi is None or cb is None or render_target is None:
            return

        if compare_mode == 3:
            self._render_tile_mode(
                cb,
                render_target,
                sources,
                channel_mask,
                tile_draws or [],
            )
            return

        primary = self._select_primary_slot(sources, compare_mode, sequence_index)
        if primary is None or primary.data is None:
            return

        secondary = sources[1] if len(sources) > 1 and compare_mode in (1, 2, 4) else None

        self._ensure_static_resources_created()
        if not self._vertex_shader or not self._fragment_shader:
            _RATE_LIMITER.warn(
                "missing-shaders",
                "RhiViewportRenderer: render skipped because shaders are not ready "
                "(shader bake may have failed)",
            )
            return

        batch = self._rhi.nextResourceUpdateBatch()
        self._upload_static_resources(batch)
        self._upload_texture(
            self._tex_a_state,
            primary.width,
            primary.height,
            primary.channels,
            primary.data,
            primary.upload_token,
            batch,
            pre_baked_buffer=primary.upload_buffer,
            frame_index=primary.frame_index,
        )
        if secondary is not None and secondary.data is not None:
            self._upload_texture(
                self._tex_b_state,
                secondary.width,
                secondary.height,
                secondary.channels,
                secondary.data,
                secondary.upload_token,
                batch,
                pre_baked_buffer=secondary.upload_buffer,
                frame_index=secondary.frame_index,
            )

        for lut in self._ocio_luts:
            if lut.dirty and lut.texture is not None and lut.rgba_data is not None:
                if lut.is_3d:
                    _upload_texture_3d(batch, lut.texture, lut.rgba_data)
                else:
                    row_stride = lut.width * 4 * 4
                    _upload_texture_bytes(
                        batch,
                        lut.texture,
                        lut.rgba_data.tobytes(),
                        row_stride_bytes=row_stride,
                    )
                lut.dirty = False

        self._ensure_pipeline(render_target)
        if not self._pipeline or self._tex_a_state.texture is None:
            _RATE_LIMITER.warn(
                "missing-pipeline",
                "RhiViewportRenderer: render skipped because graphics pipeline is not ready",
            )
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

    def _select_primary_slot(
        self,
        sources: list[FrameRenderSlot],
        compare_mode: int,
        sequence_index: int,
    ) -> FrameRenderSlot | None:
        if not sources:
            return None
        if compare_mode == 0:
            if 0 <= sequence_index < len(sources):
                return sources[sequence_index]
            return sources[0]
        return sources[0]

    def _render_tile_mode(
        self,
        cb: QRhiCommandBuffer,
        render_target: QRhiRenderTarget,
        sources: list[FrameRenderSlot],
        channel_mask: int,
        tile_draws: list[TileDrawParams],
    ) -> None:
        if not tile_draws:
            return

        self._ensure_static_resources_created()
        if not self._vertex_shader or not self._fragment_shader:
            return

        self.ensure_texture_pool(len(sources))
        batch = self._rhi.nextResourceUpdateBatch()
        self._upload_static_resources(batch)

        for index, slot in enumerate(sources):
            if slot.data is None or index >= len(self._texture_states):
                continue
            self._upload_texture(
                self._texture_states[index],
                slot.width,
                slot.height,
                slot.channels,
                slot.data,
                slot.upload_token,
                batch,
                pre_baked_buffer=slot.upload_buffer,
                frame_index=slot.frame_index,
            )

        for lut in self._ocio_luts:
            if lut.dirty and lut.texture is not None and lut.rgba_data is not None:
                if lut.is_3d:
                    _upload_texture_3d(batch, lut.texture, lut.rgba_data)
                else:
                    row_stride = lut.width * 4 * 4
                    _upload_texture_bytes(
                        batch,
                        lut.texture,
                        lut.rgba_data.tobytes(),
                        row_stride_bytes=row_stride,
                    )
                lut.dirty = False

        cb.beginPass(render_target, QColor(0, 0, 0), DEFAULT_DEPTH_STENCIL_CLEAR, batch)
        output_size = render_target.pixelSize()

        for tile in tile_draws:
            if tile.source_index < 0 or tile.source_index >= len(self._texture_states):
                continue
            tex_state = self._texture_states[tile.source_index]
            if tex_state.texture is None:
                continue

            self._tex_a_state.texture = tex_state.texture
            self._tex_a_state.last_upload_token = tex_state.last_upload_token
            self._tex_a_state.last_w = tex_state.last_w
            self._tex_a_state.last_h = tex_state.last_h
            self._tex_a_state.last_channels = tex_state.last_channels
            self._ensure_pipeline(render_target, force_rebuild=True)
            if not self._pipeline:
                continue

            per_frame = struct.pack(
                "<ffffifii",
                tile.scale_x,
                tile.scale_y,
                tile.offset_x,
                tile.offset_y,
                0,
                0.5,
                channel_mask,
                0,
            )
            batch = self._rhi.nextResourceUpdateBatch()
            batch.updateDynamicBuffer(self._per_frame_ubo, 0, PER_FRAME_UBO_SIZE, per_frame)
            if self._ocio_ubo is not None and self._ocio_ubo_size > 0:
                batch.updateDynamicBuffer(
                    self._ocio_ubo,
                    0,
                    self._ocio_ubo_size,
                    self._fill_ocio_ubo(),
                )

            cb.setGraphicsPipeline(self._pipeline)
            cb.setViewport(QRhiViewport(0, 0, output_size.width(), output_size.height()))
            cb.setShaderResources()
            cb.setVertexInput(0, [(self._vertex_buffer, 0)])
            cb.resourceUpdate(batch)
            cb.draw(4)

        cb.endPass()

    def _invalidate_pipeline(self) -> None:
        _destroy_resource(self._pipeline)
        _destroy_resource(self._srb)
        self._pipeline = None
        self._srb = None
        self._cached_render_pass_desc = None

    def _clear_ocio_luts_internal(self) -> None:
        for lut in self._ocio_luts:
            _destroy_resource(lut.texture)
            lut.texture = None
            lut.size = 0
            lut.width = 0
            lut.height = 0
            lut.rgba_data = None
            lut.dirty = False
        self._ocio_luts.clear()
        self._ocio_lut_slot_dims.clear()
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
        for state in self._texture_states:
            self._release_texture_state(state)
        self._texture_states.clear()
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

        def write_float(name: str, value: float) -> None:
            member = self._ocio_uniform_members.get(name)
            if member is None or member.is_vec3:
                return
            struct.pack_into("<f", buf, member.offset, float(value))

        def write_vec3(name: str, x: float, y: float, z: float) -> None:
            member = self._ocio_uniform_members.get(name)
            if member is None or not member.is_vec3:
                return
            struct.pack_into("<fff", buf, member.offset, x, y, z)

        # Identity defaults — prevent sat=0 desaturation if a uniform is missing.
        write_float("ocio_exposure_contrast_exposureVal", 0.0)
        write_float("ocio_exposure_contrast_gammaVal", 1.0)
        write_vec3("ocio_grading_primary_brightness", 0.0, 0.0, 0.0)
        write_vec3("ocio_grading_primary_contrast", 1.0, 1.0, 1.0)
        write_vec3("ocio_grading_primary_gamma", 1.0, 1.0, 1.0)
        write_float("ocio_grading_primary_saturation", 1.0)
        write_float("ocio_grading_primary_localBypass", 0.0)
        write_vec3("fc_cdl_slope", 1.0, 1.0, 1.0)
        write_vec3("fc_cdl_offset", 0.0, 0.0, 0.0)
        write_vec3("fc_cdl_power", 1.0, 1.0, 1.0)
        write_float("fc_cdl_saturation", 1.0)
        write_float("fc_cdl_enable", 0.0)

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

    def _ensure_pipeline(self, render_target: QRhiRenderTarget, *, force_rebuild: bool = False) -> None:
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
        if force_rebuild:
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
        for lut in self._ocio_luts:
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
        *,
        pre_baked_buffer: QByteArray | None = None,
        frame_index: int = -1,
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

        row_stride = width * channels * 2
        if pre_baked_buffer is not None and not pre_baked_buffer.isEmpty():
            _upload_texture_qbytearray(
                batch,
                state.texture,
                pre_baked_buffer,
                row_stride_bytes=row_stride,
            )
        else:
            _RATE_LIMITER.warn(
                "sync-upload-fallback",
                f"RhiViewportRenderer: no pre-staged upload buffer for frame {frame_index}, "
                "falling back to synchronous main-thread packing (perf degraded)",
            )
            pixel_bytes = np.asarray(data, dtype=np.float16).tobytes()
            _upload_texture_bytes(batch, state.texture, pixel_bytes, row_stride_bytes=row_stride)

        state.last_upload_token = upload_token
        state.last_w = width
        state.last_h = height
        state.last_channels = channels
