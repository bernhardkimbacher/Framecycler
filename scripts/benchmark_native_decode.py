#!/usr/bin/env python3
"""Microbenchmark for native OIIO stills decode → CacheManager.

Records mean per-frame decode time at full and half resolution.
Usage:
  python scripts/benchmark_native_decode.py [path/to/file.exr] [--frames N]
"""
from __future__ import annotations

import argparse
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from src.framecycler import framecycler_engine
from tests.oiio_fixtures import write_float_exr


def bench(path: str, scale: float, frames: int) -> float:
    cache = framecycler_engine.CacheManager(2.0)
    # Warm once (plugin load / thread pool).
    cache.decode_and_cache_frame(0, path, scale)
    cache.clear()

    times = []
    for i in range(frames):
        cache.clear()
        t0 = time.perf_counter()
        ok = cache.decode_and_cache_frame(i + 1, path, scale)
        t1 = time.perf_counter()
        if not ok:
            raise RuntimeError(f"decode failed for {path} scale={scale}")
        times.append((t1 - t0) * 1000.0)
    return statistics.mean(times)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("path", nargs="?", default="")
    parser.add_argument("--frames", type=int, default=8)
    args = parser.parse_args()

    tmp = None
    path = args.path
    if not path:
        tmp = tempfile.TemporaryDirectory(prefix="fc_bench_decode_")
        path = str(Path(tmp.name) / "bench.exr")
        write_float_exr(Path(path), width=1920, height=1080, value=0.4)
        print(f"Using synthetic 1920x1080 EXR at {path}")

    framecycler_engine.set_decode_threads(max(1, (os.cpu_count() or 4)))

    mean_full = bench(path, 1.0, args.frames)
    mean_half = bench(path, 0.5, args.frames)
    print(f"mean decode ms @1.0: {mean_full:.3f}  ({args.frames} frames)")
    print(f"mean decode ms @0.5: {mean_half:.3f}  ({args.frames} frames)")

    if tmp is not None:
        tmp.cleanup()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
