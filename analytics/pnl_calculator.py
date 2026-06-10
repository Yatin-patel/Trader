"""Pure functions to compute P&L metrics from closed trades + snapshots."""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any

import numpy as np

from db.analytics_repos import (
    ClosedContractsRepo,
    ClosedPositionsRepo,
    PortfolioSnapshotsRepo,
)

# Risk-free rate assumption (annual)
RISK_FREE_RATE = 0.05


def _utc_start_of_day() -> datetime:
    return datetime.now(tz=timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )


def _utc_start_of_week() -> datetime:
    now = datetime.now(tz=timezone.utc)
    return (now - timedelta(days=now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )


def _utc_start_of_month() -> datetime:
    now = datetime.now(tz=timezone.utc)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _utc_start_of_year() -> datetime:
    now = datetime.now(tz=timezone.utc)
    return now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)


def monthly_breakdown(project_id: str, from_date: datetime,
                      to_date: datetime) -> list[dict[str, Any]]:
    """Return one row per calendar month between ``from_date`` and
    ``to_date`` (inclusive). Each row has:
        {"month": "YYYY-MM", "realized_pnl": float,
         "premium_captured": float, "trade_count": int,
         "wins": int, "losses": int, "win_rate": float}

    Used by the P&L report page to show how each month contributed to
    the total. Pure aggregation over closed_contracts; stock-side P&L
    from closed_positions is added separately at the totals level (we
    can't always attribute a closure to a specific calendar month
    without ambiguity at month boundaries — option premiums are clearer).
    """
    from sqlalchemy import text
    from db.connection import session_scope
    if from_date.tzinfo is None:
        from_date = from_date.replace(tzinfo=timezone.utc)
    if to_date.tzinfo is None:
        to_date = to_date.replace(tzinfo=timezone.utc)
    with session_scope() as s:
        rows = s.execute(text("""
            SELECT DATE_FORMAT(closed_at, '%Y-%m') AS ym,
                   COALESCE(SUM(realized_pnl), 0) AS pnl,
                   COALESCE(SUM(premium_collected), 0) AS premium,
                   COUNT(*) AS trades,
                   SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) AS wins,
                   SUM(CASE WHEN realized_pnl < 0 THEN 1 ELSE 0 END) AS losses
            FROM closed_contracts
            WHERE project_id = :p
              AND closed_at >= :s AND closed_at < :e
            GROUP BY DATE_FORMAT(closed_at, '%Y-%m')
            ORDER BY ym ASC
        """), {"p": project_id, "s": from_date, "e": to_date}).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        trades = int(r[3] or 0)
        wins = int(r[4] or 0)
        losses = int(r[5] or 0)
        out.append({
            "month": r[0],
            "realized_pnl": round(float(r[1] or 0), 2),
            "premium_captured": round(float(r[2] or 0), 2),
            "trade_count": trades,
            "wins": wins,
            "losses": losses,
            "win_rate": round(wins / trades, 4) if trades else 0.0,
        })
    return out


def metrics_summary(project_id: str, period: str = "all") -> dict[str, Any]:
    """Aggregate metrics over the requested period.

    period: today | week | month | ytd | all
    """
    period = (period or "all").lower()
    if period == "today":
        since = _utc_start_of_day()
    elif period == "week":
        since = _utc_start_of_week()
    elif period == "month":
        since = _utc_start_of_month()
    elif period == "ytd":
        since = _utc_start_of_year()
    else:
        since = datetime(1970, 1, 1, tzinfo=timezone.utc)

    contracts = ClosedContractsRepo.list(project_id, since=since, limit=10000)
    realized_from_positions = ClosedPositionsRepo.realized_pnl_since(project_id, since)

    pnl_values = [c["realized_pnl"] for c in contracts]
    total_pnl = sum(pnl_values) + realized_from_positions
    wins = [v for v in pnl_values if v > 0]
    losses = [v for v in pnl_values if v < 0]
    total_premium = sum(c["premium_collected"] for c in contracts)
    trade_count = len(contracts)
    win_rate = (len(wins) / trade_count) if trade_count else 0.0
    avg_winner = (sum(wins) / len(wins)) if wins else 0.0
    avg_loser = (sum(losses) / len(losses)) if losses else 0.0
    profit_factor = (sum(wins) / abs(sum(losses))) if losses else (float("inf") if wins else 0.0)

    # Max drawdown from equity curve (best-effort; needs snapshots over the period)
    curve = PortfolioSnapshotsRepo.curve(project_id, since=since)
    drawdown = _max_drawdown([pt["equity"] for pt in curve]) if curve else 0.0

    latest_snap = PortfolioSnapshotsRepo.latest(project_id)

    # Broker-truth net account P&L = current equity − equity at the start of
    # the period. This is the ONLY P&L number that reconciles to the broker
    # (Alpaca) account. The `realized_pnl` above is gross option premium
    # (premium − close_cost) and does NOT subtract assignment/stock losses —
    # an assigned put is booked as a full premium "win" while the resulting
    # stock loss is never recorded here, so realized_pnl can read wildly
    # positive while the account is actually down. Surface account_net_pnl as
    # the headline everywhere a user reads "profit". curve[0] is the earliest
    # snapshot in the period (earliest-ever for period="all").
    starting_equity = None
    if curve:
        starting_equity = curve[0].get("equity")
    if starting_equity is None:
        starting_equity = (
            PortfolioSnapshotsRepo.earliest(project_id) or {}).get("equity")
    current_equity = (latest_snap or {}).get("equity")
    account_net_pnl = None
    account_net_pnl_pct = None
    if current_equity is not None and starting_equity:
        account_net_pnl = round(
            float(current_equity) - float(starting_equity), 2)
        if float(starting_equity) > 0:
            account_net_pnl_pct = round(
                account_net_pnl / float(starting_equity) * 100, 2)

    return {
        "period": period,
        "since": since.isoformat(),
        # Broker-reconciled truth (headline). See note above.
        "account_net_pnl": account_net_pnl,
        "account_net_pnl_pct": account_net_pnl_pct,
        "starting_equity": (round(float(starting_equity), 2)
                            if starting_equity else None),
        # Gross option premium realized (premium − close_cost). NOT net of
        # assignment/stock losses — kept for premium-capture analysis only.
        "gross_option_realized": round(total_pnl, 2),
        "realized_pnl": round(total_pnl, 2),
        "total_premium_captured": round(total_premium, 2),
        "trade_count": trade_count,
        "win_rate": round(win_rate, 4),
        "wins": len(wins),
        "losses": len(losses),
        "avg_winner": round(avg_winner, 2),
        "avg_loser": round(avg_loser, 2),
        "profit_factor": (
            None if profit_factor == float("inf") else round(profit_factor, 2)
        ),
        "max_drawdown": round(drawdown, 2),
        "current_equity": (latest_snap or {}).get("equity"),
        "unrealized_pnl": (latest_snap or {}).get("unrealized_pnl", 0.0),
    }


def _max_drawdown(equity_series: list[float]) -> float:
    """Return max drawdown as a positive dollar amount."""
    peak = float("-inf")
    max_dd = 0.0
    for v in equity_series:
        if v > peak:
            peak = v
        if peak > 0:
            dd = peak - v
            if dd > max_dd:
                max_dd = dd
    return max_dd


def equity_curve_points(project_id: str, period: str = "month") -> list[dict[str, Any]]:
    period = (period or "month").lower()
    if period == "today":
        since = _utc_start_of_day()
    elif period == "week":
        since = _utc_start_of_week()
    elif period == "month":
        since = _utc_start_of_month()
    elif period == "ytd":
        since = _utc_start_of_year()
    else:
        since = datetime(1970, 1, 1, tzinfo=timezone.utc)
    return PortfolioSnapshotsRepo.curve(project_id, since=since)


def calculate_sharpe_ratio(
    project_id: str,
    period_days: int = 252,
    risk_free_rate: float = RISK_FREE_RATE
) -> dict[str, Any]:
    """Calculate Sharpe Ratio for the portfolio.

    Sharpe = (Portfolio Return - Risk-Free Rate) / Portfolio Std Dev

    Args:
        project_id: Trading project ID
        period_days: Lookback period in days (252 = 1 trading year)
        risk_free_rate: Annual risk-free rate (default 5%)

    Returns:
        Dict with Sharpe ratio and components
    """
    since = datetime.now(tz=timezone.utc) - timedelta(days=period_days)
    curve = PortfolioSnapshotsRepo.curve(project_id, since=since)

    if len(curve) < 10:
        return {"error": "Insufficient data for Sharpe calculation", "data_points": len(curve)}

    equities = np.array([pt["equity"] for pt in curve])

    # Calculate daily returns
    returns = np.diff(equities) / equities[:-1]
    returns = returns[~np.isnan(returns)]  # Remove NaN

    if len(returns) < 5:
        return {"error": "Insufficient return data", "data_points": len(returns)}

    # Annualize returns and volatility
    mean_daily_return = float(np.mean(returns))
    std_daily_return = float(np.std(returns))

    annualized_return = mean_daily_return * 252
    annualized_volatility = std_daily_return * math.sqrt(252)

    # Sharpe ratio
    if annualized_volatility == 0:
        sharpe = 0.0
    else:
        sharpe = (annualized_return - risk_free_rate) / annualized_volatility

    return {
        "sharpe_ratio": round(sharpe, 3),
        "annualized_return_pct": round(annualized_return * 100, 2),
        "annualized_volatility_pct": round(annualized_volatility * 100, 2),
        "risk_free_rate_pct": round(risk_free_rate * 100, 2),
        "data_points": len(returns),
        "period_days": period_days,
        "interpretation": _interpret_sharpe(sharpe),
    }


def _interpret_sharpe(sharpe: float) -> str:
    """Provide interpretation of Sharpe ratio."""
    if sharpe < 0:
        return "Negative returns vs risk-free rate"
    elif sharpe < 0.5:
        return "Poor risk-adjusted returns"
    elif sharpe < 1.0:
        return "Acceptable risk-adjusted returns"
    elif sharpe < 2.0:
        return "Good risk-adjusted returns"
    elif sharpe < 3.0:
        return "Excellent risk-adjusted returns"
    else:
        return "Outstanding (verify data accuracy)"


def calculate_sortino_ratio(
    project_id: str,
    period_days: int = 252,
    risk_free_rate: float = RISK_FREE_RATE,
    mar: float | None = None
) -> dict[str, Any]:
    """Calculate Sortino Ratio for the portfolio.

    Sortino = (Portfolio Return - MAR) / Downside Deviation

    Unlike Sharpe, Sortino only penalizes downside volatility.

    Args:
        project_id: Trading project ID
        period_days: Lookback period in days
        risk_free_rate: Annual risk-free rate
        mar: Minimum Acceptable Return (defaults to risk-free rate)

    Returns:
        Dict with Sortino ratio and components
    """
    if mar is None:
        mar = risk_free_rate

    since = datetime.now(tz=timezone.utc) - timedelta(days=period_days)
    curve = PortfolioSnapshotsRepo.curve(project_id, since=since)

    if len(curve) < 10:
        return {"error": "Insufficient data for Sortino calculation", "data_points": len(curve)}

    equities = np.array([pt["equity"] for pt in curve])

    # Calculate daily returns
    returns = np.diff(equities) / equities[:-1]
    returns = returns[~np.isnan(returns)]

    if len(returns) < 5:
        return {"error": "Insufficient return data", "data_points": len(returns)}

    # Daily MAR
    mar_daily = mar / 252

    # Downside returns (only negative relative to MAR)
    downside_returns = returns - mar_daily
    downside_returns = np.minimum(downside_returns, 0)

    # Downside deviation (semi-deviation)
    downside_deviation = float(np.sqrt(np.mean(downside_returns ** 2)))
    annualized_downside = downside_deviation * math.sqrt(252)

    # Annualized return
    mean_daily_return = float(np.mean(returns))
    annualized_return = mean_daily_return * 252

    # Sortino ratio
    if annualized_downside == 0:
        sortino = 0.0 if annualized_return <= mar else float("inf")
    else:
        sortino = (annualized_return - mar) / annualized_downside

    # Handle inf for display
    sortino_display = round(sortino, 3) if sortino != float("inf") else None

    return {
        "sortino_ratio": sortino_display,
        "annualized_return_pct": round(annualized_return * 100, 2),
        "downside_deviation_pct": round(annualized_downside * 100, 2),
        "mar_pct": round(mar * 100, 2),
        "data_points": len(returns),
        "period_days": period_days,
        "interpretation": _interpret_sortino(sortino),
    }


def _interpret_sortino(sortino: float) -> str:
    """Provide interpretation of Sortino ratio."""
    if sortino == float("inf"):
        return "Perfect (no downside deviation)"
    elif sortino < 0:
        return "Returns below minimum acceptable"
    elif sortino < 1.0:
        return "Poor downside-adjusted returns"
    elif sortino < 2.0:
        return "Acceptable downside-adjusted returns"
    elif sortino < 3.0:
        return "Good downside-adjusted returns"
    else:
        return "Excellent downside-adjusted returns"


def calculate_risk_ratios(project_id: str, period_days: int = 252) -> dict[str, Any]:
    """Calculate all risk ratios in one call.

    Args:
        project_id: Trading project ID
        period_days: Lookback period

    Returns:
        Dict with all risk metrics
    """
    sharpe = calculate_sharpe_ratio(project_id, period_days)
    sortino = calculate_sortino_ratio(project_id, period_days)

    # Get summary for additional context
    period = "ytd" if period_days >= 252 else "month"
    summary = metrics_summary(project_id, period)

    # Calmar ratio (return / max drawdown)
    max_dd = summary.get("max_drawdown", 0)
    ann_return = sharpe.get("annualized_return_pct", 0) / 100

    if max_dd > 0:
        calmar = ann_return / (max_dd / summary.get("current_equity", 1) * 100)
    else:
        calmar = None

    return {
        "project_id": project_id,
        "period_days": period_days,
        "sharpe_ratio": sharpe.get("sharpe_ratio"),
        "sortino_ratio": sortino.get("sortino_ratio"),
        "calmar_ratio": round(calmar, 3) if calmar else None,
        "annualized_return_pct": sharpe.get("annualized_return_pct"),
        "annualized_volatility_pct": sharpe.get("annualized_volatility_pct"),
        "downside_deviation_pct": sortino.get("downside_deviation_pct"),
        "max_drawdown": summary.get("max_drawdown"),
        "win_rate": summary.get("win_rate"),
        "profit_factor": summary.get("profit_factor"),
        "data_points": sharpe.get("data_points"),
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
    }
