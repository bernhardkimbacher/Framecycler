#!/usr/bin/env python3
"""A/B microbench for zero-copy hot path (pin+map upload, adaptive budget, HW import).

Usage:
  python scripts/benchmark_zero_copy_hotpath.py [--frames N] [--json-out PATH]
      [--path {cpu_upload,hw_import}] [footage_dir_or_file]

Historical A/B used FRAMECYCLER_ZC_* kill-switches (now removed; map/pool/adaptive/direct
are always on). Re-runs compare a single hosted variant or use --no-ab.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(REPO, "src"))


def _p95(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[max(0, int(round(0.95 * (len(ordered) - 1))))]


def _mean(values: list[float]) -> float:
    return statistics.fmean(values) if values else 0.0


def _find_default_footage() -> str:
    for candidate in (
        os.path.join(REPO, ".temp", "TestFootage", "KPO_012_0140"),
        os.path.join(REPO, ".temp", "testFootage", "KPO_012_0140"),
        os.path.join(REPO, "tests", "fixtures", "tiny_movie.mp4"),
    ):
        if os.path.isdir(candidate) or os.path.isfile(candidate):
            return candidate
    return os.path.join(REPO, "tests", "fixtures", "tiny_movie.mp4")


def _sequence_paths(root: str, limit: int) -> list[str]:
    if os.path.isfile(root):
        return [root]
    paths = sorted(
        str(p)
        for p in Path(root).iterdir()
        if p.suffix.lower() in {".exr", ".dpx", ".tif", ".tiff", ".png", ".jpg", ".jpeg"}
    )
    return paths[: max(1, limit)]


def run_hosted_bench(
    footage: str,
    frames: int,
    path_mode: str,
    display_cache_gb: float,
) -> dict:
    from PySide6.QtWidgets import QApplication
    from PySide6.QtCore import QTimer

    from framecycler.ui.main_window import MainWindow

    app = QApplication.instance() or QApplication(sys.argv)
    window = MainWindow()
    window.resize(1280, 720)
    window.show()
    app.processEvents()
    time.sleep(0.2)

    renderer = window.viewport.native_renderer
    if hasattr(renderer, "set_exposed"):
        renderer.set_exposed(True)
    if hasattr(renderer, "set_display_cache_limit_gb"):
        renderer.set_display_cache_limit_gb(display_cache_gb)
    if hasattr(window.viewport, "native_renderer"):
        try:
            renderer.sync_and_render()
        except Exception:
            pass

    media_paths: list[str]
    if path_mode == "hw_import" and footage.lower().endswith((".mp4", ".mov", ".mxf", ".mkv")):
        media_paths = [footage]
    elif path_mode == "hw_import":
        movie = os.path.join(REPO, ".temp", "TestFootage", "Film Leader Countdown.mp4")
        if not os.path.isfile(movie):
            movie = os.path.join(REPO, "tests", "fixtures", "tiny_movie.mp4")
        media_paths = [movie]
    else:
        media_paths = _sequence_paths(footage, frames)
        if len(media_paths) == 1 and media_paths[0].lower().endswith(
            (".mp4", ".mov", ".mxf", ".mkv")
        ):
            # Still OK — movie CPU/HW path depending on decoder.
            pass
        elif not media_paths:
            raise SystemExit(f"no frames under {footage}")

    window._add_media(media_paths)
    app.processEvents()
    if not window.sources:
        raise SystemExit("no sources loaded")

    src = window.sources[0]
    n = min(frames, max(1, int(src.frame_count)))
    start = int(getattr(src, "decoder_start_frame", 0) or 0)

    # Wait for CPU cache warm on first few frames.
    deadline = time.time() + 30.0
    warmed = 0
    while time.time() < deadline and warmed < min(4, n):
        app.processEvents()
        for i in range(n):
            fi = start + i
            if src.cache.native_cache.has_frame(fi):
                warmed += 1
        if warmed >= min(4, n):
            break
        time.sleep(0.05)
        warmed = sum(
            1 for i in range(n) if src.cache.native_cache.has_frame(start + i)
        )

    def pump(ms: float = 0.05) -> None:
        t_end = time.time() + ms
        while time.time() < t_end:
            app.processEvents()
            try:
                renderer.sync_and_render()
            except Exception:
                pass
            time.sleep(0.005)

    # Ensure viewport has pushed at least one RenderParams after load.
    window.seek_to_frame(0, force_heavy=True)
    pump(0.5)

    # --- Cold isolated: clear display cache between seeks ---
    isolated_upload: list[float] = []
    isolated_bytes: list[float] = []
    isolated_staging = 0
    for i in range(n):
        renderer.clear_display_cache()
        pump(0.15)
        before = renderer.get_debug_stats()
        window.seek_to_frame(i, force_heavy=True)
        pump(0.35)
        after = renderer.get_debug_stats()
        isolated_upload.append(float(after.get("last_upload_ms", 0.0)))
        isolated_bytes.append(float(after.get("last_upload_bytes", 0.0)))
        isolated_staging += int(after.get("staging_waits", 0)) - int(
            before.get("staging_waits", 0)
        )

    # --- Warm scrub: hits only ---
    warm_upload: list[float] = []
    warm_bytes: list[float] = []
    for i in range(n):
        window.seek_to_frame(i, force_heavy=False)
        pump(0.12)
        st = renderer.get_debug_stats()
        warm_upload.append(float(st.get("last_upload_ms", 0.0)))
        warm_bytes.append(float(st.get("last_upload_bytes", 0.0)))

    final = renderer.get_debug_stats()
    result = {
        "n_frames": n,
        "display_cache_gb": display_cache_gb,
        "path": path_mode,
        "footage": footage,
        "isolated_mean_upload_ms": _mean(isolated_upload),
        "isolated_p95_upload_ms": _p95(isolated_upload),
        "isolated_mean_upload_bytes": _mean(isolated_bytes),
        "isolated_staging_waits_delta": isolated_staging,
        "warm_mean_upload_ms": _mean(warm_upload),
        "warm_mean_upload_bytes": _mean(warm_bytes),
        "end_frame_ms_max": float(final.get("end_frame_ms_max", 0.0)),
        "staging_waits": int(final.get("staging_waits", 0)),
        "zc_map_uploads": int(final.get("zc_map_uploads", 0)),
        "zc_copy_uploads": int(final.get("zc_copy_uploads", 0)),
        "hw_import_creates": int(final.get("hw_import_creates", 0)),
        "hw_import_reuses": int(final.get("hw_import_reuses", 0)),
        "frames_drawn": int(final.get("frames_drawn", 0)),
        "env": {
            "FRAMECYCLER_ZC_MAP": os.environ.get("FRAMECYCLER_ZC_MAP", "1"),
            "FRAMECYCLER_ZC_D3D_POOL": os.environ.get("FRAMECYCLER_ZC_D3D_POOL", "1"),
            "FRAMECYCLER_ZC_ADAPTIVE_BUDGET": os.environ.get(
                "FRAMECYCLER_ZC_ADAPTIVE_BUDGET", "1"
            ),
            "FRAMECYCLER_ZC_DIRECT_YUV": os.environ.get("FRAMECYCLER_ZC_DIRECT_YUV", "1"),
        },
    }

    QTimer.singleShot(0, app.quit)
    app.processEvents()
    window.close()
    return result


def _run_variant_subprocess(args: argparse.Namespace, label: str, env_overrides: dict) -> dict:
    env = os.environ.copy()
    env.update(env_overrides)
    cmd = [
        sys.executable,
        __file__,
        "--worker",
        "--frames",
        str(args.frames),
        "--path",
        args.path,
        "--display-cache-gb",
        str(args.display_cache_gb),
        args.footage,
    ]
    proc = subprocess.run(
        cmd,
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        print(proc.stdout, file=sys.stderr)
        print(proc.stderr, file=sys.stderr)
        raise SystemExit(f"variant {label} failed: rc={proc.returncode}")
    # Worker prints a single JSON object on the last non-empty stdout line.
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    if not lines:
        raise SystemExit(f"variant {label}: empty stdout")
    payload = json.loads(lines[-1])
    payload["label"] = label
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "footage",
        nargs="?",
        default=_find_default_footage(),
        help="Sequence dir, EXR, or movie path",
    )
    parser.add_argument("--frames", type=int, default=12)
    parser.add_argument(
        "--path",
        choices=("cpu_upload", "hw_import"),
        default="cpu_upload",
    )
    parser.add_argument("--display-cache-gb", type=float, default=4.0)
    parser.add_argument(
        "--json-out",
        default=os.environ.get("PERF_JSON_OUT", ""),
        help="Write full A/B JSON (or set PERF_JSON_OUT)",
    )
    parser.add_argument(
        "--gate-summary",
        action="store_true",
        help="Also emit gate-shaped zero_copy_hotpath summary fields",
    )
    parser.add_argument(
        "--worker",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--no-ab",
        action="store_true",
        help="Run a single variant with current env (no before/after)",
    )
    args = parser.parse_args()

    if args.worker or args.no_ab:
        result = run_hosted_bench(
            args.footage, args.frames, args.path, args.display_cache_gb
        )
        print(json.dumps(result))
        return 0

    before = _run_variant_subprocess(
        args,
        "before_copy_path",
        {
            "FRAMECYCLER_ZC_MAP": "0",
            "FRAMECYCLER_ZC_D3D_POOL": "0",
            "FRAMECYCLER_ZC_ADAPTIVE_BUDGET": "0",
            "FRAMECYCLER_ZC_DIRECT_YUV": "0",
        },
    )
    after = _run_variant_subprocess(
        args,
        "after_zero_copy",
        {
            "FRAMECYCLER_ZC_MAP": "1",
            "FRAMECYCLER_ZC_D3D_POOL": "1",
            "FRAMECYCLER_ZC_ADAPTIVE_BUDGET": "1",
            "FRAMECYCLER_ZC_DIRECT_YUV": "1",
        },
    )

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    out_path = args.json_out or os.path.join(
        REPO, "benchmarks", f"{stamp}_zero-copy-hotpath_ab.json"
    )
    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "description": (
            "Zero-copy hot path A/B: CPU pin+map upload vs copy_frame_data; "
            "D3D import pool; adaptive upload budget; Metal direct YUV/BGRA when eligible."
        ),
        "methodology": (
            "MainWindow hosted QWindow (not Null). Load sequence/movie; wait CPU cache warm; "
            "(1) isolated cold uploads with clear_display_cache between seeks; "
            "(2) warm scrub (display hits). Original A/B used FRAMECYCLER_ZC_* kill-switches "
            "(removed after validation; pin+map / pool / adaptive / direct are default)."
        ),
        "footage": args.footage,
        "display_cache_gb": args.display_cache_gb,
        "path": args.path,
        "before": before,
        "after": after,
    }
    if args.gate_summary:
        payload["gate"] = {
            "name": "zero_copy_hotpath",
            "mean_upload_ms": after["isolated_mean_upload_ms"],
            "p95_upload_ms": after["isolated_p95_upload_ms"],
            "mean_upload_bytes": after["isolated_mean_upload_bytes"],
            "zc_map_uploads": after["zc_map_uploads"],
            "hw_import_reuses": after["hw_import_reuses"],
        }

    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    Path(out_path).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(
        f"before isolated mean={before['isolated_mean_upload_ms']:.3f} ms  "
        f"after={after['isolated_mean_upload_ms']:.3f} ms  "
        f"map_uploads={after['zc_map_uploads']} copy_uploads={after['zc_copy_uploads']}"
    )
    print(f"wrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
