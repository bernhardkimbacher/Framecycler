#!/usr/bin/env python3
"""Before/after A/B for the C++ transport frame clock (finding #3).

Compares Python QTimer transport (FRAMECYCLER_PYTHON_TRANSPORT=1) vs the C++
monotonic clock using the same built engine — no separate worktree required.

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

    frame_times: list[float] = []
    python_ms: list[float] = []
    last_frame = {"v": window.current_frame}
    last_t = {"v": time.perf_counter()}

    def on_frame_probe():
        # Hook via polling the playhead rather than packages.
        return

    # Patch light seek / transport notify to measure Python main-thread cost.
    orig_light = window._seek_light

    def timed_light(frame, segment):
        t0 = time.perf_counter()
        orig_light(frame, segment)
        python_ms.append((time.perf_counter() - t0) * 1000.0)
        now = time.perf_counter()
        if frame != last_frame["v"]:
            frame_times.append(now - last_t["v"])
            last_frame["v"] = frame
            last_t["v"] = now

    window._seek_light = timed_light  # type: ignore[method-assign]

    # Also time the QTimer tick path for the Python transport mode.
    orig_tick = window._playback_tick

    def timed_tick():
        before = window.current_frame
        t0 = time.perf_counter()
        orig_tick()
        dt_ms = (time.perf_counter() - t0) * 1000.0
        if window.current_frame != before:
            python_ms.append(dt_ms)
            now = time.perf_counter()
            frame_times.append(now - last_t["v"])
            last_frame["v"] = window.current_frame
            last_t["v"] = now

    window._playback_tick = timed_tick  # type: ignore[method-assign]

    target_fps = float(window.fps) if window.fps > 0 else 24.0
    window.start_playback()
    t_start = time.perf_counter()
    start_playhead = int(window.current_frame)
    while time.perf_counter() - t_start < duration_s:
        app.processEvents()
        time.sleep(0.001)
        if use_cpp:
            # Prefer the authoritative C++ playhead for cadence.
            try:
                cpp_frame = int(window.viewport.native_renderer.get_transport_frame())
            except Exception:
                cpp_frame = int(window.current_frame)
            now = time.perf_counter()
            if cpp_frame != last_frame["v"]:
                frame_times.append(now - last_t["v"])
                last_frame["v"] = cpp_frame
                last_t["v"] = now
    window.stop_playback()
    elapsed = time.perf_counter() - t_start
    end_playhead = int(window.current_frame)
    if use_cpp:
        try:
            end_playhead = int(window.viewport.native_renderer.get_transport_frame())
        except Exception:
            pass

    n_advances = len(frame_times)
    frames_covered = abs(end_playhead - start_playhead)
    achieved_fps = frames_covered / elapsed if elapsed > 0 else 0.0
    intervals_ms = [t * 1000.0 for t in frame_times[1:]]  # drop first gap

    result = {
        "label": label,
        "sequence": sequence_name,
        "use_cpp_transport": use_cpp,
        "playback_timing": normalize_playback_timing(window.settings.playback_timing),
        "target_fps": target_fps,
        "duration_s": elapsed,
        "n_advances": n_advances,
        "frames_covered": frames_covered,
        "achieved_fps": achieved_fps,
        "cadence_ratio": achieved_fps / target_fps if target_fps > 0 else 0.0,
        "interval_p95_ms": _p95(intervals_ms),
        "interval_mean_ms": _mean(intervals_ms),
        "python_ms_mean": _mean(python_ms),
        "python_ms_p95": _p95(python_ms),
        "final_frame": end_playhead,
        "start_frame": int(window.start_frame),
    }
    print(
        f"[{label}] {sequence_name}: achieved={achieved_fps:.2f}/{target_fps:.2f}fps "
        f"ratio={result['cadence_ratio']:.3f} "
        f"py_ms={result['python_ms_mean']:.3f} "
        f"interval_p95={result['interval_p95_ms']:.2f}ms",
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
                "before_achieved_fps": b["achieved_fps"],
                "after_achieved_fps": a["achieved_fps"],
                "before_cadence_ratio": b["cadence_ratio"],
                "after_cadence_ratio": a["cadence_ratio"],
                "before_interval_p95_ms": b["interval_p95_ms"],
                "after_interval_p95_ms": a["interval_p95_ms"],
                "before_python_ms": b["python_ms_mean"],
                "after_python_ms": a["python_ms_mean"],
                "python_speedup": (
                    b["python_ms_mean"] / a["python_ms_mean"]
                    if a["python_ms_mean"] > 0
                    else 0.0
                ),
            }
        )
    return rows


def _print_table(rows: list[dict]) -> None:
    print()
    print("## Before / After Transport Clock")
    print()
    hdr = (
        f"{'sequence':<14} {'fps':>6} "
        f"{'ach B':>7} {'ach A':>7} "
        f"{'ratio B':>7} {'ratio A':>7} "
        f"{'p95 B':>7} {'p95 A':>7} "
        f"{'py ms B':>8} {'py ms A':>8} {'py x':>6}"
    )
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        print(
            f"{r['sequence']:<14} {r['target_fps']:6.2f} "
            f"{r['before_achieved_fps']:7.2f} {r['after_achieved_fps']:7.2f} "
            f"{r['before_cadence_ratio']:7.3f} {r['after_cadence_ratio']:7.3f} "
            f"{r['before_interval_p95_ms']:7.2f} {r['after_interval_p95_ms']:7.2f} "
            f"{r['before_python_ms']:8.3f} {r['after_python_ms']:8.3f} "
            f"{r['python_speedup']:6.2f}"
        )
    print()
    print(
        "before = Python QTimer (FRAMECYCLER_PYTHON_TRANSPORT=1); "
        "after = C++ transport clock. "
        "ratio = achieved/target fps. py x = before_ms/after_ms on per-frame Python work."
    )
    print()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--footage",
        type=Path,
        default=REPO_ROOT / ".temp" / "TestFootage",
    )
    parser.add_argument("--duration", type=float, default=3.0)
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
            "after uses C++ TransportClock. Measures achieved fps, interval p95, "
            "and Python per-frame seek/tick cost."
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
