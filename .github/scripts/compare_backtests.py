"""Compare two benchmark backtest JSONs (base vs head) and fail loudly
if the head Sharpe regresses beyond the configured tolerance."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def fmt_delta(base: float, head: float) -> str:
    diff = head - base
    sign = "+" if diff >= 0 else ""
    return f"{sign}{diff:.4f}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True, type=Path)
    ap.add_argument("--head", required=True, type=Path)
    ap.add_argument(
        "--max-sharpe-regression", type=float, default=0.15,
        help="Maximum tolerated drop in Sharpe (absolute). Default 0.15.",
    )
    args = ap.parse_args()

    base = json.loads(args.base.read_text())
    head = json.loads(args.head.read_text())

    print("== Backtest comparison ==")
    print(f"{'metric':<28}{'base':>14}{'head':>14}{'delta':>14}")
    for k in ("sharpe", "total_return_pct", "max_drawdown_pct",
              "annualized_return_pct", "annualized_vol_pct"):
        b = float(base.get(k, 0))
        h = float(head.get(k, 0))
        print(f"{k:<28}{b:>14.4f}{h:>14.4f}{fmt_delta(b, h):>14}")

    base_s = float(base.get("sharpe", 0))
    head_s = float(head.get("sharpe", 0))
    delta = head_s - base_s

    print()
    if delta < -args.max_sharpe_regression:
        print(f"FAIL: Sharpe regressed by {delta:.4f} (tolerance "
              f"-{args.max_sharpe_regression}). Investigate before merging.")
        sys.exit(1)
    print(f"OK: Sharpe delta {delta:+.4f} within tolerance.")


if __name__ == "__main__":
    main()
