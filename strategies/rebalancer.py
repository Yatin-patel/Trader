"""Portfolio Rebalancing Strategy.

Maintains target asset allocations by periodically rebalancing positions.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text

from db.connection import insert_returning_id, session_scope
from db.repositories import EventsRepo, ProjectsRepo
from execution import AlpacaClient

logger = logging.getLogger(__name__)


def _ensure_allocation_tables() -> None:
    """Create allocation tables if they don't exist."""
    with session_scope() as s:
        s.execute(text("""
            IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME = 'target_allocations')
            BEGIN
                CREATE TABLE target_allocations (
                    allocation_id BIGINT IDENTITY(1,1) PRIMARY KEY,
                    project_id VARCHAR(64) NOT NULL,
                    ticker VARCHAR(12) NOT NULL,
                    target_pct DECIMAL(8,4) NOT NULL,
                    rebalance_threshold_pct DECIMAL(8,4) NOT NULL DEFAULT 0.05,
                    created_at DATETIME(6) NOT NULL DEFAULT UTC_TIMESTAMP(),
                    updated_at DATETIME(6) NOT NULL DEFAULT UTC_TIMESTAMP(),
                    CONSTRAINT FK_target_allocations_project FOREIGN KEY (project_id)
                        REFERENCES trading_projects(project_id) ON DELETE CASCADE,
                    CONSTRAINT UQ_target_allocations UNIQUE (project_id, ticker)
                );
                CREATE INDEX IX_target_allocations_project
                    ON target_allocations(project_id);
            END
        """))
        s.commit()


def set_target_allocation(
    project_id: str,
    ticker: str,
    target_pct: float,
    threshold_pct: float = 0.05
) -> int:
    """Set target allocation for a ticker.

    Args:
        project_id: Trading project ID
        ticker: Stock symbol
        target_pct: Target allocation percentage (0.0-1.0)
        threshold_pct: Rebalance if drift exceeds this threshold

    Returns:
        Allocation ID
    """
    _ensure_allocation_tables()

    with session_scope() as s:
        # Upsert
        existing = s.execute(text("""
            SELECT allocation_id FROM target_allocations
            WHERE project_id = :p AND ticker = :t
        """), {"p": project_id, "t": ticker.upper()}).fetchone()

        if existing:
            s.execute(text("""
                UPDATE target_allocations
                SET target_pct = :pct, rebalance_threshold_pct = :thresh,
                    updated_at = UTC_TIMESTAMP()
                WHERE project_id = :p AND ticker = :t
            """), {"p": project_id, "t": ticker.upper(), "pct": target_pct, "thresh": threshold_pct})
            s.commit()
            return existing[0]
        else:
            allocation_id = insert_returning_id(s, """
                INSERT INTO target_allocations
                    (project_id, ticker, target_pct, rebalance_threshold_pct)
                VALUES (:p, :t, :pct, :thresh)
            """, {"p": project_id, "t": ticker.upper(), "pct": target_pct, "thresh": threshold_pct})
            s.commit()
            return allocation_id if row else 0


def get_target_allocations(project_id: str) -> list[dict[str, Any]]:
    """Get all target allocations for a project.

    Args:
        project_id: Trading project ID

    Returns:
        List of allocations
    """
    _ensure_allocation_tables()

    with session_scope() as s:
        rows = s.execute(text("""
            SELECT allocation_id, ticker, target_pct, rebalance_threshold_pct, updated_at
            FROM target_allocations
            WHERE project_id = :p
            ORDER BY target_pct DESC
        """), {"p": project_id}).fetchall()

    return [
        {
            "allocation_id": r[0],
            "ticker": r[1],
            "target_pct": float(r[2]),
            "threshold_pct": float(r[3]),
            "updated_at": r[4].isoformat() if r[4] else None,
        }
        for r in rows
    ]


def delete_target_allocation(project_id: str, ticker: str) -> bool:
    """Remove a target allocation.

    Args:
        project_id: Trading project ID
        ticker: Stock symbol

    Returns:
        True if deleted
    """
    _ensure_allocation_tables()

    with session_scope() as s:
        s.execute(text("""
            DELETE FROM target_allocations
            WHERE project_id = :p AND ticker = :t
        """), {"p": project_id, "t": ticker.upper()})
        s.commit()

    return True


def get_current_allocations(project_id: str) -> dict[str, Any]:
    """Get current portfolio allocations vs targets.

    Args:
        project_id: Trading project ID

    Returns:
        Dict with current and target allocations
    """
    _ensure_allocation_tables()

    project = ProjectsRepo.get(project_id)
    if project is None:
        return {"error": "Project not found"}

    try:
        client = AlpacaClient(project)
        account = client.get_account()
        positions = client.list_positions()
    except Exception as e:
        return {"error": f"Failed to get positions: {e}"}

    total_value = float(account.get("equity", 0))
    if total_value <= 0:
        return {"error": "No portfolio value"}

    # Get targets
    targets = {a["ticker"]: a for a in get_target_allocations(project_id)}

    # Calculate current allocations
    allocations = []
    cash_value = float(account.get("cash", 0))

    for pos in positions:
        if pos["asset_class"] != "us_equity":
            continue

        ticker = pos["symbol"]
        market_value = float(pos.get("market_value", 0))
        current_pct = market_value / total_value

        target = targets.get(ticker)
        target_pct = target["target_pct"] if target else None
        threshold = target["threshold_pct"] if target else 0.05

        drift = abs(current_pct - target_pct) if target_pct is not None else 0
        needs_rebalance = drift > threshold if target_pct is not None else False

        allocations.append({
            "ticker": ticker,
            "shares": float(pos["qty"]),
            "market_value": round(market_value, 2),
            "current_pct": round(current_pct, 4),
            "target_pct": target_pct,
            "drift_pct": round(drift, 4) if target_pct is not None else None,
            "needs_rebalance": needs_rebalance,
        })

    # Add tickers with targets but no positions
    current_tickers = {a["ticker"] for a in allocations}
    for ticker, target in targets.items():
        if ticker not in current_tickers:
            allocations.append({
                "ticker": ticker,
                "shares": 0,
                "market_value": 0,
                "current_pct": 0,
                "target_pct": target["target_pct"],
                "drift_pct": target["target_pct"],
                "needs_rebalance": target["target_pct"] > target["threshold_pct"],
            })

    # Cash allocation
    cash_pct = cash_value / total_value if total_value > 0 else 0

    allocations.sort(key=lambda x: x["market_value"], reverse=True)

    return {
        "project_id": project_id,
        "total_value": round(total_value, 2),
        "cash_value": round(cash_value, 2),
        "cash_pct": round(cash_pct, 4),
        "allocations": allocations,
        "needs_rebalance": any(a["needs_rebalance"] for a in allocations),
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
    }


def preview_rebalance(project_id: str) -> dict[str, Any]:
    """Preview rebalance trades needed.

    Args:
        project_id: Trading project ID

    Returns:
        Dict with proposed trades
    """
    current = get_current_allocations(project_id)

    if "error" in current:
        return current

    total_value = current["total_value"]
    trades = []

    for alloc in current["allocations"]:
        if not alloc["needs_rebalance"]:
            continue

        target_pct = alloc.get("target_pct")
        if target_pct is None:
            continue

        current_value = alloc["market_value"]
        target_value = total_value * target_pct
        diff_value = target_value - current_value

        if abs(diff_value) < 10:  # Skip tiny adjustments
            continue

        trades.append({
            "ticker": alloc["ticker"],
            "action": "BUY" if diff_value > 0 else "SELL",
            "current_value": current_value,
            "target_value": round(target_value, 2),
            "trade_value": round(abs(diff_value), 2),
            "current_pct": alloc["current_pct"],
            "target_pct": target_pct,
        })

    trades.sort(key=lambda x: x["trade_value"], reverse=True)

    return {
        "project_id": project_id,
        "total_value": total_value,
        "trades": trades,
        "trade_count": len(trades),
        "total_trade_value": sum(t["trade_value"] for t in trades),
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
    }


def execute_rebalance(project_id: str, dry_run: bool = False) -> dict[str, Any]:
    """Execute rebalance trades.

    Args:
        project_id: Trading project ID
        dry_run: If True, only preview without executing

    Returns:
        Execution results
    """
    preview = preview_rebalance(project_id)

    if "error" in preview:
        return preview

    if dry_run:
        return {"dry_run": True, **preview}

    if not preview["trades"]:
        return {"message": "No rebalancing needed", "trades": []}

    project = ProjectsRepo.get(project_id)
    if project is None:
        return {"error": "Project not found"}

    client = AlpacaClient(project)
    results = []

    # Execute sells first, then buys
    sells = [t for t in preview["trades"] if t["action"] == "SELL"]
    buys = [t for t in preview["trades"] if t["action"] == "BUY"]

    for trade in sells + buys:
        try:
            snap = client.snapshots([trade["ticker"]]).get(trade["ticker"])
            if not snap or snap.last_price <= 0:
                results.append({"ticker": trade["ticker"], "error": "No price available"})
                continue

            shares = int(trade["trade_value"] / snap.last_price)
            if shares < 1:
                results.append({"ticker": trade["ticker"], "skipped": "Trade too small"})
                continue

            order = client.submit_market_equity(
                symbol=trade["ticker"],
                qty=shares,
                side="sell" if trade["action"] == "SELL" else "buy",
                time_in_force="day"
            )

            results.append({
                "ticker": trade["ticker"],
                "action": trade["action"],
                "shares": shares,
                "price": snap.last_price,
                "value": round(shares * snap.last_price, 2),
                "order_id": order.get("id"),
                "success": True,
            })

        except Exception as e:
            results.append({
                "ticker": trade["ticker"],
                "action": trade["action"],
                "error": str(e),
            })

    EventsRepo.log(project_id, "Rebalancer", "EXECUTE", {
        "trades_attempted": len(preview["trades"]),
        "trades_executed": sum(1 for r in results if r.get("success")),
        "results": results,
    })

    return {
        "project_id": project_id,
        "executed": True,
        "results": results,
        "success_count": sum(1 for r in results if r.get("success")),
        "error_count": sum(1 for r in results if r.get("error")),
        "executed_at": datetime.now(tz=timezone.utc).isoformat(),
    }