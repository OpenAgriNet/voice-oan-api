#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def _load_results(path: str) -> dict[str, dict[str, Any]]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    rows = payload.get("results", payload)
    if not isinstance(rows, list):
        raise ValueError(f"Invalid benchmark format in {path}")
    return {row["scenario"]: row for row in rows if isinstance(row, dict) and "scenario" in row}


def _fmt(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:.1f}"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Compare two TTFT benchmark JSON outputs and report regressions."
    )
    parser.add_argument("--before", required=True, help="Path to baseline benchmark JSON.")
    parser.add_argument("--after", required=True, help="Path to candidate benchmark JSON.")
    parser.add_argument(
        "--max-p50-regression-ms",
        type=float,
        default=20.0,
        help="Fail if any scenario TTFT p50 regresses more than this many ms.",
    )
    parser.add_argument(
        "--max-p50-regression-pct",
        type=float,
        default=5.0,
        help="Fail if any scenario TTFT p50 regresses more than this percentage.",
    )
    args = parser.parse_args()

    before = _load_results(args.before)
    after = _load_results(args.after)
    shared = sorted(set(before.keys()) & set(after.keys()))
    if not shared:
        raise ValueError("No overlapping scenarios between before and after files.")

    print("TTFT comparison")
    print("===============")
    print(f"before: {args.before}")
    print(f"after:  {args.after}")
    print("")
    print("scenario | before_p50 | after_p50 | delta_ms | delta_pct | verdict")

    failures = 0
    for scenario in shared:
        before_p50 = before[scenario].get("ttft_ms", {}).get("p50")
        after_p50 = after[scenario].get("ttft_ms", {}).get("p50")
        if before_p50 is None or after_p50 is None:
            print(f"{scenario} | {_fmt(before_p50)} | {_fmt(after_p50)} | n/a | n/a | SKIP")
            continue

        delta_ms = float(after_p50) - float(before_p50)
        delta_pct = (delta_ms / float(before_p50) * 100.0) if float(before_p50) > 0 else 0.0
        regressed = delta_ms > args.max_p50_regression_ms or delta_pct > args.max_p50_regression_pct
        verdict = "FAIL" if regressed else "PASS"
        if regressed:
            failures += 1
        print(
            f"{scenario} | {_fmt(before_p50)} | {_fmt(after_p50)} | "
            f"{delta_ms:+.1f} | {delta_pct:+.1f}% | {verdict}"
        )

    print("")
    if failures:
        print(
            f"Regression gate failed: {failures} scenario(s) exceeded "
            f"{args.max_p50_regression_ms:.1f}ms or {args.max_p50_regression_pct:.1f}% p50 TTFT regression."
        )
        return 1

    print("Regression gate passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
