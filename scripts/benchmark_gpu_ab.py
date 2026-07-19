#!/usr/bin/env python3
"""Before/after A/B GPU upload & pipeline churn benchmark.

Drives the real MainWindow (hosted QWindow + Metal/D3D/Vulkan) so RHI init
works on macOS. Compares two built engine worktrees via subprocess isolation.

Usage:
  python3 scripts/benchmark_gpu_ab.py \\
      --before .temp/bench-gpu-before \\
      --after . \\
      --footage .temp/TestFootage
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]


def _mean(vals: list[float]) -> float:
    return float(statistics.mean(vals)) if vals else 0.0


def _p95(vals: list[float]) -> float:
    if not vals:
        return 0.0
    if len(vals) == 1:
        return vals[0]
    s = sorted(vals)
    k = (len(s) - 1) * 0.95
    f = int(k)
    c = min(f + 1, len(s) - 1)
    if f == c:
        return s[f]
    return s[f] + (s[c] - s[f]) * (k - f)


def _wait_drawn(app, renderer, before_drawn: int, timeout_s: float = 2.0) -> dict[str, Any]:
    deadline = time.time() + timeout_s
    stats = renderer.get_debug_stats()
    while time.time() < deadline:
        app.processEvents()
        time.sleep(0.01)
        stats = renderer.get_debug_stats()
        if int(stats.get("frames_drawn", 0)) > before_drawn:
            break
    return stats


def _snapshot_counters(stats: dict[str, Any]) -> dict[str, int]:
    return {
        "pipeline_rebuilds": int(stats.get("pipeline_rebuilds", 0)),
        "srb_updates": int(stats.get("srb_updates", 0)),
        "textures_created": int(stats.get("textures_created", 0)),
        "textures_pooled_reuses": int(stats.get("textures_pooled_reuses", 0)),
        "gpu_cache_hits": int(stats.get("gpu_cache_hits", 0)),
        "gpu_cache_misses": int(stats.get("gpu_cache_misses", 0)),
        "frames_drawn": int(stats.get("frames_drawn", 0)),
    }


def run_one_sequence(
    label: str,
    sequence_name: str,
    exr_paths: list[str],
    display_cache_gb: float,
    seek_passes: int,
) -> dict[str, Any]:
    from PySide6.QtWidgets import QApplication

    from framecycler.ui.main_window import MainWindow
    from framecycler.core.media_source import local_index_to_decoder_frame

    app = QApplication.instance() or QApplication(sys.argv)
    window = MainWindow()
    window.settings.display_cache_limit_gb = display_cache_gb
    window.settings.decode_cache_limit_gb = max(8.0, display_cache_gb)
    window._apply_renderer_cache_settings()
    window.resize(1280, 720)
    window.show()
    app.processEvents()
    time.sleep(0.25)

    window._add_media(exr_paths, mode="replace")
    app.processEvents()
    time.sleep(0.5)

    sources = window.sources
    if not sources:
        window.close()
        raise RuntimeError(f"[{label}] no source loaded for {sequence_name}")
    src = sources[0]

    renderer = window.viewport.native_renderer
    # Timeline uses global frame numbers (EXR start_frame..), not local 0..N.
    global_start = int(window.start_frame)
    frame_count = max(1, int(window.end_frame) - global_start + 1)
    frame_count = min(frame_count, 16)
    global_frames = [global_start + i for i in range(frame_count)]

    # Wait for CPU cache to fill the first frame_count frames.
    if hasattr(src.cache, "trigger_prefetch"):
        src.cache.trigger_prefetch()
    for global_frame in global_frames:
        # Map global timeline frame → decoder frame via the loaded source.
        local_frame = global_frame - global_start
        decoder_frame = local_index_to_decoder_frame(src, local_frame)
        deadline = time.time() + 45.0
        while time.time() < deadline:
            app.processEvents()
            if src.cache.native_cache.has_frame(decoder_frame):
                break
            window.seek_to_frame(global_frame)
            if hasattr(src.cache, "trigger_prefetch"):
                src.cache.trigger_prefetch()
            app.processEvents()
            time.sleep(0.05)
        else:
            print(
                f"[{label}] WARN: frame {global_frame}/{decoder_frame} not in CPU cache",
                flush=True,
            )

    def clear_gpu_and_settle() -> None:
        renderer.clear_display_cache()
        deadline = time.time() + 2.0
        while time.time() < deadline:
            app.processEvents()
            time.sleep(0.02)
            display = renderer.get_display_cache_stats()
            if int(display.get("resident_frames", -1)) == 0:
                break
        # One more present after clear so bind state is empty.
        before_drawn = int(renderer.get_debug_stats().get("frames_drawn", 0))
        window.seek_to_frame(global_start)
        _wait_drawn(app, renderer, before_drawn)
        renderer.clear_display_cache()
        for _ in range(20):
            app.processEvents()
            time.sleep(0.01)
            if int(renderer.get_display_cache_stats().get("resident_frames", -1)) == 0:
                break

    def seek_and_sample(global_frame: int, require_upload: bool = False) -> dict[str, Any]:
        before_drawn = int(renderer.get_debug_stats().get("frames_drawn", 0))
        before_misses = int(renderer.get_debug_stats().get("gpu_cache_misses", 0))
        before_srb = int(renderer.get_debug_stats().get("srb_updates", 0))
        before_rb = int(renderer.get_debug_stats().get("pipeline_rebuilds", 0))
        window.seek_to_frame(global_frame)
        deadline = time.time() + 3.0
        stats = renderer.get_debug_stats()
        while time.time() < deadline:
            app.processEvents()
            time.sleep(0.01)
            stats = renderer.get_debug_stats()
            drawn = int(stats.get("frames_drawn", 0)) > before_drawn
            uploaded = int(stats.get("last_upload_count", 0)) > 0
            missed = int(stats.get("gpu_cache_misses", 0)) > before_misses
            churned = (
                int(stats.get("srb_updates", 0)) > before_srb
                or int(stats.get("pipeline_rebuilds", 0)) > before_rb
            )
            if require_upload:
                if drawn and (uploaded or missed or float(stats.get("last_upload_ms", 0.0)) >= 1.0):
                    break
            elif drawn or churned:
                break
        return stats

    clear_gpu_and_settle()

    stats0 = renderer.get_debug_stats()
    has_churn_counters = "pipeline_rebuilds" in stats0
    base = _snapshot_counters(stats0)

    # --- Cold pass: clear once, then seek N globals (GPU miss / upload heavy) ---
    cold_upload_ms: list[float] = []
    cold_render_ms: list[float] = []
    cold_draw_ms: list[float] = []
    cold_upload_bytes: list[int] = []
    cold_upload_counts: list[int] = []
    cold_rebuild_series: list[int] = []
    cold_srb_series: list[int] = []

    for global_frame in global_frames:
        stats = seek_and_sample(global_frame)
        cold_upload_ms.append(float(stats.get("last_upload_ms", 0.0)))
        cold_render_ms.append(float(stats.get("last_render_ms", 0.0)))
        cold_draw_ms.append(float(stats.get("last_draw_ms", 0.0)))
        cold_upload_bytes.append(int(stats.get("last_upload_bytes", 0)))
        cold_upload_counts.append(int(stats.get("last_upload_count", 0)))
        if has_churn_counters:
            cold_rebuild_series.append(int(stats.get("pipeline_rebuilds", 0)))
            cold_srb_series.append(int(stats.get("srb_updates", 0)))

    cold_end = _snapshot_counters(renderer.get_debug_stats())

    # Isolated per-frame upload timings: clear GPU between seeks so each sample
    # is a true cold upload (avoids lookahead masking later frames).
    isolated_upload_ms: list[float] = []
    isolated_upload_bytes: list[int] = []
    isolated_upload_counts: list[int] = []
    for global_frame in global_frames:
        clear_gpu_and_settle()
        stats = seek_and_sample(global_frame, require_upload=True)
        # Prefer samples that actually moved bytes; fall back to non-trivial upload phase.
        up_count = int(stats.get("last_upload_count", 0))
        up_ms = float(stats.get("last_upload_ms", 0.0))
        up_bytes = int(stats.get("last_upload_bytes", 0))
        if up_count > 0 or up_bytes > 0 or up_ms >= 1.0:
            isolated_upload_ms.append(up_ms)
            isolated_upload_bytes.append(up_bytes)
            isolated_upload_counts.append(up_count)

    # --- Warm pass: no clear; re-seek same frames (GPU hits) ---
    # Seed GPU cache for all frames once more without clearing.
    for global_frame in global_frames:
        seek_and_sample(global_frame)

    warm_base = _snapshot_counters(renderer.get_debug_stats())
    warm_upload_ms: list[float] = []
    warm_upload_bytes: list[int] = []
    warm_upload_counts: list[int] = []
    warm_rebuild_series: list[int] = []
    warm_srb_series: list[int] = []

    for _pass in range(max(1, seek_passes)):
        for global_frame in global_frames:
            stats = seek_and_sample(global_frame)
            warm_upload_ms.append(float(stats.get("last_upload_ms", 0.0)))
            warm_upload_bytes.append(int(stats.get("last_upload_bytes", 0)))
            warm_upload_counts.append(int(stats.get("last_upload_count", 0)))
            if has_churn_counters:
                warm_rebuild_series.append(int(stats.get("pipeline_rebuilds", 0)))
                warm_srb_series.append(int(stats.get("srb_updates", 0)))

    warm_end = _snapshot_counters(renderer.get_debug_stats())
    final = renderer.get_debug_stats()
    display = renderer.get_display_cache_stats()

    def delta(end: dict[str, int], start: dict[str, int], key: str) -> int | None:
        if not has_churn_counters and key in (
            "pipeline_rebuilds",
            "srb_updates",
            "textures_created",
            "textures_pooled_reuses",
        ):
            return None
        return end[key] - start[key]

    cold_rebuild_delta = delta(cold_end, base, "pipeline_rebuilds")
    warm_rebuild_delta = delta(warm_end, warm_base, "pipeline_rebuilds")
    cold_srb_delta = delta(cold_end, base, "srb_updates")
    warm_srb_delta = delta(warm_end, warm_base, "srb_updates")
    pool_reuse_delta = delta(warm_end, base, "textures_pooled_reuses")
    tex_created_delta = delta(warm_end, base, "textures_created")

    # Steady-state: rebuild growth across warm seek series (want 0 after).
    steady_rebuild_delta = 0
    if len(warm_rebuild_series) >= 2:
        steady_rebuild_delta = warm_rebuild_series[-1] - warm_rebuild_series[0]

    result = {
        "label": label,
        "sequence": sequence_name,
        "n_frames": frame_count,
        "display_cache_gb": display_cache_gb,
        "has_churn_counters": has_churn_counters,
        "null_backend": bool(getattr(renderer, "is_fallback_null_backend", lambda: False)()),
        # Streaming cold seek (one clear, then N seeks)
        "cold_mean_upload_ms": _mean(cold_upload_ms),
        "cold_p95_upload_ms": _p95(cold_upload_ms),
        "cold_mean_render_ms": _mean(cold_render_ms),
        "cold_p95_render_ms": _p95(cold_render_ms),
        "cold_mean_draw_ms": _mean(cold_draw_ms),
        "cold_mean_upload_bytes": _mean([float(x) for x in cold_upload_bytes]),
        "cold_mean_upload_count": _mean([float(x) for x in cold_upload_counts]),
        "cold_pipeline_rebuilds": cold_rebuild_delta,
        "cold_srb_updates": cold_srb_delta,
        # Isolated cold uploads (clear between each frame)
        "isolated_upload_n": len(isolated_upload_ms),
        "isolated_mean_upload_ms": _mean(isolated_upload_ms),
        "isolated_p95_upload_ms": _p95(isolated_upload_ms),
        "isolated_mean_upload_bytes": _mean([float(x) for x in isolated_upload_bytes]),
        # Warm scrub
        "warm_mean_upload_ms": _mean(warm_upload_ms),
        "warm_mean_upload_bytes": _mean([float(x) for x in warm_upload_bytes]),
        "warm_mean_upload_count": _mean([float(x) for x in warm_upload_counts]),
        "warm_pipeline_rebuilds": warm_rebuild_delta,
        "warm_srb_updates": warm_srb_delta,
        "steady_rebuild_delta": int(steady_rebuild_delta) if has_churn_counters else None,
        "pipeline_rebuilds_final": int(final.get("pipeline_rebuilds", 0)) if has_churn_counters else None,
        "srb_updates_final": int(final.get("srb_updates", 0)) if has_churn_counters else None,
        "textures_created": tex_created_delta,
        "textures_pooled_reuses": pool_reuse_delta,
        "staging_waits": int(final.get("staging_waits", 0)) if has_churn_counters else None,
        "gpu_cache_hits": int(final.get("gpu_cache_hits", display.get("hits", 0))),
        "gpu_cache_misses": int(final.get("gpu_cache_misses", display.get("misses", 0))),
        "display_resident_frames": int(display.get("resident_frames", 0)),
        "frames_drawn": int(final.get("frames_drawn", 0)),
    }

    print(
        f"[{label}] {sequence_name}: "
        f"iso_up={result['isolated_mean_upload_ms']:.2f}ms(n={result['isolated_upload_n']}) "
        f"cold_rb={result['cold_pipeline_rebuilds']} warm_rb={result['warm_pipeline_rebuilds']} "
        f"warm_srb={result['warm_srb_updates']} pool={result['textures_pooled_reuses']}",
        flush=True,
    )

    window.close()
    app.processEvents()
    return result


def run_worker(args: argparse.Namespace) -> int:
    engine_root = Path(args.run_engine_root)
    if not engine_root.is_absolute():
        engine_root = REPO_ROOT / engine_root
    src = engine_root / "src"
    sys.path.insert(0, str(src))
    sys.path.insert(0, str(REPO_ROOT))

    footage = Path(args.footage)
    if not footage.is_absolute():
        footage = REPO_ROOT / footage

    sequences = []
    for child in sorted(footage.iterdir()):
        if child.is_dir() and not child.name.startswith("."):
            files = sorted(glob.glob(str(child / "*.exr")))
            if files:
                sequences.append((child.name, [files[0]]))
    if not sequences:
        print(f"No sequences under {footage}", file=sys.stderr)
        return 1

    from framecycler import framecycler_engine

    runs = []
    for name, files in sequences:
        runs.append(
            run_one_sequence(
                args.run_label,
                name,
                files,
                args.display_cache_gb,
                args.seek_passes,
            )
        )

    out = {
        "label": args.run_label,
        "engine_root": str(engine_root),
        "so_path": getattr(framecycler_engine, "__file__", ""),
        "has_set_force_null": hasattr(framecycler_engine.RhiRenderer, "set_force_null_backend"),
        "runs": runs,
    }
    Path(args.run_out).write_text(json.dumps(out, indent=2) + "\n")
    return 0


def _pair(before_runs: list[dict], after_runs: list[dict]) -> list[dict]:
    after_map = {r["sequence"]: r for r in after_runs}
    rows = []
    for b in before_runs:
        a = after_map.get(b["sequence"])
        if not a:
            continue

        def speedup(before_v: float, after_v: float) -> float:
            if after_v <= 0:
                return 0.0
            return before_v / after_v

        rows.append(
            {
                "sequence": b["sequence"],
                "n_frames": b["n_frames"],
                "before_isolated_upload_ms": b["isolated_mean_upload_ms"],
                "after_isolated_upload_ms": a["isolated_mean_upload_ms"],
                "upload_speedup": speedup(
                    b["isolated_mean_upload_ms"], a["isolated_mean_upload_ms"]
                ),
                "before_isolated_n": b["isolated_upload_n"],
                "after_isolated_n": a["isolated_upload_n"],
                "before_cold_rebuilds": b.get("cold_pipeline_rebuilds"),
                "after_cold_rebuilds": a.get("cold_pipeline_rebuilds"),
                "before_warm_rebuilds": b.get("warm_pipeline_rebuilds"),
                "after_warm_rebuilds": a.get("warm_pipeline_rebuilds"),
                "before_warm_srb": b.get("warm_srb_updates"),
                "after_warm_srb": a.get("warm_srb_updates"),
                "before_steady_rebuild_delta": b.get("steady_rebuild_delta"),
                "after_steady_rebuild_delta": a.get("steady_rebuild_delta"),
                "before_pool_reuse": b.get("textures_pooled_reuses"),
                "after_pool_reuse": a.get("textures_pooled_reuses"),
                "before_tex_created": b.get("textures_created"),
                "after_tex_created": a.get("textures_created"),
            }
        )
    return rows


def _print_table(rows: list[dict]) -> None:
    print()
    print("## Before / After GPU Upload & Pipeline Churn")
    print()
    hdr = (
        f"{'sequence':<14} {'N':>3} "
        f"{'up ms B':>8} {'up ms A':>8} {'up x':>6} "
        f"{'cold rb B':>9} {'cold rb A':>9} "
        f"{'warm rb B':>9} {'warm rb A':>9} "
        f"{'warm srb A':>10} {'pool A':>6}"
    )
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        def fmt(v: Any) -> str:
            if v is None:
                return "n/a"
            return str(int(v))

        print(
            f"{r['sequence']:<14} {r['n_frames']:3d} "
            f"{r['before_isolated_upload_ms']:8.2f} {r['after_isolated_upload_ms']:8.2f} "
            f"{r['upload_speedup']:6.2f} "
            f"{fmt(r['before_cold_rebuilds']):>9} {fmt(r['after_cold_rebuilds']):>9} "
            f"{fmt(r['before_warm_rebuilds']):>9} {fmt(r['after_warm_rebuilds']):>9} "
            f"{fmt(r['after_warm_srb']):>10} {fmt(r['after_pool_reuse']):>6}"
        )
    print()
    print(
        "up x = before_ms / after_ms on isolated cold uploads (higher better). "
        "warm rb = pipeline rebuilds during warm scrub (want ~0 after). "
        "warm srb A = SRB updateResources during warm scrub. pool A = texture pool reuses."
    )
    print()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--before", type=Path, help="Worktree root @ pre-change")
    parser.add_argument("--after", type=Path, help="Worktree/repo root @ post-change")
    parser.add_argument(
        "--footage",
        type=Path,
        default=REPO_ROOT / ".temp" / "TestFootage",
    )
    parser.add_argument("--display-cache-gb", type=float, default=4.0)
    parser.add_argument("--seek-passes", type=int, default=1)
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=REPO_ROOT / "benchmarks",
    )
    parser.add_argument("--run-label", default="", help=argparse.SUPPRESS)
    parser.add_argument("--run-engine-root", default="", help=argparse.SUPPRESS)
    parser.add_argument("--run-out", default="", help=argparse.SUPPRESS)
    args = parser.parse_args()

    if args.run_label:
        return run_worker(args)

    if not args.before or not args.after:
        print("--before and --after required", file=sys.stderr)
        return 2

    before_root = args.before if args.before.is_absolute() else REPO_ROOT / args.before
    after_root = args.after if args.after.is_absolute() else REPO_ROOT / args.after
    footage = args.footage if args.footage.is_absolute() else REPO_ROOT / args.footage
    out_dir = args.out_dir if args.out_dir.is_absolute() else REPO_ROOT / args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    before_json = out_dir / f"_gpu_before_{stamp}.json"
    after_json = out_dir / f"_gpu_after_{stamp}.json"

    script = str(Path(__file__).resolve())

    def child(label: str, root: Path, out_json: Path) -> None:
        cmd = [
            sys.executable,
            script,
            "--run-label",
            label,
            "--run-engine-root",
            str(root),
            "--run-out",
            str(out_json),
            "--footage",
            str(footage),
            "--display-cache-gb",
            str(args.display_cache_gb),
            "--seek-passes",
            str(args.seek_passes),
        ]
        print(f"--- subprocess: {label} ({root}) ---", flush=True)
        env = os.environ.copy()
        env["PYTHONPATH"] = str(root / "src") + os.pathsep + env.get("PYTHONPATH", "")
        subprocess.run(cmd, check=True, env=env)

    child("before", before_root, before_json)
    child("after", after_root, after_json)

    before = json.loads(before_json.read_text())
    after = json.loads(after_json.read_text())
    rows = _pair(before["runs"], after["runs"])
    _print_table(rows)

    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "description": (
            "Before/after GPU upload & pipeline churn (finding #2): "
            "fromRawData staging, stable SRB updateResources, tile dynamic UBO, texture pool."
        ),
        "change": {
            "before_commit": "a7fb893",
            "after_commit": "wip-gpu-churn",
            "after_subject": "GPU upload & pipeline churn hardening",
        },
        "methodology": (
            "MainWindow hosted QWindow; load EXR sequence; wait for CPU cache; "
            "clear display cache; (1) streaming cold seek measuring pipeline rebuilds; "
            "(2) isolated cold uploads with clear between each seek; "
            "(3) warm scrub measuring pipeline rebuilds vs SRB updates. "
            f"display_cache_gb={args.display_cache_gb}."
        ),
        "footage": str(footage),
        "display_cache_gb": args.display_cache_gb,
        "before": before,
        "after": after,
        "comparison": rows,
    }
    out_path = out_dir / "20260719_gpu-upload-pipeline-churn_ab.json"
    out_path.write_text(json.dumps(payload, indent=2) + "\n")
    before_json.unlink(missing_ok=True)
    after_json.unlink(missing_ok=True)
    wip = out_dir / "20260719_gpu-upload-pipeline-churn_wip.json"
    if wip.exists():
        wip.unlink()
    print(f"Wrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
