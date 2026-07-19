"""Dockable Scopes panel: two side-by-side ScopePanes."""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Optional

from PySide6.QtCore import QObject, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPaintEvent
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..fonts import ui_font
from ..probe_sampling import ocio_cpu_processor
from .analysis import (
    DEFAULT_MAX_WIDTH,
    PLAY_MAX_WIDTH,
    ScopeType,
    compute_scope_accumulators,
    compute_scopes_from_cache,
)
from .painters import accumulator_image, draw_scope_chrome

logger = logging.getLogger(__name__)

_SCOPE_LABELS = (
    (ScopeType.WAVEFORM, "Waveform"),
    (ScopeType.PARADE, "Parade"),
    (ScopeType.VECTORSCOPE, "Vectorscope"),
    (ScopeType.HISTOGRAM, "Histogram"),
    (ScopeType.CIE, "CIE Chromaticity"),
)

# Idle-kick while active; does not run analysis on the GUI thread.
_KICK_MS = 16


class _ScopeBridge(QObject):
    """Queued bridge from the worker thread back to the GUI thread."""

    result_ready = Signal(object)


class ScopeCanvas(QWidget):
    """Paints readout then chrome on top."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._scope_type = ScopeType.WAVEFORM
        self._image = None
        self.setMinimumSize(120, 100)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)

    def set_scope_type(self, scope_type: ScopeType) -> None:
        self._scope_type = scope_type
        self.update()

    def set_image(self, image) -> None:
        self._image = image
        self.update()

    def clear(self) -> None:
        self._image = None
        self.update()

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        # Dark field → translucent readout → chrome overlays on top.
        painter.fillRect(rect, QColor(12, 14, 16))
        if self._image is not None and not self._image.isNull():
            if self._scope_type in (ScopeType.VECTORSCOPE, ScopeType.CIE):
                side = min(rect.width(), rect.height())
                dest = QRectF(
                    rect.center().x() - side * 0.5,
                    rect.center().y() - side * 0.5,
                    side,
                    side,
                )
            else:
                dest = rect
            painter.drawImage(dest, self._image)
        draw_scope_chrome(painter, rect, self._scope_type, fill_background=False)


class ScopePane(QWidget):
    """One scope view: type combo + canvas."""

    type_changed = Signal()

    def __init__(self, default: ScopeType = ScopeType.WAVEFORM, parent=None):
        super().__init__(parent)
        self._scope_type = default

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(4)

        bar = QHBoxLayout()
        bar.setContentsMargins(0, 0, 0, 0)
        bar.setSpacing(4)
        self.combo = QComboBox()
        for st, label in _SCOPE_LABELS:
            self.combo.addItem(label, st.value)
        idx = self.combo.findData(default.value)
        if idx >= 0:
            self.combo.blockSignals(True)
            self.combo.setCurrentIndex(idx)
            self.combo.blockSignals(False)
        self.combo.currentIndexChanged.connect(self._on_type)
        bar.addWidget(self.combo, stretch=1)

        self.btn_settings = QToolButton()
        self.btn_settings.setText("⋯")
        self.btn_settings.setToolTip("Scope settings (gain/intensity — coming later)")
        self.btn_settings.setEnabled(False)
        bar.addWidget(self.btn_settings)
        root.addLayout(bar)

        self.canvas = ScopeCanvas()
        self.canvas.set_scope_type(default)
        root.addWidget(self.canvas, stretch=1)

    @property
    def scope_type(self) -> ScopeType:
        return self._scope_type

    def _on_type(self, _index: int) -> None:
        data = self.combo.currentData()
        try:
            self._scope_type = ScopeType(str(data))
        except Exception:
            return
        self.canvas.set_scope_type(self._scope_type)
        self.canvas.clear()
        self.type_changed.emit()

    def set_image(self, image) -> None:
        if image is None:
            self.canvas.clear()
            return
        self.canvas.set_image(image)


def _worker_compute(
    cache_provider,
    array_provider,
    type_values,
    cpu_processor,
    max_width: int,
    dilate: int,
    job_id: int,
) -> dict:
    """Background job: sample cache + OCIO + accumulate + bake QImages."""
    try:
        types = tuple(ScopeType(v) for v in type_values)
        accums = None
        key = None

        # Preferred: (native_cache, decoder_frame) — C++ downsample under lock.
        if cache_provider is not None:
            try:
                native_cache, frame_index = cache_provider()
            except Exception as exc:
                return {"ok": False, "job_id": job_id, "error": repr(exc)}
            if native_cache is not None and frame_index is not None:
                accums = compute_scopes_from_cache(
                    native_cache,
                    int(frame_index),
                    types,
                    max_width=max_width,
                    cpu_processor=cpu_processor,
                )
                if accums is not None:
                    key = (int(frame_index), int(max_width), tuple(type_values))

        # Fallback: full array provider (tests / missing native path).
        if accums is None and array_provider is not None:
            try:
                arr, frame_index = array_provider()
            except Exception as exc:
                return {"ok": False, "job_id": job_id, "error": repr(exc)}
            if arr is None:
                return {"ok": False, "job_id": job_id, "empty": True}
            accums = compute_scope_accumulators(
                arr,
                types,
                max_width=max_width,
                cpu_processor=cpu_processor,
            )
            key = (
                int(frame_index) if frame_index is not None else -1,
                int(arr.shape[0]),
                int(arr.shape[1]),
                int(max_width),
            )

        if accums is None:
            return {"ok": False, "job_id": job_id, "empty": True}

        images = []
        for st, accum in zip(types, accums):
            images.append(accumulator_image(st, accum, dilate=dilate))
        return {
            "ok": True,
            "job_id": job_id,
            "key": key,
            "types": type_values,
            "images": images,
        }
    except Exception as exc:
        return {"ok": False, "job_id": job_id, "error": repr(exc)}


class ScopesPanel(QWidget):
    """Two ScopePanes; analysis runs on a worker so playback UI stays smooth."""

    hide_requested = Signal()

    def __init__(self, main_window=None, parent=None):
        super().__init__(parent)
        self._main_window = main_window
        self._cache_provider: Optional[Callable[[], tuple]] = None
        self._array_provider: Optional[Callable[[], tuple]] = None
        self._ocio_provider: Optional[Callable[[], object]] = None
        self._playing_provider: Optional[Callable[[], bool]] = None
        self._last_key: tuple | None = None
        self._dirty = True
        self._have_frame = False
        self._active = False
        self._inflight = False
        self._pending = False
        self._job_id = 0
        self._cpu_processor = None
        self._cpu_sig: str | None = None

        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="fc-scopes")
        self._bridge = _ScopeBridge(self)
        self._bridge.result_ready.connect(self._on_worker_result)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(6)

        header = QHBoxLayout()
        title = QLabel("Scopes")
        title.setFont(ui_font(11, weight=QFont.Weight.Bold))
        header.addWidget(title)
        hint = QLabel("approx. display-referred")
        hint.setFont(ui_font(9))
        hint.setStyleSheet("color: #889099;")
        hint.setToolTip(
            "Scopes sample the CPU cache and apply a CPU OCIO approx of the viewer "
            "(CDL grading is GPU-only and may differ slightly)."
        )
        header.addWidget(hint)
        header.addStretch()
        self.btn_close = QPushButton("×")
        self.btn_close.setFixedSize(22, 22)
        self.btn_close.setFlat(True)
        self.btn_close.setToolTip("Hide panel")
        self.btn_close.clicked.connect(self.hide_requested.emit)
        header.addWidget(self.btn_close)
        layout.addLayout(header)

        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.pane_a = ScopePane(ScopeType.WAVEFORM)
        self.pane_b = ScopePane(ScopeType.VECTORSCOPE)
        self.pane_a.type_changed.connect(self._on_pane_type_changed)
        self.pane_b.type_changed.connect(self._on_pane_type_changed)
        self.splitter.addWidget(self.pane_a)
        self.splitter.addWidget(self.pane_b)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 1)
        layout.addWidget(self.splitter, stretch=1)

        self._kick_timer = QTimer(self)
        self._kick_timer.setInterval(_KICK_MS)
        self._kick_timer.timeout.connect(self._kick_tick)

        self.setMinimumWidth(320)
        self.setMinimumHeight(180)

    def set_providers(
        self,
        *,
        cache_provider: Callable[[], tuple] | None = None,
        frame_provider: Callable[[], tuple] | None = None,
        ocio_provider: Callable[[], object] | None = None,
        playing_provider: Callable[[], bool] | None = None,
    ) -> None:
        """cache_provider → (native_cache|None, decoder_frame|None).
        frame_provider → (array|None, frame_index|None) fallback.
        """
        self._cache_provider = cache_provider
        self._array_provider = frame_provider
        self._ocio_provider = ocio_provider
        self._playing_provider = playing_provider
        self._dirty = True
        QTimer.singleShot(0, self._schedule)

    def set_active(self, active: bool) -> None:
        """Called when the hosting dock is shown/hidden."""
        self._active = bool(active)
        if self._active:
            self._dirty = True
            if not self._kick_timer.isActive():
                self._kick_timer.start()
            QTimer.singleShot(0, self._schedule)
        else:
            self._kick_timer.stop()
            self._pending = False

    def notify_frame_changed(self) -> None:
        """Mark dirty. Immediate schedule only when not playing (scrub/pause)."""
        self._dirty = True
        if not self._is_live():
            return
        if not self._is_playing():
            self._schedule()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self.set_active(True)
        if self.splitter.count() == 2 and sum(self.splitter.sizes()) == 0:
            self.splitter.setSizes([1, 1])

    def hideEvent(self, event) -> None:  # noqa: N802
        self.set_active(False)
        super().hideEvent(event)

    def closeEvent(self, event) -> None:  # noqa: N802
        self._kick_timer.stop()
        self._pending = False
        super().closeEvent(event)

    def _is_live(self) -> bool:
        if self._active:
            return True
        try:
            return bool(self.isVisible())
        except Exception:
            return False

    def _is_playing(self) -> bool:
        if self._playing_provider is None:
            return False
        try:
            return bool(self._playing_provider())
        except Exception:
            return False

    def _on_pane_type_changed(self) -> None:
        self._dirty = True
        self._schedule()

    def _kick_tick(self) -> None:
        """While active: kick a new job only when the worker is idle."""
        if not self._is_live():
            return
        if self._inflight:
            # Keep coalescing to the latest playhead once the job finishes.
            if self._is_playing():
                self._pending = True
            return
        if self._is_playing() or self._dirty or not self._have_frame:
            self._schedule()

    def _schedule(self) -> None:
        if not self._is_live():
            return
        if self._cache_provider is None and self._array_provider is None:
            return
        if self._inflight:
            self._pending = True
            return
        self._start_job()

    def _snapshot_cpu_processor(self):
        ocio = None
        if self._ocio_provider is not None:
            try:
                ocio = self._ocio_provider()
            except Exception:
                logger.exception("Scopes OCIO provider failed")
                ocio = None
        sig = ""
        if ocio is not None:
            sig = (
                f"{getattr(ocio, 'input_colorspace', '')}|"
                f"{getattr(ocio, 'look', '')}|"
                f"{getattr(ocio, 'display_output', '')}|"
                f"{getattr(ocio, 'config_path', '')}"
            )
        if sig != self._cpu_sig:
            try:
                self._cpu_processor = ocio_cpu_processor(ocio)
            except Exception:
                logger.exception("Scopes OCIO processor snapshot failed")
                self._cpu_processor = None
            self._cpu_sig = sig
        return self._cpu_processor

    def _start_job(self) -> None:
        self._inflight = True
        self._pending = False
        self._dirty = False
        self._job_id += 1
        job_id = self._job_id

        type_values = (self.pane_a.scope_type.value, self.pane_b.scope_type.value)
        playing = self._is_playing()
        max_width = PLAY_MAX_WIDTH if playing else DEFAULT_MAX_WIDTH
        dilate = 1 if playing else 2
        cpu = self._snapshot_cpu_processor()

        fut = self._pool.submit(
            _worker_compute,
            self._cache_provider,
            self._array_provider,
            type_values,
            cpu,
            max_width,
            dilate,
            job_id,
        )

        def _done(f, bridge=self._bridge):
            try:
                result = f.result()
            except Exception as exc:
                result = {"ok": False, "job_id": job_id, "error": repr(exc)}
            bridge.result_ready.emit(result)

        fut.add_done_callback(_done)

    def _on_worker_result(self, result: object) -> None:
        self._inflight = False
        if not isinstance(result, dict):
            self._dirty = True
            return

        if not result.get("ok"):
            if result.get("empty"):
                self.pane_a.set_image(None)
                self.pane_b.set_image(None)
                self._have_frame = False
                self._last_key = None
            elif result.get("error"):
                logger.warning("Scopes worker failed: %s", result.get("error"))
                self._have_frame = False
            self._dirty = True
            if self._pending or (self._is_live() and not self._have_frame):
                self._schedule()
            return

        types = result.get("types") or ()
        images = result.get("images") or ()
        panes = (self.pane_a, self.pane_b)
        ok = True
        for pane, type_val, image in zip(panes, types, images):
            if pane.scope_type.value != type_val:
                ok = False
                continue
            pane.set_image(image)

        self._last_key = result.get("key")
        self._have_frame = ok
        if not ok:
            self._dirty = True

        if self._pending or self._dirty:
            self._schedule()
