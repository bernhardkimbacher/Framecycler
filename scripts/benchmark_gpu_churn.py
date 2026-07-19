#!/usr/bin/env python3
"""Headless GPU upload/pipeline churn microbenchmark.

Creates an offscreen QWindow + RhiRenderer (Null backend on offscreen),
fills a CacheManager with synthetic float16 frames, then drives N present
cycles while recording debug counters.

Usage:
  python3 scripts/benchmark_gpu_churn.py [--frames 60] [--width 512] [--height 288]
  python3 scripts/benchmark_gpu_churn.py --out benchmarks/20260719_gpu-upload-pipeline-churn_<sha>.json
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "src"))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
from PySide6.QtGui import QGuiApplication, QWindow
from PySide6.QtCore import QTimer

from src.framecycler import framecycler_engine


SIMPLE_VERT = """#version 440
layout(location = 0) in vec2 position;
layout(location = 1) in vec2 texCoord;
layout(location = 0) out vec2 v_uv;
layout(std140, binding = 0) uniform buf {
    float scale_x;
    float scale_y;
    float pan_x;
    float pan_y;
    int compare_mode;
    float wipe_pos;
    int channel_mask;
    int padding;
};
void main() {
    vec2 p = position * vec2(scale_x, scale_y) + vec2(pan_x, pan_y);
    gl_Position = vec4(p, 0.0, 1.0);
    v_uv = texCoord;
}
"""

SIMPLE_FRAG = """#version 440
layout(location = 0) in vec2 v_uv;
layout(location = 0) out vec4 fragColor;
layout(binding = 1) uniform sampler2D texA;
layout(binding = 2) uniform sampler2D texB;
layout(std140, binding = 0) uniform buf {
    float scale_x;
    float scale_y;
    float pan_x;
    float pan_y;
    int compare_mode;
    float wipe_pos;
    int channel_mask;
    int padding;
};
void main() {
    fragColor = texture(texA, v_uv);
}
"""


def run_bench(width: int, height: int, n_frames: int, cache_gb: float) -> dict:
    app = QGuiApplication.instance() or QGuiApplication(sys.argv)

    window = QWindow()
    window.resize(width, height)
    window.setSurfaceType(QWindow.SurfaceType.OpenGLSurface)
    window.create()
    window.show()
    app.processEvents()

    renderer = framecycler_engine.RhiRenderer()
    ok = renderer.initialize(int(window.winId()))
    if not ok:
        raise RuntimeError("RhiRenderer.initialize failed")
    renderer.set_exposed(True)
    renderer.set_pending_size(width, height)
    renderer.set_display_cache_limit_gb(cache_gb)
    renderer.set_upload_queue_policy(framecycler_engine.UploadQueuePolicy.EveryFrame)
    renderer.set_shader_sources("bench_simple", SIMPLE_VERT, SIMPLE_FRAG)
    app.processEvents()
    time.sleep(0.05)

    cache = framecycler_engine.CacheManager(cache_gb)
    channels = 4
    pixels = np.full((height, width, channels), np.float16(0.25), dtype=np.float16)
    # Pre-decode enough unique frames so present cycles swap textures.
    unique = max(8, min(n_frames, 24))
    for i in range(unique):
        frame = pixels.copy()
        frame[..., 0] = np.float16((i + 1) / (unique + 1))
        cache.write_frame(i, width, height, channels, frame)

    renderer.register_cache(0, cache)
    renderer.set_source_playhead(0, 0, 1, 0, unique - 1)

    upload_ms = []
    render_ms = []
    rebuilds_series = []
    srb_series = []
    created_series = []
    reused_series = []

    for i in range(n_frames):
        frame_index = i % unique
        params = framecycler_engine.RenderParams()
        params.compare_mode = 0
        params.sequence_index = 0
        params.scale_x = 1.0
        params.scale_y = 1.0
        slot = framecycler_engine.FrameSlotSpec()
        slot.source_index = 0
        slot.frame_index = frame_index
        slot.width = width
        slot.height = height
        slot.channels = channels
        slot.upload_token = frame_index
        slot.data_size = width * height * channels
        params.slots = [slot]
        renderer.update_render_params(params)
        renderer.request_redraw()
        # Allow render thread to process.
        deadline = time.time() + 1.0
        while time.time() < deadline:
            app.processEvents()
            stats = renderer.get_debug_stats()
            if stats.get("begin_frame_ok", 0) > i:
                break
            time.sleep(0.002)
        stats = renderer.get_debug_stats()
        upload_ms.append(float(stats.get("last_upload_ms", 0.0)))
        render_ms.append(float(stats.get("last_render_ms", 0.0)))
        rebuilds_series.append(int(stats.get("pipeline_rebuilds", 0)))
        srb_series.append(int(stats.get("srb_updates", 0)))
        created_series.append(int(stats.get("textures_created", 0)))
        reused_series.append(int(stats.get("textures_pooled_reuses", 0)))

    # Drain a few more events so last frame stats settle.
    for _ in range(10):
        app.processEvents()
        time.sleep(0.01)

    final = renderer.get_debug_stats()
    display = renderer.get_display_cache_stats()

    # Steady-state rebuilds after first 5 frames should be flat.
    warm_rebuilds = rebuilds_series[5:] if len(rebuilds_series) > 5 else rebuilds_series
    steady_rebuild_delta = (
        (warm_rebuilds[-1] - warm_rebuilds[0]) if len(warm_rebuilds) >= 2 else 0
    )

    result = {
        "width": width,
        "height": height,
        "n_frames": n_frames,
        "unique_frames": unique,
        "cache_gb": cache_gb,
        "null_backend": bool(renderer.is_fallback_null_backend()),
        "mean_upload_ms": statistics.mean(upload_ms) if upload_ms else 0.0,
        "mean_render_ms": statistics.mean(render_ms) if render_ms else 0.0,
        "p95_upload_ms": (
            statistics.quantiles(upload_ms, n=20)[18] if len(upload_ms) >= 20 else max(upload_ms or [0.0])
        ),
        "pipeline_rebuilds_final": int(final.get("pipeline_rebuilds", 0)),
        "srb_updates_final": int(final.get("srb_updates", 0)),
        "steady_rebuild_delta": int(steady_rebuild_delta),
        "textures_created": int(final.get("textures_created", 0)),
        "textures_pooled_reuses": int(final.get("textures_pooled_reuses", 0)),
        "staging_waits": int(final.get("staging_waits", 0)),
        "gpu_cache_hits": int(final.get("gpu_cache_hits", 0)),
        "gpu_cache_misses": int(final.get("gpu_cache_misses", 0)),
        "display_resident_frames": int(display.get("resident_frames", 0)),
        "frames_drawn": int(final.get("frames_drawn", 0)),
        "begin_frame_ok": int(final.get("begin_frame_ok", 0)),
    }

    renderer.shutdown()
    window.destroy()
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frames", type=int, default=48)
    parser.add_argument("--width", type=int, default=512)
    parser.add_argument("--height", type=int, default=288)
    parser.add_argument("--cache-gb", type=float, default=0.25)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--commit", type=str, default="")
    args = parser.parse_args()

    result = run_bench(args.width, args.height, args.frames, args.cache_gb)
    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "description": (
            "GPU upload & pipeline churn microbench (finding #2): "
            "fromRawData staging, stable SRB updateResources, texture pool."
        ),
        "commit": args.commit,
        "result": result,
    }

    print(json.dumps(payload["result"], indent=2))
    print(
        f"steady_rebuild_delta={result['steady_rebuild_delta']} "
        f"srb_updates={result['srb_updates_final']} "
        f"pooled_reuses={result['textures_pooled_reuses']} "
        f"mean_upload_ms={result['mean_upload_ms']:.3f}"
    )

    if args.out:
        out = args.out if args.out.is_absolute() else REPO_ROOT / args.out
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, indent=2) + "\n")
        print(f"Wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
