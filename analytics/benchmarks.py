"""Benchmark comparison and alpha/beta calculation.

Compares portfolio performance against market benchmarks (SPY, QQQ, etc.).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import numpy as np
from sqlalchemy import text

from db.connection import session_scope
from db.repositories import ProjectsRepo
from execution import AlpacaClient

logger = logging.getLogger(__name__)

# Default benchmarks
DEFAULT_BENCHMARKS = ["SPY", "QQQ", "IWM", "DIA"]


def get_benchmark_returns(
    ticker: str,
    project_id: str,
    days: int = 90
) -> dict[str, Any]:
    """Get historical returns for a benchmark.

    Args:
        ticker: Benchmark ticker (e.g., SPY)
        project_id: Trading project ID (for broker access)
        days: Lookback period

    Returns:
        Dict with returns data
    """
    project = ProjectsRepo.get(project_id)
    if project is None:
        return {"error": "Project not found"}

    try:
        import yfinance as yf
        end = datetime.now(tz=timezone.utc)
        start = end - timedelta(days=days)

        bench = yf.Ticker(ticker)
        hist = bench.history(start=start, end=end)

        if hist.empty:
            return {"error": f"No data for {ticker}"}

        prices = hist["Close"].values
        returns = np.diff(prices) / prices[:-1]

        total_return = (prices[-1] - prices[0]) / prices[0]
        annualized = ((1 + total_return) ** (365 / days)) - 1

        return {
            "ticker": ticker,
            "start_price": float(prices[0]),
            "end_price": float(prices[-1]),
            "total_return_pct": round(total_return * 100, 2),
            "annualized_return_pct": round(annualized * 100, 2),
            "daily_returns": returns.tolist(),
            "volatility": round(float(np.std(returns) * np.sqrt(252)) * 100, 2),
            "days": days,
        }

    except Exception as e:
        logger.warning("Failed to get benchmark data for %s: %s", ticker, e)
        return {"error": str(e)}


def get_portfolio_returns(project_id: str, days: int = 90) -> dict[str, Any]:
    """Get historical portfolio returns from snapshots.

    Args:
        project_id: Trading project ID
        days: Lookback period

    Returns:
        Dict with returns data
    """
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=days)

    with session_scope() as s:
        rows = s.execute(text("""
            SELECT snapshot_at, equity
            FROM portfolio_snapshots
            WHERE project_id = :p AND snapshot_at >= :cutoff
            ORDER BY snapshot_at
        """), {"p": project_id, "cutoff": cutoff}).fetchall()

    if len(rows) < 2:
        return {"error": "Insufficient snapshot data"}

    timestamps = [r[0] for r in rows]
    equities = [float(r[1]) for r in rows]

    returns = []
    for i in range(1, len(equities)):
        if equities[i - 1] > 0:
            returns.append((equities[i] - equities[i - 1]) / equities[i - 1])
        else:
            returns.append(0)

    total_return = (equities[-1] - equities[0]) / equities[0] if equities[0] > 0 else 0
    actual_days = (timestamps[-1] - timestamps[0]).days or 1
    annualized = ((1 + total_return) ** (365 / actual_days)) - 1

    return {
        "start_equity": equities[0],
        "end_equity": equities[-1],
        "total_return_pct": round(total_return * 100, 2),
        "annualized_return_pct": round(annualized * 100, 2),
        "daily_returns": returns,
        "volatility": round(float(np.std(returns) * np.sqrt(252)) * 100, 2) if returns else 0,
        "days": actual_days,
        "snapshots": len(rows),
    }


def calculate_alpha_beta(
    project_id: str,
    benchmark: str = "SPY",
    days: int = 90
) -> dict[str, Any]:
    """Calculate alpha and beta against a benchmark.

    Alpha: Excess return above benchmark (risk-adjusted)
    Beta: Portfolio volatility relative to benchmark

    Args:
        project_id: Trading project ID
        benchmark: Benchmark ticker
        days: Lookback period

    Returns:
        Dict with alpha, beta, and related metrics
    """
    portfolio = get_portfolio_returns(project_id, days)
    if "error" in portfolio:
        return portfolio

    bench_data = get_benchmark_returns(benchmark, project_id, days)
    if "error" in bench_data:
        return bench_data

    port_returns = np.array(portfolio["daily_returns"])
    bench_returns = np.array(bench_data["daily_returns"])

    # Align lengths (use shorter)
    min_len = min(len(port_returns), len(bench_returns))
    port_returns = port_returns[-min_len:]
    bench_returns = bench_returns[-min_len:]

    if len(port_returns) < 5:
        return {"error": "Insufficient data for alpha/beta calculation"}

    # Calculate beta (covariance / variance)
    covariance = np.cov(port_returns, bench_returns)[0, 1]
    variance = np.var(bench_returns)

    beta = covariance / variance if variance > 0 else 1.0

    # Calculate alpha (Jensen's alpha)
    risk_free_rate = 0.05 / 252  # ~5% annual, daily
    port_mean = np.mean(port_returns)
    bench_mean = np.mean(bench_returns)

    alpha_daily = port_mean - (risk_free_rate + beta * (bench_mean - risk_free_rate))
    alpha_annual = alpha_daily * 252

    # Correlation
    correlation = np.corrcoef(port_returns, bench_returns)[0, 1]

    # Sharpe ratio
    excess_returns = port_returns - risk_free_rate
    sharpe = (np.mean(excess_returns) / np.std(excess_returns)) * np.sqrt(252) if np.std(excess_returns) > 0 else 0

    # Information ratio
    tracking_error = np.std(port_returns - bench_returns) * np.sqrt(252)
    info_ratio = ((portfolio["annualized_return_pct"] - bench_data["annualized_return_pct"]) / 100 / tracking_error
                  if tracking_error > 0 else 0)

    return {
        "benchmark": benchmark,
        "days": days,
        "alpha": round(alpha_annual * 100, 2),
        "beta": round(beta, 3),
        "correlation": round(correlation, 3),
        "sharpe_ratio": round(sharpe, 2),
        "information_ratio": round(info_ratio, 2),
        "tracking_error_pct": round(tracking_error * 100, 2),
        "portfolio": {
            "return_pct": portfolio["total_return_pct"],
            "annualized_pct": portfolio["annualized_return_pct"],
            "volatility_pct": portfolio["volatility"],
        },
        "benchmark_data": {
            "return_pct": bench_data["total_return_pct"],
            "annualized_pct": bench_data["annualized_return_pct"],
            "volatility_pct": bench_data["volatility"],
        },
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
    }


def calculate_benchmark_comparison(
    project_id: str,
    benchmark: str = "SPY",
    days: int = 90
) -> dict[str, Any]:
    """Compare portfolio performance vs benchmark.

    Args:
        project_id: Trading project ID
        benchmark: Benchmark ticker
        days: Lookback period

    Returns:
        Comprehensive comparison dict
    """
    alpha_beta = calculate_alpha_beta(project_id, benchmark, days)

    if "error" in alpha_beta:
        return alpha_beta

    port = alpha_beta["portfolio"]
    bench = alpha_beta["benchmark_data"]

    outperformance = port["return_pct"] - bench["return_pct"]

    return {
        "project_id": project_id,
        "benchmark": benchmark,
        "period_days": days,
        "portfolio_return_pct": port["return_pct"],
        "benchmark_return_pct": bench["return_pct"],
        "outperformance_pct": round(outperformance, 2),
        "alpha_pct": alpha_beta["alpha"],
        "beta": alpha_beta["beta"],
        "sharpe_ratio": alpha_beta["sharpe_ratio"],
        "information_ratio": alpha_beta["information_ratio"],
        "correlation": alpha_beta["correlation"],
        "risk_adjusted_better": alpha_beta["alpha"] > 0,
        "summary": _generate_summary(alpha_beta, outperformance),
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
    }


def _generate_summary(alpha_beta: dict[str, Any], outperformance: float) -> str:
    """Generate human-readable performance summary."""
    parts = []

    if outperformance > 0:
        parts.append(f"Portfolio outperformed {alpha_beta['benchmark']} by {outperformance:.1f}%")
    else:
        parts.append(f"Portfolio underperformed {alpha_beta['benchmark']} by {abs(outperformance):.1f}%")

    if alpha_beta["alpha"] > 0:
        parts.append(f"with positive alpha of {alpha_beta['alpha']:.1f}%")
    else:
        parts.append(f"with negative alpha of {alpha_beta['alpha']:.1f}%")

    if alpha_beta["beta"] > 1.1:
        parts.append("(higher volatility than market)")
    elif alpha_beta["beta"] < 0.9:
        parts.append("(lower volatility than market)")

    return " ".join(parts)


def multi_benchmark_comparison(
    project_id: str,
    benchmarks: list[str] | None = None,
    days: int = 90
) -> dict[str, Any]:
    """Compare portfolio against multiple benchmarks.

    Args:
        project_id: Trading project ID
        benchmarks: List of benchmark tickers (defaults to SPY, QQQ, IWM, DIA)
        days: Lookback period

    Returns:
        Dict with comparison vs each benchmark
    """
    if benchmarks is None:
        benchmarks = DEFAULT_BENCHMARKS

    results = []
    for bench in benchmarks:
        comparison = calculate_benchmark_comparison(project_id, bench, days)
        if "error" not in comparison:
            results.append(comparison)

    # Find best and worst
    if results:
        best = max(results, key=lambda x: x["outperformance_pct"])
        worst = min(results, key=lambda x: x["outperformance_pct"])
    else:
        best = worst = None

    return {
        "project_id": project_id,
        "period_days": days,
        "comparisons": results,
        "best_vs": best["benchmark"] if best else None,
        "worst_vs": worst["benchmark"] if worst else None,
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
    }
