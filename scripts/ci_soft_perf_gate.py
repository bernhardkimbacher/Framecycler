#!/usr/bin/env python3
"""Soft perf gate: compare benchmark JSON against packaging/perf_baselines.json.

Exit 0 always for CI (warnings only) unless --strict. With --strict, exit 1 when
any metric exceeds baseline * threshold (default 2.0).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
DEFAULT_BASELINES = REPO / "packaging" / "perf_baselines.json"


def _load(path: Path) -> dict:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--baselines",
        type=Path,
        default=DEFAULT_BASELINES,
        help="Checked-in baseline JSON",
    )
    parser.add_argument(
        "--results-dir",
        type=Path,
        default=REPO / "artifacts" / "perf",
        help="Directory of measured *.json results",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=2.0,
        help="Fail/warn when measured > baseline * threshold",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit 1 on regression (default: warn only)",
    )
    args = parser.parse_args()

    if not args.baselines.is_file():
        print(f"No baselines at {args.baselines}; skipping")
        return 0
    if not args.results_dir.is_dir():
        print(f"No results dir {args.results_dir}; skipping")
        return 0

    baselines = _load(args.baselines)
    regressions: list[str] = []
    for path in sorted(args.results_dir.glob("*.json")):
        data = _load(path)
        name = data.get("name") or path.stem
        base = baselines.get(name)
        if not base:
            print(f"No baseline for {name}; skipping")
            continue
        for key, base_val in base.items():
            if key not in data:
                continue
            try:
                measured = float(data[key])
                expected = float(base_val)
            except (TypeError, ValueError):
                continue
            limit = expected * args.threshold
            if measured > limit:
                msg = (
                    f"{name}.{key}: {measured:.3f} > {limit:.3f} "
                    f"(baseline {expected:.3f} × {args.threshold})"
                )
                regressions.append(msg)
                print(f"REGRESSION: {msg}")
            else:
                print(f"OK {name}.{key}: {measured:.3f} (limit {limit:.3f})")

    if regressions:
        print(f"{len(regressions)} soft perf regression(s)", file=sys.stderr)
        return 1 if args.strict else 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
