"""Monte Carlo Value at Risk (VaR) Simulation.

Estimates potential portfolio losses using Monte Carlo simulation.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import numpy as np

from db.analytics_repos import PortfolioSnapshotsRepo
from db.repositories import ProjectsRepo
from execution import AlpacaClient

logger = logging.getLogger(__name__)

# Default simulation parameters
DEFAULT_SIMULATIONS = 10000
DEFAULT_HORIZON_DAYS = 1
DEFAULT_CONFIDENCE = 0.95


def monte_carlo_var(
    project_id: str,
    confidence: float = DEFAULT_CONFIDENCE,
    simulations: int = DEFAULT_SIMULATIONS,
    horizon_days: int = DEFAULT_HORIZON_DAYS,
    lookback_days: int = 252
) -> dict[str, Any]:
    """Calculate Value at Risk using Monte Carlo simulation.

    Simulates portfolio returns based on historical volatility and
    estimates potential loss at specified confidence level.

    Args:
        project_id: Trading project ID
        confidence: Confidence level (e.g., 0.95 for 95% VaR)
        simulations: Number of Monte Carlo simulations
        horizon_days: Time horizon in days
        lookback_days: Historical data for volatility estimation

    Returns:
        Dict with VaR estimates and simulation results
    """
    # Get historical equity curve
    since = datetime.now(tz=timezone.utc) - timedelta(days=lookback_days)
    curve = PortfolioSnapshotsRepo.curve(project_id, since=since)

    if len(curve) < 20:
        return {"error": "Insufficient historical data", "data_points": len(curve)}

    equities = np.array([pt["equity"] for pt in curve])
    current_equity = equities[-1]

    # Calculate historical daily returns
    returns = np.diff(equities) / equities[:-1]
    returns = returns[~np.isnan(returns)]

    if len(returns) < 10:
        return {"error": "Insufficient return data", "data_points": len(returns)}

    # Estimate return distribution parameters
    mu = float(np.mean(returns))
    sigma = float(np.std(returns))

    # Monte Carlo simulation
    np.random.seed(42)  # For reproducibility

    # Simulate multi-day returns if horizon > 1
    if horizon_days > 1:
        # Scale parameters for multi-day horizon
        mu_horizon = mu * horizon_days
        sigma_horizon = sigma * np.sqrt(horizon_days)
    else:
        mu_horizon = mu
        sigma_horizon = sigma

    # Generate simulated returns
    simulated_returns = np.random.normal(mu_horizon, sigma_horizon, simulations)

    # Calculate simulated portfolio values
    simulated_values = current_equity * (1 + simulated_returns)

    # Calculate P&L
    simulated_pnl = simulated_values - current_equity

    # Sort for VaR calculation
    sorted_pnl = np.sort(simulated_pnl)

    # VaR at confidence level
    var_index = int((1 - confidence) * simulations)
    var_value = -sorted_pnl[var_index]  # Negative because loss

    # Expected Shortfall (CVaR) - average loss beyond VaR
    cvar_value = -np.mean(sorted_pnl[:var_index])

    # Additional statistics
    percentiles = {
        "p99": -np.percentile(simulated_pnl, 1),
        "p95": -np.percentile(simulated_pnl, 5),
        "p90": -np.percentile(simulated_pnl, 10),
        "p75": -np.percentile(simulated_pnl, 25),
        "median": np.median(simulated_pnl),
    }

    # VaR as percentage of portfolio
    var_pct = (var_value / current_equity) * 100

    return {
        "project_id": project_id,
        "current_equity": round(current_equity, 2),
        "horizon_days": horizon_days,
        "confidence_level": confidence,
        "simulations": simulations,
        "var_dollars": round(var_value, 2),
        "var_pct": round(var_pct, 2),
        "cvar_dollars": round(cvar_value, 2),
        "cvar_pct": round((cvar_value / current_equity) * 100, 2),
        "percentiles": {k: round(v, 2) for k, v in percentiles.items()},
        "distribution": {
            "mean_daily_return": round(mu * 100, 4),
            "daily_volatility": round(sigma * 100, 4),
            "annualized_volatility": round(sigma * np.sqrt(252) * 100, 2),
        },
        "simulation_stats": {
            "min_pnl": round(float(np.min(simulated_pnl)), 2),
            "max_pnl": round(float(np.max(simulated_pnl)), 2),
            "mean_pnl": round(float(np.mean(simulated_pnl)), 2),
            "std_pnl": round(float(np.std(simulated_pnl)), 2),
        },
        "interpretation": _interpret_var(var_pct, confidence),
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
    }


def _interpret_var(var_pct: float, confidence: float) -> str:
    """Provide interpretation of VaR result."""
    pct_label = f"{confidence * 100:.0f}%"

    if var_pct < 1:
        return f"Low risk: {pct_label} confident daily loss won't exceed {var_pct:.1f}% of portfolio"
    elif var_pct < 3:
        return f"Moderate risk: {pct_label} confident daily loss won't exceed {var_pct:.1f}%"
    elif var_pct < 5:
        return f"Elevated risk: {pct_label} confident daily loss won't exceed {var_pct:.1f}%"
    else:
        return f"High risk: Portfolio could lose {var_pct:.1f}% in a day (with {(1-confidence)*100:.0f}% probability of worse)"


def historical_var(
    project_id: str,
    confidence: float = DEFAULT_CONFIDENCE,
    horizon_days: int = DEFAULT_HORIZON_DAYS,
    lookback_days: int = 252
) -> dict[str, Any]:
    """Calculate Historical VaR (non-parametric).

    Uses actual historical returns rather than simulated ones.

    Args:
        project_id: Trading project ID
        confidence: Confidence level
        horizon_days: Time horizon
        lookback_days: Historical data period

    Returns:
        Dict with historical VaR estimate
    """
    since = datetime.now(tz=timezone.utc) - timedelta(days=lookback_days)
    curve = PortfolioSnapshotsRepo.curve(project_id, since=since)

    if len(curve) < 20:
        return {"error": "Insufficient historical data", "data_points": len(curve)}

    equities = np.array([pt["equity"] for pt in curve])
    current_equity = equities[-1]

    # Calculate returns
    returns = np.diff(equities) / equities[:-1]
    returns = returns[~np.isnan(returns)]

    if len(returns) < 10:
        return {"error": "Insufficient return data"}

    # For multi-day horizon, use rolling returns
    if horizon_days > 1:
        # Calculate n-day rolling returns
        rolling_returns = []
        for i in range(len(equities) - horizon_days):
            ret = (equities[i + horizon_days] - equities[i]) / equities[i]
            rolling_returns.append(ret)
        returns = np.array(rolling_returns)

    # Sort returns and find VaR percentile
    sorted_returns = np.sort(returns)
    var_index = int((1 - confidence) * len(sorted_returns))

    var_return = sorted_returns[var_index]
    var_dollars = -var_return * current_equity

    # CVaR
    cvar_return = np.mean(sorted_returns[:var_index])
    cvar_dollars = -cvar_return * current_equity

    return {
        "project_id": project_id,
        "method": "historical",
        "current_equity": round(current_equity, 2),
        "horizon_days": horizon_days,
        "confidence_level": confidence,
        "lookback_days": lookback_days,
        "var_dollars": round(var_dollars, 2),
        "var_pct": round(-var_return * 100, 2),
        "cvar_dollars": round(cvar_dollars, 2),
        "cvar_pct": round(-cvar_return * 100, 2),
        "data_points": len(returns),
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
    }


def parametric_var(
    project_id: str,
    confidence: float = DEFAULT_CONFIDENCE,
    horizon_days: int = DEFAULT_HORIZON_DAYS,
    lookback_days: int = 252
) -> dict[str, Any]:
    """Calculate Parametric (Delta-Normal) VaR.

    Assumes returns are normally distributed.

    Args:
        project_id: Trading project ID
        confidence: Confidence level
        horizon_days: Time horizon
        lookback_days: Historical data period

    Returns:
        Dict with parametric VaR estimate
    """
    from scipy import stats

    since = datetime.now(tz=timezone.utc) - timedelta(days=lookback_days)
    curve = PortfolioSnapshotsRepo.curve(project_id, since=since)

    if len(curve) < 20:
        return {"error": "Insufficient historical data", "data_points": len(curve)}

    equities = np.array([pt["equity"] for pt in curve])
    current_equity = equities[-1]

    # Calculate returns
    returns = np.diff(equities) / equities[:-1]
    returns = returns[~np.isnan(returns)]

    if len(returns) < 10:
        return {"error": "Insufficient return data"}

    # Fit normal distribution
    mu = float(np.mean(returns))
    sigma = float(np.std(returns))

    # Scale for horizon
    mu_horizon = mu * horizon_days
    sigma_horizon = sigma * np.sqrt(horizon_days)

    # Z-score for confidence level
    z = stats.norm.ppf(1 - confidence)

    # VaR
    var_return = mu_horizon + z * sigma_horizon
    var_dollars = -var_return * current_equity

    return {
        "project_id": project_id,
        "method": "parametric",
        "current_equity": round(current_equity, 2),
        "horizon_days": horizon_days,
        "confidence_level": confidence,
        "var_dollars": round(var_dollars, 2),
        "var_pct": round(-var_return * 100, 2),
        "z_score": round(z, 4),
        "mean_return": round(mu * 100, 4),
        "volatility": round(sigma * 100, 4),
        "data_points": len(returns),
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
    }


def var_comparison(
    project_id: str,
    confidence: float = DEFAULT_CONFIDENCE,
    horizon_days: int = DEFAULT_HORIZON_DAYS
) -> dict[str, Any]:
    """Compare all VaR methodologies.

    Args:
        project_id: Trading project ID
        confidence: Confidence level
        horizon_days: Time horizon

    Returns:
        Dict with comparison of VaR methods
    """
    mc_var = monte_carlo_var(project_id, confidence, horizon_days=horizon_days)
    hist_var = historical_var(project_id, confidence, horizon_days=horizon_days)
    param_var = parametric_var(project_id, confidence, horizon_days=horizon_days)

    methods = []

    if "error" not in mc_var:
        methods.append({
            "method": "Monte Carlo",
            "var_dollars": mc_var["var_dollars"],
            "var_pct": mc_var["var_pct"],
            "cvar_dollars": mc_var.get("cvar_dollars"),
        })

    if "error" not in hist_var:
        methods.append({
            "method": "Historical",
            "var_dollars": hist_var["var_dollars"],
            "var_pct": hist_var["var_pct"],
            "cvar_dollars": hist_var.get("cvar_dollars"),
        })

    if "error" not in param_var:
        methods.append({
            "method": "Parametric",
            "var_dollars": param_var["var_dollars"],
            "var_pct": param_var["var_pct"],
        })

    if not methods:
        return {"error": "All VaR calculations failed"}

    # Average and conservative estimates
    var_values = [m["var_dollars"] for m in methods]

    return {
        "project_id": project_id,
        "confidence_level": confidence,
        "horizon_days": horizon_days,
        "methods": methods,
        "average_var": round(np.mean(var_values), 2),
        "conservative_var": round(max(var_values), 2),
        "recommendation": f"Use conservative estimate (${max(var_values):,.0f}) for risk management",
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
    }
