from __future__ import annotations

import shutil
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Callable


class ShaderBaker:
    """Debounced wrapper around pyside6-qsb for RHI shader baking."""

    def __init__(self, debounce_ms: int = 150):
        self.debounce_ms = debounce_ms
        self._timer: threading.Timer | None = None
        self._lock = threading.Lock()
        self._generation = 0
        self._qsb_tool = self._find_qsb_tool()

    @staticmethod
    def _find_qsb_tool() -> str | None:
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

    @property
    def available(self) -> bool:
        return self._qsb_tool is not None

    def cancel_pending(self) -> None:
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None

    def bake_async(
        self,
        vertex_source: str,
        fragment_source: str,
        callback: Callable[[bytes, bytes | None], None],
    ) -> None:
        """Schedule a debounced bake; callback receives (vert_qsb, frag_qsb_or_none)."""
        with self._lock:
            self._generation += 1
            generation = self._generation
            if self._timer is not None:
                self._timer.cancel()

            def run_bake() -> None:
                with self._lock:
                    if generation != self._generation:
                        return
                    self._timer = None
                try:
                    vert_qsb, frag_qsb = self.bake_sync(vertex_source, fragment_source)
                    callback(vert_qsb, frag_qsb)
                except Exception as exc:
                    callback(b"", None)
                    print(f"ShaderBaker: bake failed: {exc}")

            delay = max(self.debounce_ms, 0) / 1000.0
            self._timer = threading.Timer(delay, run_bake)
            self._timer.daemon = True
            self._timer.start()

    def bake_sync(self, vertex_source: str, fragment_source: str) -> tuple[bytes, bytes]:
        if not self._qsb_tool:
            raise RuntimeError("pyside6-qsb not found; install PySide6 in the active environment")

        with tempfile.TemporaryDirectory(prefix="framecycler_shader_bake_") as tmp:
            tmp_path = Path(tmp)
            vert_path = tmp_path / "quad.vert"
            frag_path = tmp_path / "quad.frag"
            vert_qsb_path = tmp_path / "quad.vert.qsb"
            frag_qsb_path = tmp_path / "quad.frag.qsb"
            vert_path.write_text(vertex_source, encoding="utf-8")
            frag_path.write_text(fragment_source, encoding="utf-8")

            common_args = [
                "--glsl",
                "100es,120,150,330,430,450",
                "--hlsl",
                "50",
                "--msl",
                "12,20",
            ]
            for src, out in ((vert_path, vert_qsb_path), (frag_path, frag_qsb_path)):
                cmd = [self._qsb_tool, *common_args, str(src), "-o", str(out)]
                result = subprocess.run(cmd, capture_output=True, text=True)
                if result.returncode != 0:
                    raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "pyside6-qsb failed")

            return vert_qsb_path.read_bytes(), frag_qsb_path.read_bytes()
