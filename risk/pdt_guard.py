"""Pattern Day Trading (PDT) Rule Enforcement.

Tracks day trades and prevents PDT violations for accounts under $25,000.
A day trade is defined as opening and closing the same position within the same day.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text

from db.connection import session_scope
from db.repositories import EventsRepo, ProjectsRepo
from execution import AlpacaClient

logger = logging.getLogger(__name__)

# PDT threshold - accounts under this equity are subject to PDT rules
PDT_EQUITY_THRESHOLD = 25000.0

# Maximum day trades allowed in a 5-day rolling window for PDT accounts
MAX_DAY_TRADES_5D = 3


def count_day_trades_5d(project_id: str) -> int:
    """Count day trades in the last 5 trading days.

    A day trade is recorded when a position is opened and closed on the same day.

    Args:
        project_id: Trading project ID

    Returns:
        Number of day trades in rolling 5-day window
    """
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=5)

    with session_scope() as s:
        # Check if table exists
        exists = s.execute(text(
            "SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME = 'day_trade_log'"
        )).fetchone()

        if not exists:
            return 0

        row = s.execute(text("""
            SELECT COUNT(*) FROM day_trade_log
            WHERE project_id = :p AND trade_date >= :cutoff
        """), {"p": project_id, "cutoff": cutoff}).fetchone()

    return int(row[0]) if row else 0


def log_day_trade(
    project_id: str,
    symbol: str,
    open_order_id: str,
    close_order_id: str,
    trade_date: datetime | None = None
) -> int:
    """Record a day trade for PDT tracking.

    Args:
        project_id: Trading project ID
        symbol: Stock/option symbol
        open_order_id: Order ID that opened the position
        close_order_id: Order ID that closed the position
        trade_date: Date of the trade (defaults to today)

    Returns:
        Trade ID
    """
    if trade_date is None:
        trade_date = datetime.now(tz=timezone.utc)

    with session_scope() as s:
        # Ensure table exists
        s.execute(text("""
            IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME = 'day_trade_log')
            BEGIN
                CREATE TABLE day_trade_log (
                    trade_id BIGINT IDENTITY(1,1) PRIMARY KEY,
                    project_id VARCHAR(64) NOT NULL,
                    symbol VARCHAR(64) NOT NULL,
                    open_order_id VARCHAR(64) NOT NULL,
                    close_order_id VARCHAR(64) NOT NULL,
                    trade_date DATETIME(6) NOT NULL,
                    created_at DATETIME(6) NOT NULL DEFAULT UTC_TIMESTAMP()
                );
                CREATE INDEX IX_day_trade_log_project_date
                    ON day_trade_log(project_id, trade_date);
            END
        """))
        s.commit()

        row = s.execute(text("""
            INSERT INTO day_trade_log
                (project_id, symbol, open_order_id, close_order_id, trade_date)
            OUTPUT INSERTED.trade_id
            VALUES (:p, :sym, :open_id, :close_id, :td)
        """), {
            "p": project_id,
            "sym": symbol,
            "open_id": open_order_id,
            "close_id": close_order_id,
            "td": trade_date,
        }).fetchone()
        s.commit()

    return int(row[0]) if row else 0


def get_account_equity(project_id: str) -> float | None:
    """Get current account equity from broker.

    Args:
        project_id: Trading project ID

    Returns:
        Account equity or None if unavailable
    """
    project = ProjectsRepo.get(project_id)
    if project is None:
        return None

    try:
        client = AlpacaClient(project)
        account = client.get_account()
        return float(account.get("equity", 0))
    except Exception as e:
        logger.warning("Failed to get account equity: %s", e)
        return None


def pdt_can_trade(project_id: str, account_equity: float | None = None) -> tuple[bool, str]:
    """Check if a new day trade is allowed under PDT rules.

    Args:
        project_id: Trading project ID
        account_equity: Account equity (fetched if not provided)

    Returns:
        Tuple of (can_trade, reason)
    """
    # Get equity if not provided
    if account_equity is None:
        account_equity = get_account_equity(project_id)

    if account_equity is None:
        return True, "Unable to verify equity, allowing trade"

    # Accounts >= $25k are exempt from PDT
    if account_equity >= PDT_EQUITY_THRESHOLD:
        return True, f"Account equity ${account_equity:,.2f} >= ${PDT_EQUITY_THRESHOLD:,.0f}, PDT exempt"

    # Count existing day trades
    day_trades = count_day_trades_5d(project_id)

    if day_trades >= MAX_DAY_TRADES_5D:
        return False, (
            f"PDT violation risk: {day_trades} day trades in 5 days "
            f"(max {MAX_DAY_TRADES_5D}). Account equity ${account_equity:,.2f} "
            f"is below ${PDT_EQUITY_THRESHOLD:,.0f} threshold."
        )

    remaining = MAX_DAY_TRADES_5D - day_trades
    return True, f"Day trade allowed: {remaining} remaining in 5-day window"


def pdt_status(project_id: str) -> dict[str, Any]:
    """Get comprehensive PDT status for a project.

    Args:
        project_id: Trading project ID

    Returns:
        Dict with PDT status details
    """
    equity = get_account_equity(project_id)
    day_trades = count_day_trades_5d(project_id)
    can_trade, reason = pdt_can_trade(project_id, equity)

    is_exempt = equity is not None and equity >= PDT_EQUITY_THRESHOLD

    return {
        "project_id": project_id,
        "account_equity": equity,
        "pdt_threshold": PDT_EQUITY_THRESHOLD,
        "is_pdt_exempt": is_exempt,
        "day_trades_5d": day_trades,
        "max_day_trades": MAX_DAY_TRADES_5D,
        "remaining_day_trades": max(0, MAX_DAY_TRADES_5D - day_trades) if not is_exempt else None,
        "can_day_trade": can_trade,
        "status_message": reason,
        "checked_at": datetime.now(tz=timezone.utc).isoformat(),
    }


def evaluate_pdt_risk(project_id: str) -> list[dict[str, Any]]:
    """Evaluate PDT risk and generate warnings.

    Called by the risk evaluation pipeline.

    Args:
        project_id: Trading project ID

    Returns:
        List of warnings/alerts
    """
    status = pdt_status(project_id)
    warnings = []

    if status["is_pdt_exempt"]:
        return warnings

    remaining = status.get("remaining_day_trades", MAX_DAY_TRADES_5D)

    if remaining == 0:
        warnings.append({
            "severity": "error",
            "type": "PDT_LIMIT_REACHED",
            "message": f"PDT limit reached: {status['day_trades_5d']} day trades in 5 days. "
                       f"No additional day trades allowed until count resets.",
            "account_equity": status["account_equity"],
        })
        EventsRepo.log(project_id, "PDTGuard", "ALERT", {
            "type": "PDT_LIMIT_REACHED",
            "day_trades": status["day_trades_5d"],
            "equity": status["account_equity"],
        })
    elif remaining == 1:
        warnings.append({
            "severity": "warning",
            "type": "PDT_LIMIT_WARNING",
            "message": f"PDT warning: Only 1 day trade remaining in 5-day window. "
                       f"Account equity ${status['account_equity']:,.2f} is below PDT threshold.",
            "account_equity": status["account_equity"],
        })
        EventsRepo.log(project_id, "PDTGuard", "WARNING", {
            "type": "PDT_LIMIT_WARNING",
            "remaining": remaining,
            "equity": status["account_equity"],
        })
    elif remaining == 2:
        warnings.append({
            "severity": "info",
            "type": "PDT_LIMIT_APPROACHING",
            "message": f"PDT notice: {remaining} day trades remaining in 5-day window.",
            "account_equity": status["account_equity"],
        })

    return warnings
