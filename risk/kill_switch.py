"""Kill-switch evaluator.

Reads enabled risk_limits for a project and checks whether any threshold has
been breached this cycle. On breach: records it, logs an event, optionally
liquidates, and flips trading_projects.is_active = 0.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text

from db.analytics_repos import (
    ClosedContractsRepo,
    ClosedPositionsRepo,
    PortfolioSnapshotsRepo,
)
from db.connection import session_scope
from db.repositories import EventsRepo, ProjectsRepo
from db.risk_repos import RiskLimitsRepo
from execution import AlpacaClient

logger = logging.getLogger(__name__)


def _today_start() -> datetime:
    return datetime.now(tz=timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )


def _check_daily_loss(project_id: str, threshold: float) -> tuple[bool, float]:
    """Threshold is a positive dollar amount; breach when realized P&L < -threshold."""
    today = _today_start()
    realized = (
        ClosedContractsRepo.realized_pnl_since(project_id, today)
        + ClosedPositionsRepo.realized_pnl_since(project_id, today)
    )
    return (realized <= -abs(threshold), realized)


def _check_drawdown(project_id: str, threshold_pct: float) -> tuple[bool, float]:
    """Threshold is a 0..1 fraction; breach when equity_drop / peak > threshold."""
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    curve = PortfolioSnapshotsRepo.curve(project_id, since=epoch, max_points=5000)
    if not curve:
        return (False, 0.0)
    peak = max(p["equity"] for p in curve)
    current = curve[-1]["equity"]
    if peak <= 0:
        return (False, 0.0)
    dd = (peak - current) / peak
    return (dd >= threshold_pct, dd)


def _check_consecutive_losses(project_id: str, n: int) -> tuple[bool, int]:
    trades = ClosedContractsRepo.list(project_id, limit=int(max(20, n * 2)))
    streak = 0
    for t in trades:                          # newest-first
        if t["realized_pnl"] < 0:
            streak += 1
        else:
            break
    return (streak >= int(n), streak)


def _check_error_storm(project_id: str, n: int,
                       window_minutes: int) -> tuple[bool, int]:
    cutoff = datetime.now(tz=timezone.utc) - timedelta(minutes=int(window_minutes or 5))
    with session_scope() as s:
        row = s.execute(text("""
            SELECT COUNT(*) FROM dbo.agent_events
            WHERE project_id = :p
              AND event_type = 'ERROR'
              AND created_at >= :since
        """), {"p": project_id, "since": cutoff}).fetchone()
    count = int(row[0] or 0)
    return (count >= int(n), count)


_EVALUATORS = {
    "daily_loss":          _check_daily_loss,
    "drawdown_pct":        _check_drawdown,
    "consecutive_losses":  _check_consecutive_losses,
    "error_storm":         _check_error_storm,
}


def _deactivate_project(project_id: str) -> None:
    with session_scope() as s:
        s.execute(text("""
            UPDATE dbo.trading_projects SET is_active = 0
            WHERE project_id = :p
        """), {"p": project_id})
        s.commit()


def _liquidate_all(project_id: str) -> list[dict[str, Any]]:
    project = ProjectsRepo.get(project_id)
    if project is None:
        return []
    client = AlpacaClient(project)
    results = []
    try:
        positions = client.list_positions()
    except Exception as e:
        return [{"error": str(e)}]
    for p in positions:
        try:
            res = client.liquidate_position(p["symbol"])
            results.append({"symbol": p["symbol"], "result": res})
        except Exception as e:
            results.append({"symbol": p["symbol"], "error": str(e)})
    return results


def evaluate_kill_switches(project_id: str) -> list[dict[str, Any]]:
    """Evaluate all enabled kill switches; return list of breaches (if any)."""
    breaches: list[dict[str, Any]] = []
    limits = RiskLimitsRepo.list(project_id, enabled_only=True)
    if not limits:
        return breaches

    for lim in limits:
        evaluator = _EVALUATORS.get(lim["limit_type"])
        if evaluator is None:
            continue
        try:
            if lim["limit_type"] == "error_storm":
                breached, value = evaluator(project_id, lim["threshold"],
                                            lim["window_minutes"])
            else:
                breached, value = evaluator(project_id, lim["threshold"])
        except Exception as e:
            logger.exception("kill-switch eval error: %s", e)
            continue
        if not breached:
            continue

        # Record + act
        RiskLimitsRepo.record_breach(lim["limit_id"], float(value))
        actions: list[dict[str, Any]] = []
        if lim["action"] == "LIQUIDATE":
            actions = _liquidate_all(project_id)
        _deactivate_project(project_id)

        narrative = [
            f"🛑 Kill switch BREACHED: {lim['limit_type']} (threshold "
            f"{lim['threshold']}, observed {value:.4f}).",
            f"Action: {lim['action']}. Project is_active flipped to false.",
        ]
        breach_payload = {
            "limit_type": lim["limit_type"],
            "threshold": lim["threshold"],
            "observed_value": value,
            "action": lim["action"],
            "liquidation_results": actions,
            "narrative": narrative,
        }
        EventsRepo.log(project_id, "Risk", "KILL_SWITCH", breach_payload)
        try:
            from notifications.dispatcher import notify_event
            notify_event(project_id, "KILL_SWITCH", breach_payload)
        except Exception as e:
            logger.exception("notifier failed on kill_switch: %s", e)
        breaches.append({
            "limit_type": lim["limit_type"],
            "observed": value,
            "action": lim["action"],
        })

    return breaches
