"""Periodic portfolio-value snapshotter.

Pulls current account state from Alpaca and writes a row to
portfolio_snapshots so the equity curve has data points.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from db.analytics_repos import (
    ClosedContractsRepo,
    ClosedPositionsRepo,
    PortfolioSnapshotsRepo,
)
from db.repositories import ProjectsRepo
from execution import AlpacaClient

logger = logging.getLogger(__name__)


def take_snapshot(project_id: str) -> int | None:
    project = ProjectsRepo.get(project_id)
    if project is None:
        return None
    try:
        client = AlpacaClient(project)
        acct = client.get_account()
        positions = client.list_positions()
    except Exception as e:
        logger.warning("snapshotter: alpaca fetch failed for %s: %s", project_id, e)
        return None

    # Compute live unrealized P&L from open positions.
    unrealized = 0.0
    long_mv = 0.0
    short_mv = 0.0
    for p in positions:
        pl = p.get("unrealized_pl")
        if pl is not None:
            unrealized += float(pl)
        mv = p.get("market_value") or 0
        try:
            mv = float(mv)
        except Exception:
            mv = 0
        if mv >= 0:
            long_mv += mv
        else:
            short_mv += mv

    # Realized P&L today (since 00:00 UTC for simplicity).
    today_start = datetime.now(tz=timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    realized_day = (
        ClosedContractsRepo.realized_pnl_since(project_id, today_start)
        + ClosedPositionsRepo.realized_pnl_since(project_id, today_start)
    )

    return PortfolioSnapshotsRepo.insert(
        project_id=project_id,
        cash=acct["cash"],
        buying_power=acct["buying_power"],
        equity=acct["equity"],
        long_market_value=long_mv,
        short_market_value=short_mv,
        realized_pnl_day=realized_day,
        unrealized_pnl=unrealized,
    )
