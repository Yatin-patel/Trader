"""Dollar-Cost Averaging (DCA) Strategy.

Automates periodic purchases of specified assets regardless of price.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text

from db.connection import session_scope
from db.repositories import EventsRepo, ProjectsRepo
from execution import AlpacaClient

logger = logging.getLogger(__name__)


def _ensure_dca_tables() -> None:
    """Create DCA tables if they don't exist."""
    with session_scope() as s:
        s.execute(text("""
            IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME = 'dca_schedules')
            BEGIN
                CREATE TABLE dca_schedules (
                    schedule_id BIGINT IDENTITY(1,1) PRIMARY KEY,
                    project_id VARCHAR(64) NOT NULL,
                    ticker VARCHAR(12) NOT NULL,
                    frequency VARCHAR(20) NOT NULL DEFAULT 'weekly',
                    amount_dollars DECIMAL(18,2) NOT NULL,
                    next_execution_date DATE NOT NULL,
                    last_execution_date DATE NULL,
                    enabled BIT NOT NULL DEFAULT 1,
                    total_invested DECIMAL(18,2) NOT NULL DEFAULT 0,
                    total_shares DECIMAL(18,6) NOT NULL DEFAULT 0,
                    execution_count INT NOT NULL DEFAULT 0,
                    created_at DATETIME(6) NOT NULL DEFAULT UTC_TIMESTAMP(),
                    updated_at DATETIME(6) NOT NULL DEFAULT UTC_TIMESTAMP(),
                    CONSTRAINT FK_dca_schedules_project FOREIGN KEY (project_id)
                        REFERENCES trading_projects(project_id) ON DELETE CASCADE
                );
                CREATE INDEX IX_dca_schedules_project_enabled
                    ON dca_schedules(project_id, enabled, next_execution_date);
            END
        """))
        s.commit()


def create_dca_schedule(
    project_id: str,
    ticker: str,
    amount_dollars: float,
    frequency: str = "weekly",
    start_date: date | None = None
) -> int:
    """Create a new DCA schedule.

    Args:
        project_id: Trading project ID
        ticker: Stock symbol to buy
        amount_dollars: Dollar amount to invest each period
        frequency: 'daily', 'weekly', 'biweekly', 'monthly'
        start_date: First execution date (defaults to next occurrence)

    Returns:
        Schedule ID
    """
    _ensure_dca_tables()

    if start_date is None:
        start_date = _next_execution_date(frequency)

    with session_scope() as s:
        row = s.execute(text("""
            INSERT INTO dca_schedules
                (project_id, ticker, frequency, amount_dollars, next_execution_date)
            OUTPUT INSERTED.schedule_id
            VALUES (:p, :t, :f, :amt, :next)
        """), {
            "p": project_id,
            "t": ticker.upper(),
            "f": frequency,
            "amt": amount_dollars,
            "next": start_date,
        }).fetchone()
        s.commit()

    EventsRepo.log(project_id, "DCA", "SCHEDULE_CREATED", {
        "ticker": ticker,
        "amount": amount_dollars,
        "frequency": frequency,
        "start_date": start_date.isoformat(),
    })

    return int(row[0]) if row else 0


def _next_execution_date(frequency: str, from_date: date | None = None) -> date:
    """Calculate next execution date based on frequency.

    Args:
        frequency: 'daily', 'weekly', 'biweekly', 'monthly'
        from_date: Calculate from this date (defaults to today)

    Returns:
        Next execution date
    """
    if from_date is None:
        from_date = date.today()

    if frequency == "daily":
        return from_date + timedelta(days=1)
    elif frequency == "weekly":
        # Next Monday
        days_ahead = 7 - from_date.weekday()
        if days_ahead == 0:
            days_ahead = 7
        return from_date + timedelta(days=days_ahead)
    elif frequency == "biweekly":
        days_ahead = 14 - from_date.weekday()
        if days_ahead <= 0:
            days_ahead += 14
        return from_date + timedelta(days=days_ahead)
    elif frequency == "monthly":
        # First of next month
        if from_date.month == 12:
            return date(from_date.year + 1, 1, 1)
        return date(from_date.year, from_date.month + 1, 1)
    else:
        return from_date + timedelta(days=7)


def list_dca_schedules(
    project_id: str,
    enabled_only: bool = False
) -> list[dict[str, Any]]:
    """List DCA schedules for a project.

    Args:
        project_id: Trading project ID
        enabled_only: Only return enabled schedules

    Returns:
        List of schedules
    """
    _ensure_dca_tables()

    sql = """
        SELECT schedule_id, ticker, frequency, amount_dollars,
               next_execution_date, last_execution_date, enabled,
               total_invested, total_shares, execution_count, created_at
        FROM dca_schedules
        WHERE project_id = :p
    """
    params: dict[str, Any] = {"p": project_id}

    if enabled_only:
        sql += " AND enabled = 1"

    sql += " ORDER BY next_execution_date"

    with session_scope() as s:
        rows = s.execute(text(sql), params).fetchall()

    return [
        {
            "schedule_id": r[0],
            "ticker": r[1],
            "frequency": r[2],
            "amount_dollars": float(r[3]),
            "next_execution_date": r[4].isoformat() if r[4] else None,
            "last_execution_date": r[5].isoformat() if r[5] else None,
            "enabled": bool(r[6]),
            "total_invested": float(r[7]),
            "total_shares": float(r[8]),
            "execution_count": int(r[9]),
            "avg_cost_basis": round(float(r[7]) / float(r[8]), 2) if r[8] > 0 else None,
            "created_at": r[10].isoformat() if r[10] else None,
        }
        for r in rows
    ]


def update_dca_schedule(
    project_id: str,
    schedule_id: int,
    amount_dollars: float | None = None,
    frequency: str | None = None,
    enabled: bool | None = None
) -> bool:
    """Update a DCA schedule.

    Args:
        project_id: Trading project ID
        schedule_id: Schedule to update
        amount_dollars: New amount (optional)
        frequency: New frequency (optional)
        enabled: Enable/disable (optional)

    Returns:
        True if updated
    """
    _ensure_dca_tables()

    updates = []
    params: dict[str, Any] = {"p": project_id, "sid": schedule_id}

    if amount_dollars is not None:
        updates.append("amount_dollars = :amt")
        params["amt"] = amount_dollars

    if frequency is not None:
        updates.append("frequency = :f")
        params["f"] = frequency

    if enabled is not None:
        updates.append("enabled = :e")
        params["e"] = 1 if enabled else 0

    if not updates:
        return False

    updates.append("updated_at = UTC_TIMESTAMP()")

    sql = f"""
        UPDATE dca_schedules
        SET {', '.join(updates)}
        WHERE project_id = :p AND schedule_id = :sid
    """

    with session_scope() as s:
        s.execute(text(sql), params)
        s.commit()

    return True


def delete_dca_schedule(project_id: str, schedule_id: int) -> bool:
    """Delete a DCA schedule.

    Args:
        project_id: Trading project ID
        schedule_id: Schedule to delete

    Returns:
        True if deleted
    """
    _ensure_dca_tables()

    with session_scope() as s:
        s.execute(text("""
            DELETE FROM dca_schedules
            WHERE project_id = :p AND schedule_id = :sid
        """), {"p": project_id, "sid": schedule_id})
        s.commit()

    return True


def execute_dca_purchase(project_id: str, schedule_id: int) -> dict[str, Any]:
    """Execute a DCA purchase for a schedule.

    Args:
        project_id: Trading project ID
        schedule_id: Schedule to execute

    Returns:
        Execution result
    """
    _ensure_dca_tables()

    # Get schedule
    with session_scope() as s:
        row = s.execute(text("""
            SELECT ticker, amount_dollars, frequency, enabled
            FROM dca_schedules
            WHERE project_id = :p AND schedule_id = :sid
        """), {"p": project_id, "sid": schedule_id}).fetchone()

    if not row:
        return {"error": "Schedule not found"}

    ticker, amount, frequency, enabled = row

    if not enabled:
        return {"error": "Schedule is disabled"}

    project = ProjectsRepo.get(project_id)
    if project is None:
        return {"error": "Project not found"}

    try:
        client = AlpacaClient(project)

        # Get current price
        snap = client.snapshots([ticker]).get(ticker)
        if not snap or snap.last_price <= 0:
            return {"error": f"Could not get price for {ticker}"}

        # Calculate shares to buy (fractional if supported)
        shares = amount / snap.last_price

        # Submit market order
        order = client.submit_market_equity(
            symbol=ticker,
            qty=int(shares) if shares >= 1 else 1,
            side="buy",
            time_in_force="day"
        )

        actual_shares = float(order.get("qty", shares))
        actual_amount = actual_shares * snap.last_price

        # Update schedule
        with session_scope() as s:
            s.execute(text("""
                UPDATE dca_schedules
                SET total_invested = total_invested + :amt,
                    total_shares = total_shares + :shares,
                    execution_count = execution_count + 1,
                    last_execution_date = CAST(UTC_TIMESTAMP() AS DATE),
                    next_execution_date = :next,
                    updated_at = UTC_TIMESTAMP()
                WHERE project_id = :p AND schedule_id = :sid
            """), {
                "p": project_id,
                "sid": schedule_id,
                "amt": actual_amount,
                "shares": actual_shares,
                "next": _next_execution_date(frequency),
            })
            s.commit()

        EventsRepo.log(project_id, "DCA", "EXECUTE", {
            "schedule_id": schedule_id,
            "ticker": ticker,
            "amount": actual_amount,
            "shares": actual_shares,
            "price": snap.last_price,
            "order": order,
        })

        return {
            "success": True,
            "ticker": ticker,
            "shares": actual_shares,
            "amount": round(actual_amount, 2),
            "price": snap.last_price,
            "order_id": order.get("id"),
        }

    except Exception as e:
        logger.exception("DCA execution failed: %s", e)
        EventsRepo.log(project_id, "DCA", "ERROR", {
            "schedule_id": schedule_id,
            "ticker": ticker,
            "error": str(e),
        })
        return {"error": str(e)}


def get_due_schedules(project_id: str) -> list[dict[str, Any]]:
    """Get schedules due for execution today.

    Args:
        project_id: Trading project ID

    Returns:
        List of due schedules
    """
    _ensure_dca_tables()

    today = date.today()

    with session_scope() as s:
        rows = s.execute(text("""
            SELECT schedule_id, ticker, amount_dollars, frequency
            FROM dca_schedules
            WHERE project_id = :p AND enabled = 1 AND next_execution_date <= :today
        """), {"p": project_id, "today": today}).fetchall()

    return [
        {
            "schedule_id": r[0],
            "ticker": r[1],
            "amount_dollars": float(r[2]),
            "frequency": r[3],
        }
        for r in rows
    ]


def execute_due_schedules(project_id: str) -> list[dict[str, Any]]:
    """Execute all due DCA schedules for a project.

    Args:
        project_id: Trading project ID

    Returns:
        List of execution results
    """
    due = get_due_schedules(project_id)
    results = []

    for schedule in due:
        result = execute_dca_purchase(project_id, schedule["schedule_id"])
        results.append({
            "schedule_id": schedule["schedule_id"],
            "ticker": schedule["ticker"],
            **result,
        })

    return results
