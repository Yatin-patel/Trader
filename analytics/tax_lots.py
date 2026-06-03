"""Tax-lot accounting (FIFO).

Why this matters: when shares are assigned via a CSP, we open a tax lot
at the strike price. When those shares are called away via a CC (or
sold for any reason), we consume the oldest lot(s) first (FIFO) and
realize a capital gain or loss. The IRS requires per-lot accounting
for capital-gains tax reporting; without it, you cannot do
tax-loss-harvesting or compute true cost basis.

What this module exposes:
  open_lot()           - record a new lot (called by analytics/closure
                         detector on assignment, or by the manual
                         "I deposited 100 shares" admin path)
  consume_lots_fifo()  - sell shares; debits the oldest lots first and
                         returns the realized gain breakdown
  open_lots()          - list still-open lots for a project/ticker
  closed_lots()        - list closed lots with realized PnL
  capital_gains_summary() - yearly summary, short vs long term

Holding period rule (IRS): >365 calendar days = long-term; <= 365 =
short-term. That's the only distinction we make; specific identification
and ETF wash-sale rules are out of scope for v1.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text

from db.connection import insert_returning_id, session_scope

logger = logging.getLogger(__name__)


def open_lot(project_id: str, ticker: str, *, quantity: int,
             cost_per_share: float, opened_at: datetime | None = None,
             source: str = "assignment",
             linked_contract_id: int | None = None) -> int:
    """Open a new tax lot. Returns the lot_id."""
    if quantity <= 0:
        raise ValueError("quantity must be > 0")
    opened_at = opened_at or datetime.now(tz=timezone.utc)
    with session_scope() as s:
        lot_id = insert_returning_id(s, """
            INSERT INTO tax_lots
                (project_id, ticker, quantity_opened, quantity_remaining,
                 cost_per_share, opened_at, source, linked_contract_id)
            VALUES (:p, :t, :q, :q, :c, :o, :s, :lc)
        """, {"p": project_id, "t": ticker.upper(),
              "q": int(quantity), "c": float(cost_per_share),
              "o": opened_at, "s": source[:32],
              "lc": linked_contract_id})
        s.commit()
        logger.info("opened tax lot %s: %s x%d @ $%.4f",
                    lot_id, ticker, quantity, cost_per_share)
        return lot_id


def consume_lots_fifo(project_id: str, ticker: str, *,
                      quantity: int, sale_price: float,
                      closed_at: datetime | None = None,
                      reason: str = "called_away") -> dict[str, Any]:
    """Sell shares FIFO; returns the per-lot realization breakdown.

    Raises ValueError if there aren't enough open shares to consume.
    """
    if quantity <= 0:
        raise ValueError("quantity must be > 0")
    closed_at = closed_at or datetime.now(tz=timezone.utc)
    ticker = ticker.upper()

    remaining = int(quantity)
    realizations: list[dict[str, Any]] = []
    total_realized = 0.0
    short_term_realized = 0.0
    long_term_realized = 0.0

    with session_scope() as s:
        open_rows = s.execute(text("""
            SELECT lot_id, quantity_remaining, cost_per_share, opened_at
            FROM tax_lots
            WHERE project_id = :p AND ticker = :t
              AND quantity_remaining > 0
            ORDER BY opened_at ASC, lot_id ASC
        """), {"p": project_id, "t": ticker}).fetchall()

        total_available = sum(int(r[1]) for r in open_rows)
        if total_available < remaining:
            raise ValueError(
                f"insufficient open lots for {ticker}: "
                f"requested {remaining}, available {total_available}"
            )

        for row in open_rows:
            if remaining <= 0:
                break
            lot_id = int(row[0])
            lot_remaining = int(row[1])
            cost = float(row[2])
            opened_at = row[3]
            consume_qty = min(remaining, lot_remaining)

            # Holding period
            holding_days = (closed_at.date() - (
                opened_at.date() if hasattr(opened_at, "date")
                else opened_at
            )).days
            term = "long" if holding_days > 365 else "short"

            proceeds = float(sale_price) * consume_qty
            basis = cost * consume_qty
            realized = proceeds - basis
            total_realized += realized
            if term == "long":
                long_term_realized += realized
            else:
                short_term_realized += realized

            # Insert consumption row
            insert_returning_id(s, """
                INSERT INTO tax_lot_consumptions
                    (lot_id, project_id, ticker, quantity, sale_price,
                     proceeds, basis, realized_pnl, holding_days,
                     term, closed_at, reason)
                VALUES (:l, :p, :t, :q, :sp, :pr, :b, :rl, :hd, :tm,
                        :ca, :rsn)
            """, {"l": lot_id, "p": project_id, "t": ticker,
                  "q": consume_qty, "sp": float(sale_price),
                  "pr": proceeds, "b": basis, "rl": realized,
                  "hd": int(holding_days), "tm": term,
                  "ca": closed_at, "rsn": reason[:32]})

            # Decrement lot
            new_remaining = lot_remaining - consume_qty
            s.execute(text("""
                UPDATE tax_lots
                SET quantity_remaining = :nr,
                    closed_at = CASE WHEN :nr = 0 THEN :ca ELSE closed_at END
                WHERE lot_id = :l
            """), {"nr": new_remaining, "ca": closed_at, "l": lot_id})

            realizations.append({
                "lot_id": lot_id, "quantity": consume_qty,
                "cost_per_share": cost, "sale_price": float(sale_price),
                "proceeds": proceeds, "basis": basis,
                "realized_pnl": realized, "holding_days": holding_days,
                "term": term,
            })
            remaining -= consume_qty
        s.commit()

    return {
        "ticker": ticker, "quantity_sold": int(quantity),
        "sale_price": float(sale_price),
        "lots_consumed": len(realizations),
        "total_realized_pnl": total_realized,
        "short_term_realized": short_term_realized,
        "long_term_realized": long_term_realized,
        "realizations": realizations,
    }


def open_lots(project_id: str,
              ticker: str | None = None) -> list[dict[str, Any]]:
    sql = (
        "SELECT lot_id, ticker, quantity_opened, quantity_remaining, "
        "cost_per_share, opened_at, source, linked_contract_id "
        "FROM tax_lots "
        "WHERE project_id = :p AND quantity_remaining > 0"
    )
    params: dict[str, Any] = {"p": project_id}
    if ticker:
        sql += " AND ticker = :t"
        params["t"] = ticker.upper()
    sql += " ORDER BY opened_at ASC"
    with session_scope() as s:
        rows = s.execute(text(sql), params).fetchall()
    return [{
        "lot_id": int(r[0]), "ticker": r[1],
        "quantity_opened": int(r[2]),
        "quantity_remaining": int(r[3]),
        "cost_per_share": float(r[4]),
        "opened_at": r[5].isoformat() if r[5] else None,
        "source": r[6],
        "linked_contract_id": int(r[7]) if r[7] is not None else None,
    } for r in rows]


def capital_gains_summary(project_id: str, year: int) -> dict[str, Any]:
    """Per-year cap-gains summary, broken out by ticker and term."""
    start = datetime(year, 1, 1, tzinfo=timezone.utc)
    end = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
    with session_scope() as s:
        rows = s.execute(text("""
            SELECT ticker, term,
                   SUM(quantity) AS qty,
                   SUM(proceeds) AS proceeds,
                   SUM(basis) AS basis,
                   SUM(realized_pnl) AS pnl
            FROM tax_lot_consumptions
            WHERE project_id = :p
              AND closed_at >= :s AND closed_at < :e
            GROUP BY ticker, term
            ORDER BY ticker, term
        """), {"p": project_id, "s": start, "e": end}).fetchall()
    by_ticker: dict[str, dict[str, Any]] = {}
    short_total = long_total = 0.0
    for r in rows:
        t = r[0]
        term = r[1]
        pnl = float(r[5] or 0)
        by_ticker.setdefault(t, {"ticker": t, "short": 0, "long": 0})
        by_ticker[t][term] = pnl
        if term == "long":
            long_total += pnl
        else:
            short_total += pnl
    return {
        "year": year,
        "short_term_total": short_total,
        "long_term_total": long_total,
        "grand_total": short_total + long_total,
        "by_ticker": list(by_ticker.values()),
    }
