#!/usr/bin/env python3
"""Before/after A/B for the C++ transport frame clock (finding #3).

Compares Python QTimer transport (FRAMECYCLER_PYTHON_TRANSPORT=1) vs the C++
monotonic clock using the same built engine — no separate worktree required.

Cadence (interval p95 / present fps) is computed from render-thread present
timestamps drained after the run — the same authoritative source for both modes.
Python main-thread cost is still measured via light-seek / tick hooks.

Usage:
  python3 scripts/benchmark_transport_ab.py --footage .temp/TestFootage
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


def _present_intervals_ms(samples: list[dict[str, Any]], trim: int = 5) -> tuple[list[float], dict[str, Any]]:
    """Build present intervals from drained render-thread samples."""
    if len(samples) < 2:
        return [], {
            "present_n": len(samples),
            "present_achieved_fps": 0.0,
            "present_span_s": 0.0,
        }

    # Trim warmup / teardown samples when we have enough.
    trimmed = samples
    if len(samples) > trim * 2 + 2:
        trimmed = samples[trim:-trim]

    intervals: list[float] = []
    for i in range(1, len(trimmed)):
        dt_ns = int(trimmed[i]["steady_ns"]) - int(trimmed[i - 1]["steady_ns"])
        if dt_ns > 0:
            intervals.append(dt_ns / 1_000_000.0)

    span_s = (int(trimmed[-1]["steady_ns"]) - int(trimmed[0]["steady_ns"])) / 1e9
    present_fps = (len(trimmed) - 1) / span_s if span_s > 0 else 0.0
    meta = {
        "present_n": len(trimmed),
        "present_n_raw": len(samples),
        "present_achieved_fps": present_fps,
        "present_span_s": span_s,
        "present_trim": trim if trimmed is not samples else 0,
    }
    return intervals, meta


def run_one(
    label: str,
    sequence_name: str,
    exr_path: str,
    duration_s: float,
    use_cpp: bool,
) -> dict[str, Any]:
    from PySide6.QtWidgets import QApplication
    from framecycler.ui.main_window import MainWindow
    from framecycler.core.playback_timing import (
        PLAYBACK_TIMING_REALTIME,
        normalize_playback_timing,
    )

    app = QApplication.instance() or QApplication(sys.argv)
    window = MainWindow()
    # Realtime mode shows catch-up / cadence most clearly.
    window.settings.playback_timing = PLAYBACK_TIMING_REALTIME
    window.settings.display_cache_limit_gb = 4.0
    window.settings.decode_cache_limit_gb = 8.0
    window._apply_renderer_cache_settings()
    window.resize(1280, 720)
    window.show()
    app.processEvents()
    time.sleep(0.2)

    window._add_media([exr_path], mode="replace")
    app.processEvents()
    time.sleep(0.4)

    if not window.sources:
        window.close()
        raise RuntimeError(f"[{label}] no source for {sequence_name}")

    renderer = window.viewport.native_renderer
    if not hasattr(renderer, "drain_present_timings"):
        window.close()
        raise RuntimeError(
            f"[{label}] renderer missing drain_present_timings — rebuild the engine"
        )

    # Prefetch a short window so every_frame gating does not stall realtime.
    src = window.sources[0]
    if hasattr(src.cache, "trigger_prefetch"):
        src.cache.trigger_prefetch()
    for i in range(min(24, int(getattr(src, "frame_count", 1)))):
        g = window.start_frame + i
        window.seek_to_frame(g)
        app.processEvents()
        time.sleep(0.01)

    window.seek_to_frame(window.start_frame)
    app.processEvents()

    python_ms: list[float] = []

    # Patch light seek / transport notify to measure Python main-thread cost only.
    orig_light = window._seek_light

    def timed_light(frame, segment):
        t0 = time.perf_counter()
        orig_light(frame, segment)
        python_ms.append((time.perf_counter() - t0) * 1000.0)

    window._seek_light = timed_light  # type: ignore[method-assign]

    orig_tick = window._playback_tick

    def timed_tick():
        before = window.current_frame
        t0 = time.perf_counter()
        orig_tick()
        dt_ms = (time.perf_counter() - t0) * 1000.0
        if window.current_frame != before:
            python_ms.append(dt_ms)

    window._playback_tick = timed_tick  # type: ignore[method-assign]

    target_fps = float(window.fps) if window.fps > 0 else 24.0

    renderer.clear_present_timings()
    renderer.set_present_timing_enabled(True)

    window.start_playback()
    t_start = time.perf_counter()
    start_playhead = int(window.current_frame)
    while time.perf_counter() - t_start < duration_s:
        app.processEvents()
        time.sleep(0.001)
    window.stop_playback()
    elapsed = time.perf_counter() - t_start

    samples = list(renderer.drain_present_timings())
    renderer.set_present_timing_enabled(False)

    end_playhead = int(window.current_frame)
    if use_cpp:
        try:
            end_playhead = int(renderer.get_transport_frame())
        except Exception:
            pass

    intervals_ms, present_meta = _present_intervals_ms(samples)
    frames_covered = abs(end_playhead - start_playhead)
    present_fps = float(present_meta["present_achieved_fps"])
    content_fps = frames_covered / elapsed if elapsed > 0 else 0.0
    cadence_ratio = present_fps / target_fps if target_fps > 0 else 0.0

    debug_stats: dict[str, Any] = {}
    try:
        debug_stats = dict(renderer.get_debug_stats())
    except Exception:
        pass

    # Compact present samples for JSON (full list; typically < few hundred).
    present_samples = [
        {
            "steady_ns": int(s["steady_ns"]),
            "global_frame": int(s["global_frame"]),
            "frames_drawn": int(s["frames_drawn"]),
        }
        for s in samples
    ]

    result = {
        "label": label,
        "sequence": sequence_name,
        "use_cpp_transport": use_cpp,
        "playback_timing": normalize_playback_timing(window.settings.playback_timing),
        "target_fps": target_fps,
        "duration_s": elapsed,
        "frames_covered": frames_covered,
        "content_fps": content_fps,
        "present_n": present_meta["present_n"],
        "present_n_raw": present_meta.get("present_n_raw", present_meta["present_n"]),
        "present_achieved_fps": present_fps,
        "cadence_ratio": cadence_ratio,
        "present_interval_mean_ms": _mean(intervals_ms),
        "present_interval_p95_ms": _p95(intervals_ms),
        # Aliases used by the comparison table (authoritative present intervals).
        "achieved_fps": present_fps,
        "interval_mean_ms": _mean(intervals_ms),
        "interval_p95_ms": _p95(intervals_ms),
        "python_ms_mean": _mean(python_ms),
        "python_ms_p95": _p95(python_ms),
        "final_frame": end_playhead,
        "start_frame": int(window.start_frame),
        "debug_stats": debug_stats,
        "present_samples": present_samples,
    }
    print(
        f"[{label}] {sequence_name}: present={present_fps:.2f}/{target_fps:.2f}fps "
        f"content={content_fps:.2f}fps "
        f"ratio={cadence_ratio:.3f} "
        f"py_ms={result['python_ms_mean']:.3f} "
        f"present_p95={result['present_interval_p95_ms']:.2f}ms "
        f"n={result['present_n']} "
        f"upload_ms={debug_stats.get('upload_ms_total', 0):.1f} "
        f"endF_max={debug_stats.get('end_frame_ms_max', 0):.1f}",
        flush=True,
    )
    window.close()
    app.processEvents()
    return result


def run_worker(args: argparse.Namespace) -> int:
    engine_root = Path(args.run_engine_root)
    if not engine_root.is_absolute():
        engine_root = REPO_ROOT / engine_root
    sys.path.insert(0, str(engine_root / "src"))
    sys.path.insert(0, str(REPO_ROOT))

    footage = Path(args.footage)
    if not footage.is_absolute():
        footage = REPO_ROOT / footage

    sequences = []
    for child in sorted(footage.iterdir()):
        if child.is_dir() and not child.name.startswith("."):
            files = sorted(glob.glob(str(child / "*.exr")))
            if files:
                sequences.append((child.name, files[0]))
    if not sequences:
        print(f"No sequences under {footage}", file=sys.stderr)
        return 1

    use_cpp = args.run_label == "after"
    runs = [
        run_one(args.run_label, name, path, args.duration, use_cpp)
        for name, path in sequences
    ]
    out = {
        "label": args.run_label,
        "use_cpp_transport": use_cpp,
        "engine_root": str(engine_root),
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
        rows.append(
            {
                "sequence": b["sequence"],
                "target_fps": b["target_fps"],
                "before_achieved_fps": b["present_achieved_fps"],
                "after_achieved_fps": a["present_achieved_fps"],
                "before_content_fps": b.get("content_fps", 0.0),
                "after_content_fps": a.get("content_fps", 0.0),
                "before_cadence_ratio": b["cadence_ratio"],
                "after_cadence_ratio": a["cadence_ratio"],
                "before_interval_p95_ms": b["present_interval_p95_ms"],
                "after_interval_p95_ms": a["present_interval_p95_ms"],
                "before_interval_mean_ms": b["present_interval_mean_ms"],
                "after_interval_mean_ms": a["present_interval_mean_ms"],
                "before_present_n": b["present_n"],
                "after_present_n": a["present_n"],
                "before_python_ms": b["python_ms_mean"],
                "after_python_ms": a["python_ms_mean"],
                "before_upload_ms_total": (b.get("debug_stats") or {}).get(
                    "upload_ms_total", 0.0
                ),
                "after_upload_ms_total": (a.get("debug_stats") or {}).get(
                    "upload_ms_total", 0.0
                ),
                "before_end_frame_ms_max": (b.get("debug_stats") or {}).get(
                    "end_frame_ms_max", 0.0
                ),
                "after_end_frame_ms_max": (a.get("debug_stats") or {}).get(
                    "end_frame_ms_max", 0.0
                ),
                "python_speedup": (
                    b["python_ms_mean"] / a["python_ms_mean"]
                    if a["python_ms_mean"] > 0
                    else 0.0
                ),
                "p95_ratio_after_over_before": (
                    a["present_interval_p95_ms"] / b["present_interval_p95_ms"]
                    if b["present_interval_p95_ms"] > 0
                    else 0.0
                ),
            }
        )
    return rows


def _print_table(rows: list[dict]) -> None:
    print()
    print("## Before / After Transport Clock (present-thread intervals)")
    print()
    hdr = (
        f"{'sequence':<14} {'fps':>6} "
        f"{'pres B':>7} {'pres A':>7} "
        f"{'cont B':>7} {'cont A':>7} "
        f"{'p95 B':>7} {'p95 A':>7} {'p95×':>5} "
        f"{'py ms B':>8} {'py ms A':>8} {'py x':>6}"
    )
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        print(
            f"{r['sequence']:<14} {r['target_fps']:6.2f} "
            f"{r['before_achieved_fps']:7.2f} {r['after_achieved_fps']:7.2f} "
            f"{r['before_content_fps']:7.2f} {r['after_content_fps']:7.2f} "
            f"{r['before_interval_p95_ms']:7.2f} {r['after_interval_p95_ms']:7.2f} "
            f"{r['p95_ratio_after_over_before']:5.2f} "
            f"{r['before_python_ms']:8.3f} {r['after_python_ms']:8.3f} "
            f"{r['python_speedup']:6.2f}"
        )
    print()
    print(
        "Intervals from render-thread present timestamps (identical source both modes). "
        "pres = present fps; cont = content fps (frames_covered/duration). "
        "before = Python QTimer; after = C++ TransportClock. "
        "p95× = after_p95/before_p95 (1.0 = no change). "
        "py x = before_ms/after_ms on per-frame Python work."
    )
    print()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--footage",
        type=Path,
        default=REPO_ROOT / ".temp" / "TestFootage",
    )
    parser.add_argument("--duration", type=float, default=10.0)
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=REPO_ROOT / "benchmarks",
    )
    parser.add_argument("--engine-root", type=Path, default=REPO_ROOT)
    parser.add_argument("--run-label", default="", help=argparse.SUPPRESS)
    parser.add_argument("--run-engine-root", default="", help=argparse.SUPPRESS)
    parser.add_argument("--run-out", default="", help=argparse.SUPPRESS)
    args = parser.parse_args()

    if args.run_label:
        return run_worker(args)

    engine_root = args.engine_root if args.engine_root.is_absolute() else REPO_ROOT / args.engine_root
    footage = args.footage if args.footage.is_absolute() else REPO_ROOT / args.footage
    out_dir = args.out_dir if args.out_dir.is_absolute() else REPO_ROOT / args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    before_json = out_dir / f"_transport_before_{stamp}.json"
    after_json = out_dir / f"_transport_after_{stamp}.json"
    script = str(Path(__file__).resolve())

    def child(label: str, out_json: Path, use_cpp: bool) -> None:
        env = os.environ.copy()
        env["PYTHONPATH"] = str(engine_root / "src") + os.pathsep + env.get("PYTHONPATH", "")
        if use_cpp:
            env.pop("FRAMECYCLER_PYTHON_TRANSPORT", None)
        else:
            env["FRAMECYCLER_PYTHON_TRANSPORT"] = "1"
        cmd = [
            sys.executable,
            script,
            "--run-label",
            label,
            "--run-engine-root",
            str(engine_root),
            "--run-out",
            str(out_json),
            "--footage",
            str(footage),
            "--duration",
            str(args.duration),
        ]
        print(f"--- subprocess: {label} (cpp={use_cpp}) ---", flush=True)
        subprocess.run(cmd, check=True, env=env)

    child("before", before_json, use_cpp=False)
    child("after", after_json, use_cpp=True)

    before = json.loads(before_json.read_text())
    after = json.loads(after_json.read_text())
    rows = _pair(before["runs"], after["runs"])
    _print_table(rows)

    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "description": (
            "Before/after transport clock (finding #3): Python PreciseTimer vs "
            "C++ monotonic presentation-driven clock."
        ),
        "methodology": (
            f"MainWindow realtime playback for {args.duration}s per sequence; "
            "before uses FRAMECYCLER_PYTHON_TRANSPORT=1 (QTimer fallback); "
            "after uses C++ TransportClock. "
            "Interval p95/mean and present fps are computed from render-thread "
            "present timestamps (drain_present_timings) — identical source for "
            "both modes. content_fps = frames_covered/duration so smooth-but-frozen "
            "playhead cannot masquerade as a win. "
            "Per-present GPU upload work is capped to ~2 jobs while transport is "
            "playing (lookahead reserved behind present-authoritative sync). "
            "Python per-frame cost is still measured via light-seek/tick hooks; "
            "debug_stats (upload_ms_total, end_frame_ms_max) and raw present_samples "
            "are persisted for stall correlation."
        ),
        "footage": str(footage),
        "duration_s": args.duration,
        "before": before,
        "after": after,
        "comparison": rows,
    }
    out_path = out_dir / "20260719_transport-frame-clock_ab.json"
    out_path.write_text(json.dumps(payload, indent=2) + "\n")
    before_json.unlink(missing_ok=True)
    after_json.unlink(missing_ok=True)
    print(f"Wrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
