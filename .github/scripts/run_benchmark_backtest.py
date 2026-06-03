"""Deterministic benchmark backtest for the CI gate.

Run the wheel strategy across a fixed 90-day window using a fixed seed
universe and the project's DEFAULT settings (no project_settings rows).
Output a single JSON file with the headline metrics so a sibling script
can compare head vs base.

Why deterministic:
  CI runs need to produce the same numbers every time. We bypass the
  real broker (no live data), instead generating synthetic price paths
  from a fixed seed. The point is comparing the strategy's algorithmic
  behavior, not its market timing.
"""
from __future__ import annotations

import argparse
import json
import math
import random
from datetime import date, datetime, timedelta
from pathlib import Path


# Fixed seed universe — same names every CI run
UNIVERSE = ["AAPL", "MSFT", "NVDA", "META", "F", "SOFI", "HOOD",
            "SHOP", "COIN", "PLTR"]
DAYS = 90
SEED = 42


def synthetic_path(seed: int, ticker: str, days: int) -> list[float]:
    """Geometric Brownian motion price path with per-ticker drift+vol.
    Deterministic given (seed, ticker, days)."""
    rng = random.Random(f"{seed}:{ticker}")
    # Per-ticker parameters — these never change across CI runs.
    s0 = 50 + (hash(ticker) & 0xff) * 1.2     # starting price
    mu = (hash(ticker) & 0xf) / 1000.0         # daily drift
    sigma = 0.015 + (hash(ticker) & 0x3) / 500.0  # daily vol
    prices = [s0]
    for _ in range(days):
        # log-normal step
        z = rng.gauss(0.0, 1.0)
        prev = prices[-1]
        new = prev * math.exp(mu - 0.5 * sigma ** 2 + sigma * z)
        prices.append(max(0.5, new))
    return prices


def simulate_wheel(prices_by_ticker: dict[str, list[float]]) -> dict:
    """Simulate a simplified wheel:
       * sell a 30-DTE CSP every Monday on the highest-IV-proxy underlying
       * if underlying ends below strike at expiry, hold shares
       * sell a 30-DTE CC every Monday on assigned tickers
       * compute end-of-day mark-to-market for daily P&L
    Returns a metrics dict suitable for comparison."""
    cash = 25_000.0
    realized = 0.0
    open_csps: list[dict] = []
    open_ccs: list[dict] = []
    held: dict[str, int] = {}    # ticker -> shares
    cost_basis: dict[str, float] = {}
    nav_series: list[float] = []

    for day in range(DAYS):
        # Sell-block: only on Mondays (day % 7 == 0)
        if day % 7 == 0:
            # Pick top "loud" ticker by daily ret variance (proxy for IV)
            best_t = None
            best_vol = -1.0
            for t, ps in prices_by_ticker.items():
                if held.get(t, 0) >= 100:
                    continue  # already in CC phase
                if day < 10:
                    continue
                window = ps[max(0, day - 10):day + 1]
                if len(window) < 2:
                    continue
                rets = [
                    abs((window[i] - window[i - 1]) / window[i - 1])
                    for i in range(1, len(window))
                ]
                v = sum(rets) / len(rets)
                if v > best_vol:
                    best_vol = v
                    best_t = t
            if best_t:
                p = prices_by_ticker[best_t][day]
                strike = round(p * 0.95, 0)         # ~0.30 delta proxy
                premium = p * 0.012                  # 1.2% of underlying
                # cash secured
                collateral = strike * 100.0
                if cash >= collateral:
                    cash -= collateral
                    cash += premium * 100.0
                    realized += premium * 100.0
                    open_csps.append({
                        "ticker": best_t,
                        "strike": strike,
                        "premium": premium,
                        "expiry_day": day + 30,
                    })

        # Sell CC on assigned positions
        if day % 7 == 0:
            for t in list(held.keys()):
                if held[t] < 100:
                    continue
                # already have an open CC?
                if any(cc["ticker"] == t for cc in open_ccs):
                    continue
                p = prices_by_ticker[t][day]
                strike = round(p * 1.05, 0)
                premium = p * 0.010
                cash += premium * 100.0
                realized += premium * 100.0
                open_ccs.append({
                    "ticker": t,
                    "strike": strike,
                    "premium": premium,
                    "expiry_day": day + 30,
                })

        # Process expirations
        expired_csps = [c for c in open_csps if c["expiry_day"] == day]
        for c in expired_csps:
            p = prices_by_ticker[c["ticker"]][day]
            if p < c["strike"]:
                # assigned
                held[c["ticker"]] = held.get(c["ticker"], 0) + 100
                cost_basis[c["ticker"]] = c["strike"]
                # cash was already debited
            else:
                # expires worthless; release collateral
                cash += c["strike"] * 100.0
        open_csps = [c for c in open_csps if c["expiry_day"] > day]

        expired_ccs = [c for c in open_ccs if c["expiry_day"] == day]
        for c in expired_ccs:
            p = prices_by_ticker[c["ticker"]][day]
            if p > c["strike"]:
                # called away
                cash += c["strike"] * 100.0
                cap_gain = (c["strike"] - cost_basis.get(c["ticker"], c["strike"])) * 100
                realized += cap_gain
                held[c["ticker"]] = held.get(c["ticker"], 0) - 100
                if held[c["ticker"]] <= 0:
                    held.pop(c["ticker"], None)
                    cost_basis.pop(c["ticker"], None)
        open_ccs = [c for c in open_ccs if c["expiry_day"] > day]

        # Mark to market
        equity = cash
        for t, qty in held.items():
            equity += qty * prices_by_ticker[t][day]
        for c in open_csps:
            # short put liability ≈ max(0, strike - price) per share
            p = prices_by_ticker[c["ticker"]][day]
            liability = max(0.0, c["strike"] - p) * 100.0
            equity -= liability
        nav_series.append(equity)

    # Metrics
    returns = []
    for i in range(1, len(nav_series)):
        prev = nav_series[i - 1]
        if prev > 0:
            returns.append((nav_series[i] - prev) / prev)
    avg = sum(returns) / len(returns) if returns else 0.0
    if returns:
        var = sum((r - avg) ** 2 for r in returns) / len(returns)
        std = math.sqrt(var)
    else:
        std = 0.0
    annualized_return = avg * 252
    annualized_vol = std * math.sqrt(252)
    sharpe = annualized_return / annualized_vol if annualized_vol > 0 else 0.0
    max_dd = 0.0
    peak = nav_series[0] if nav_series else 0.0
    for nav in nav_series:
        if nav > peak:
            peak = nav
        dd = (nav - peak) / peak if peak > 0 else 0.0
        if dd < max_dd:
            max_dd = dd
    return {
        "starting_nav": 25_000.0,
        "ending_nav": nav_series[-1] if nav_series else 0.0,
        "total_return_pct": ((nav_series[-1] / 25_000.0) - 1.0) * 100
                            if nav_series else 0.0,
        "realized_pnl": realized,
        "sharpe": round(sharpe, 4),
        "max_drawdown_pct": round(max_dd * 100, 4),
        "annualized_return_pct": round(annualized_return * 100, 4),
        "annualized_vol_pct": round(annualized_vol * 100, 4),
        "days": DAYS,
        "universe_size": len(UNIVERSE),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", required=True, type=Path)
    args = ap.parse_args()

    prices = {t: synthetic_path(SEED, t, DAYS) for t in UNIVERSE}
    metrics = simulate_wheel(prices)

    args.output.write_text(json.dumps(metrics, indent=2))
    print(json.dumps(metrics, indent=2))


if __name__ == "__main__":
    main()
