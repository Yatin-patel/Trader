"""Pure functions to compute P&L metrics from closed trades + snapshots."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from db.analytics_repos import (
    ClosedContractsRepo,
    ClosedPositionsRepo,
    PortfolioSnapshotsRepo,
)


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

    return {
        "period": period,
        "since": since.isoformat(),
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
