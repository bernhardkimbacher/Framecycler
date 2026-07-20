#!/usr/bin/env python3
"""Microbench movie decode (includes sws + float16 convert).

Usage:
  python scripts/benchmark_movie_convert.py [path] [--frames N]
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from pathlib import Path

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(REPO, "src"))

from framecycler import framecycler_engine  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "path",
        nargs="?",
        default=os.path.join(REPO, "tests", "fixtures", "tiny_movie.mp4"),
        help="Movie path (default: tests/fixtures/tiny_movie.mp4)",
    )
    parser.add_argument("--frames", type=int, default=60, help="Frames to decode")
    parser.add_argument(
        "--json-out",
        default=os.environ.get("PERF_JSON_OUT", ""),
        help="Write metrics JSON (or set PERF_JSON_OUT)",
    )
    args = parser.parse_args()

    if not os.path.isfile(args.path):
        print(f"missing movie: {args.path}", file=sys.stderr)
        return 1

    backend = framecycler_engine.half_convert_backend()
    ok = framecycler_engine.half_convert_self_test()
    print(f"half_convert backend={backend} self_test={'ok' if ok else 'FAIL'}")
    if not ok:
        return 2

    dec = framecycler_engine.NativeMovieDecoder()
    if not dec.open(args.path):
        print("open failed", file=sys.stderr)
        return 1

    start = int(dec.start_frame)
    count = min(int(args.frames), int(dec.frame_count))
    width, height = int(dec.width), int(dec.height)
    hw = str(dec.hw_type)
    times_ms: list[float] = []
    # Warmup
    dec.decode_frame(start, 1.0)
    for i in range(count):
        t0 = time.perf_counter()
        frame = dec.decode_frame(start + i, 1.0)
        t1 = time.perf_counter()
        if frame is None:
            print(f"decode failed at {start + i}", file=sys.stderr)
            dec.close()
            return 1
        times_ms.append((t1 - t0) * 1000.0)
    dec.close()

    mean = statistics.fmean(times_ms)
    p95 = sorted(times_ms)[max(0, int(round(0.95 * (len(times_ms) - 1))))]
    print(
        f"decoded {count} frames from {os.path.basename(args.path)} "
        f"({width}x{height} hw={hw})"
    )
    print(f"mean={mean:.3f} ms/frame  p95={p95:.3f} ms  min={min(times_ms):.3f} max={max(times_ms):.3f}")
    if args.json_out:
        out = Path(args.json_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "name": "movie_convert",
            "mean_ms": mean,
            "p95_ms": p95,
            "min_ms": min(times_ms),
            "max_ms": max(times_ms),
            "frames": count,
            "width": width,
            "height": height,
            "hw": hw,
        }
        out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
