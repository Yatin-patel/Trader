"""Dividend tracking and projection for long-term investing.

Tracks dividend events, calculates yields, and projects income.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text

from db.connection import insert_returning_id, session_scope
from db.repositories import EventsRepo, ProjectsRepo
from execution import AlpacaClient

logger = logging.getLogger(__name__)


def _ensure_dividend_tables() -> None:
    """Create dividend tables if they don't exist."""
    with session_scope() as s:
        s.execute(text("""
            IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME = 'dividend_events')
            BEGIN
                CREATE TABLE dividend_events (
                    event_id BIGINT IDENTITY(1,1) PRIMARY KEY,
                    project_id VARCHAR(64) NOT NULL,
                    ticker VARCHAR(12) NOT NULL,
                    ex_date DATE NOT NULL,
                    record_date DATE NULL,
                    pay_date DATE NULL,
                    amount DECIMAL(18,6) NOT NULL,
                    shares_held INT NOT NULL,
                    total_amount DECIMAL(18,4) NOT NULL,
                    status VARCHAR(20) NOT NULL DEFAULT 'PENDING',
                    created_at DATETIME(6) NOT NULL DEFAULT UTC_TIMESTAMP(),
                    CONSTRAINT FK_dividend_events_project FOREIGN KEY (project_id)
                        REFERENCES trading_projects(project_id) ON DELETE CASCADE
                );
                CREATE INDEX IX_dividend_events_project_ticker
                    ON dividend_events(project_id, ticker, ex_date);
            END
        """))
        s.commit()


def fetch_dividend_info(ticker: str) -> dict[str, Any] | None:
    """Fetch dividend information for a ticker from external source.

    Args:
        ticker: Stock symbol

    Returns:
        Dict with dividend info or None if unavailable
    """
    try:
        import yfinance as yf
        stock = yf.Ticker(ticker)
        info = stock.info

        dividend_rate = info.get("dividendRate", 0) or 0
        dividend_yield = info.get("dividendYield", 0) or 0
        ex_date = info.get("exDividendDate")

        if ex_date:
            ex_date = datetime.fromtimestamp(ex_date, tz=timezone.utc).date()

        # Get dividend history
        divs = stock.dividends
        history = []
        if not divs.empty:
            for dt, amount in divs.tail(8).items():
                history.append({
                    "date": dt.strftime("%Y-%m-%d"),
                    "amount": float(amount),
                })

        return {
            "ticker": ticker.upper(),
            "dividend_rate": dividend_rate,
            "dividend_yield": dividend_yield * 100 if dividend_yield < 1 else dividend_yield,
            "ex_dividend_date": ex_date.isoformat() if ex_date else None,
            "frequency": "quarterly" if len(history) >= 4 else "annual",
            "history": history,
        }

    except Exception as e:
        logger.warning("Failed to fetch dividend info for %s: %s", ticker, e)
        return None


def record_dividend_event(
    project_id: str,
    ticker: str,
    ex_date: date,
    amount: float,
    shares_held: int,
    record_date: date | None = None,
    pay_date: date | None = None
) -> int:
    """Record an expected or received dividend event.

    Args:
        project_id: Trading project ID
        ticker: Stock symbol
        ex_date: Ex-dividend date
        amount: Dividend amount per share
        shares_held: Number of shares held at ex-date
        record_date: Record date (optional)
        pay_date: Payment date (optional)

    Returns:
        Event ID
    """
    _ensure_dividend_tables()

    total = amount * shares_held

    with session_scope() as s:
        event_id = insert_returning_id(s, """
            INSERT INTO dividend_events
                (project_id, ticker, ex_date, record_date, pay_date, amount, shares_held, total_amount)
            VALUES (:p, :t, :ex, :rec, :pay, :amt, :shares, :total)
        """, {
            "p": project_id,
            "t": ticker.upper(),
            "ex": ex_date,
            "rec": record_date,
            "pay": pay_date,
            "amt": amount,
            "shares": shares_held,
            "total": total,
        })
        s.commit()

    return event_id if row else 0


def list_dividend_events(
    project_id: str,
    ticker: str | None = None,
    status: str | None = None,
    limit: int = 50
) -> list[dict[str, Any]]:
    """List dividend events for a project.

    Args:
        project_id: Trading project ID
        ticker: Filter by ticker (optional)
        status: Filter by status (optional)
        limit: Maximum results

    Returns:
        List of dividend events
    """
    _ensure_dividend_tables()

    sql = """
        SELECT event_id, ticker, ex_date, record_date, pay_date,
               amount, shares_held, total_amount, status, created_at
        FROM dividend_events
        WHERE project_id = :p
    """
    params: dict[str, Any] = {"p": project_id}

    if ticker:
        sql += " AND ticker = :t"
        params["t"] = ticker.upper()

    if status:
        sql += " AND status = :s"
        params["s"] = status

    sql += f" ORDER BY ex_date DESC LIMIT {int(limit)}"

    with session_scope() as s:
        rows = s.execute(text(sql), params).fetchall()

    return [
        {
            "event_id": r[0],
            "ticker": r[1],
            "ex_date": r[2].isoformat() if r[2] else None,
            "record_date": r[3].isoformat() if r[3] else None,
            "pay_date": r[4].isoformat() if r[4] else None,
            "amount": float(r[5]),
            "shares_held": int(r[6]),
            "total_amount": float(r[7]),
            "status": r[8],
            "created_at": r[9].isoformat() if r[9] else None,
        }
        for r in rows
    ]


def project_dividend_income(project_id: str, months: int = 12) -> dict[str, Any]:
    """Project dividend income based on current holdings and dividend rates.

    Args:
        project_id: Trading project ID
        months: Projection period in months

    Returns:
        Dict with projected income by ticker and total
    """
    project = ProjectsRepo.get(project_id)
    if project is None:
        return {"error": "Project not found"}

    try:
        client = AlpacaClient(project)
        positions = client.list_positions()
    except Exception as e:
        return {"error": f"Failed to get positions: {e}"}

    projections = []
    total_annual = 0.0

    for pos in positions:
        if pos["asset_class"] != "us_equity":
            continue

        ticker = pos["symbol"]
        qty = int(pos["qty"])

        div_info = fetch_dividend_info(ticker)
        if not div_info or div_info["dividend_rate"] == 0:
            continue

        annual_income = div_info["dividend_rate"] * qty
        period_income = annual_income * (months / 12)

        projections.append({
            "ticker": ticker,
            "shares": qty,
            "dividend_rate": div_info["dividend_rate"],
            "dividend_yield": div_info["dividend_yield"],
            "annual_income": round(annual_income, 2),
            "period_income": round(period_income, 2),
            "next_ex_date": div_info.get("ex_dividend_date"),
        })

        total_annual += annual_income

    projections.sort(key=lambda x: x["annual_income"], reverse=True)

    return {
        "project_id": project_id,
        "projection_months": months,
        "total_annual_income": round(total_annual, 2),
        "total_period_income": round(total_annual * (months / 12), 2),
        "by_ticker": projections,
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
    }


def dividend_summary(project_id: str) -> dict[str, Any]:
    """Get dividend summary including received and projected.

    Args:
        project_id: Trading project ID

    Returns:
        Dict with dividend summary
    """
    _ensure_dividend_tables()

    # Get received dividends (last 12 months)
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=365)

    with session_scope() as s:
        received = s.execute(text("""
            SELECT COALESCE(SUM(total_amount), 0), COUNT(*)
            FROM dividend_events
            WHERE project_id = :p AND status = 'RECEIVED' AND ex_date >= :cutoff
        """), {"p": project_id, "cutoff": cutoff}).fetchone()

        pending = s.execute(text("""
            SELECT COALESCE(SUM(total_amount), 0), COUNT(*)
            FROM dividend_events
            WHERE project_id = :p AND status = 'PENDING'
        """), {"p": project_id}).fetchone()

    projection = project_dividend_income(project_id, months=12)

    return {
        "project_id": project_id,
        "received_12m": {
            "total": float(received[0]) if received else 0,
            "count": int(received[1]) if received else 0,
        },
        "pending": {
            "total": float(pending[0]) if pending else 0,
            "count": int(pending[1]) if pending else 0,
        },
        "projected_12m": projection.get("total_annual_income", 0),
        "top_payers": projection.get("by_ticker", [])[:5],
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
    }