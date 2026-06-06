"""Pattern Day Trading (PDT) Rule Enforcement.

Tracks day trades and prevents PDT violations for accounts under $25,000.
A day trade is defined as opening and closing the same position within the same day.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text

from db.connection import insert_returning_id, session_scope
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


def log_same_day_closure(
    project_id: str,
    symbol: str,
    opened_at: Any,
    closed_at: Any,
) -> int | None:
    """Insert a day_trade_log row IF the closure was same-day as the
    open.

    Why this exists: until today, the platform only counted intraday
    long calls/puts as day trades. A wheel CSP opened at 9:30am and
    closed at 11am by take-profit IS ALSO a day trade for PDT
    purposes. On a sub-$25k account those silent day trades stack up
    and the broker locks the account at the 4th one in a 5-day
    window. This logs them properly so the pre-open PDT check sees
    them.

    Called from analytics.closure_detector for every detected
    closure. Returns the trade_id on insert, None when not same-day.
    """
    from datetime import datetime as _dt, timezone as _tz
    if opened_at is None or closed_at is None:
        return None
    if isinstance(opened_at, _dt) and opened_at.tzinfo is None:
        opened_at = opened_at.replace(tzinfo=_tz.utc)
    if isinstance(closed_at, _dt) and closed_at.tzinfo is None:
        closed_at = closed_at.replace(tzinfo=_tz.utc)
    try:
        if opened_at.date() != closed_at.date():
            return None
    except Exception:
        return None
    # Use the closure timestamp as the trade_date so day_trade_log
    # rolling-5-day query works.
    return log_day_trade(
        project_id=project_id,
        symbol=symbol,
        open_order_id="same_day_open_close",
        close_order_id="auto_logged_by_closure_detector",
        trade_date=closed_at,
    )


def is_pdt_at_risk(
    project_id: str,
    *,
    opening_now: bool = False,
) -> tuple[bool, str]:
    """Return (blocked, reason) for any sub-$25k account when opening
    a new position would risk hitting the 4th day-trade in 5 days.

    Difference from ``pdt_can_trade``:
      * checks count vs (MAX − safety_margin), not just MAX
      * when ``opening_now=True``, treats the *potential* future
        closure as a possible day-trade so we don't open a 3rd
        wheel CSP when we've already done 2 today + 1 last week

    Sub-$25k account = subject to FINRA PDT rules. >=$25k accounts
    return (False, "exempt") immediately.
    """
    equity = get_account_equity(project_id)
    if equity is None:
        # Without an equity reference, err on the side of allowing.
        return (False, "no equity reference; PDT check skipped")
    if equity >= PDT_EQUITY_THRESHOLD:
        return (False,
                f"account ${equity:,.0f} ≥ "
                f"${PDT_EQUITY_THRESHOLD:,.0f} (PDT exempt)")
    n = count_day_trades_5d(project_id)
    # Margin of safety: don't open a trade that COULD become the 4th
    # day-trade. Treat 3-of-3 already-counted as the cliff.
    if opening_now and n >= MAX_DAY_TRADES_5D - 1:
        return (True,
                f"PDT cliff: {n} day-trades in 5d (cap "
                f"{MAX_DAY_TRADES_5D}); a same-day close on a new "
                f"open would be the 4th. Skipping.")
    if n >= MAX_DAY_TRADES_5D:
        return (True,
                f"PDT cap hit: {n} day-trades in 5d (cap "
                f"{MAX_DAY_TRADES_5D}).")
    return (False, f"{MAX_DAY_TRADES_5D - n} day-trades remaining "
                   f"in 5d window")


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
        # day_trade_log is owned by db/schema_mysql.sql — no lazy DDL
        # needed (the old SQL Server CREATE TABLE here didn't run on
        # MySQL anyway).
        trade_id = insert_returning_id(s, """
            INSERT INTO day_trade_log
                (project_id, symbol, open_order_id, close_order_id, trade_date)
            VALUES (:p, :sym, :open_id, :close_id, :td)
        """, {
            "p": project_id,
            "sym": symbol,
            "open_id": open_order_id,
            "close_id": close_order_id,
            "td": trade_date,
        })
        s.commit()

    return trade_id if trade_id else 0


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