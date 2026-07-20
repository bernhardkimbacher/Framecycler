#!/usr/bin/env python3
"""Microbench EXR full-res vs proxy decode (tile/scanline band path).

Usage:
  python scripts/benchmark_exr_proxy.py [--frames N] [--size WxH] [--tiled]
"""

from __future__ import annotations

import argparse
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(REPO, "src"))
sys.path.insert(0, REPO)

from framecycler import framecycler_engine  # noqa: E402
from tests.oiio_fixtures import write_float_exr, write_tiled_float_exr  # noqa: E402


def _parse_size(text: str) -> tuple[int, int]:
    w_s, h_s = text.lower().split("x", 1)
    return int(w_s), int(h_s)


def _bench(cache, path: str, frame_base: int, scale: float, repeats: int) -> list[float]:
    times_ms: list[float] = []
    # Warmup (unique frame ids so cache hits do not mask decode cost)
    cache.decode_and_cache_frame(frame_base, path, scale)
    for i in range(repeats):
        fid = frame_base + 1 + i
        t0 = time.perf_counter()
        ok = cache.decode_and_cache_frame(fid, path, scale)
        t1 = time.perf_counter()
        if not ok:
            raise RuntimeError(f"decode failed scale={scale} frame={fid}")
        times_ms.append((t1 - t0) * 1000.0)
    return times_ms


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frames", type=int, default=20, help="Repeats per scale")
    parser.add_argument("--size", default="2048x1556", help="WxH (default 2K-ish)")
    parser.add_argument(
        "--tiled",
        action="store_true",
        default=True,
        help="Write tiled OpenEXR (default)",
    )
    parser.add_argument(
        "--scanline",
        action="store_true",
        help="Force scanline EXR instead of tiled",
    )
    args = parser.parse_args()
    width, height = _parse_size(args.size)
    use_tiled = bool(args.tiled) and not bool(args.scanline)

    with tempfile.TemporaryDirectory(prefix="fc_exr_proxy_bench_") as tmp:
        path = Path(tmp) / ("plate_tiled.exr" if use_tiled else "plate_scan.exr")
        if use_tiled:
            write_tiled_float_exr(
                path, width=width, height=height, value=0.5, tile_size=64
            )
        else:
            write_float_exr(path, width=width, height=height, value=0.5)

        cache = framecycler_engine.CacheManager(2.0)
        full = _bench(cache, str(path), 1000, 1.0, args.frames)
        proxy = _bench(cache, str(path), 2000, 0.5, args.frames)

        def fmt(times: list[float]) -> str:
            mean = statistics.fmean(times)
            p95 = sorted(times)[max(0, int(round(0.95 * (len(times) - 1))))]
            return f"mean={mean:.3f} ms  p95={p95:.3f}  min={min(times):.3f} max={max(times):.3f}"

        kind = "tiled" if use_tiled else "scanline"
        print(f"EXR proxy bench {width}x{height} {kind} repeats={args.frames}")
        print(f"  scale=1.0  {fmt(full)}")
        print(f"  scale=0.5  {fmt(proxy)}")
        ratio = statistics.fmean(proxy) / max(1e-9, statistics.fmean(full))
        print(f"  proxy/full ratio={ratio:.3f} (lower is better for proxy)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
