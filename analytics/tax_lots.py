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


def form_8949_rows(project_id: str, year: int) -> list[dict[str, Any]]:
    """Per-lot detail rows formatted for IRS Form 8949 / Schedule D.

    Combines BOTH sources of realized cap-gains for the year:
      1. Stock-lot FIFO consumptions (one row per debit from tax_lots)
      2. Option-premium realizations (one row per EXPIRED /
         BOUGHT_TO_CLOSE / STOPPED_OUT short option in closed_contracts)

    The columns match what TurboTax / H&R Block / Drake import flows
    expect for manual capital-gains entry (Description, Date Acquired,
    Date Sold, Proceeds, Cost Basis, Term, Gain/Loss).

    NOTE: This is informational, not tax advice. Wash-sale adjustments
    and specific-identification rules are out of scope — FIFO only.
    Consult a tax professional before filing.
    """
    start = datetime(year, 1, 1, tzinfo=timezone.utc)
    end = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
    with session_scope() as s:
        rows = s.execute(text("""
            SELECT c.consumption_id, c.ticker, c.quantity,
                   c.proceeds, c.basis, c.realized_pnl, c.holding_days,
                   c.term, c.closed_at, c.sale_price,
                   l.opened_at, l.cost_per_share
            FROM tax_lot_consumptions c
            JOIN tax_lots l ON l.lot_id = c.lot_id
            WHERE c.project_id = :p
              AND c.closed_at >= :s AND c.closed_at < :e
            ORDER BY c.closed_at ASC, c.consumption_id ASC
        """), {"p": project_id, "s": start, "e": end}).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        opened_at = r[10]
        closed_at = r[8]
        out.append({
            "kind": "stock",
            "description": f"{int(r[2])} sh {r[1]}",
            "date_acquired": (opened_at.date().isoformat()
                              if opened_at else None),
            "date_sold": (closed_at.date().isoformat()
                          if closed_at else None),
            "proceeds": float(r[3]),
            "cost_basis": float(r[4]),
            "realized_pnl": float(r[5]),
            "holding_days": int(r[6]),
            "term": r[7],   # 'short' | 'long'
            "ticker": r[1],
            "quantity": int(r[2]),
            "sale_price": float(r[9]),
            "cost_per_share": float(r[11]),
        })

    # Option-premium realizations.
    for r in option_realizations_for_year(project_id, year):
        opened = r.get("opened_at")
        closed = r.get("closed_at")
        out.append({
            "kind": "option",
            "description": r["description"],
            "date_acquired": (opened.date().isoformat()
                              if opened else None),
            "date_sold": (closed.date().isoformat()
                          if closed else None),
            "proceeds": float(r["proceeds"]),
            "cost_basis": float(r["basis"]),
            "realized_pnl": float(r["realized_pnl"]),
            "holding_days": int(r["holding_days"]),
            "term": r["term"],
            "ticker": r["ticker"],
            "quantity": int(r["quantity"]),
            "sale_price": None,
            "cost_per_share": None,
            "closure_reason": r.get("closure_reason"),
        })

    # Chronological order across both kinds (consistent file output).
    out.sort(key=lambda x: (x.get("date_sold") or "",
                            x.get("ticker") or ""))
    return out


def option_realizations_for_year(project_id: str,
                                  year: int) -> list[dict[str, Any]]:
    """Realized cap-gains rows from option closures in ``year``.

    Premium kept on EXPIRED, BOUGHT_TO_CLOSE, and STOPPED_OUT short
    options is a separately-realized taxable event. Per IRS guidance,
    ASSIGNED option premium does NOT generate a standalone cap gain —
    it adjusts the basis of the underlying stock lot. We therefore
    exclude assigned closures from this query.

    Each returned row mirrors ``tax_lot_consumptions`` shape so
    ``capital_gains_summary`` can union the two sources:

        {"ticker", "term": "short"|"long",
         "quantity", "proceeds", "basis",
         "realized_pnl", "holding_days",
         "closed_at", "opened_at", "description"}

    Holding period rule: equity-option short positions can in principle
    be long-term if held > 365 days; in practice for wheel strategies
    they never are. We compute term from ``days_held`` regardless.
    """
    start = datetime(year, 1, 1, tzinfo=timezone.utc)
    end = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
    with session_scope() as s:
        rows = s.execute(text("""
            SELECT ticker, strategy_phase, option_symbol,
                   strike_price, quantity, premium_collected,
                   close_cost, realized_pnl, days_held,
                   opened_at, closed_at, closure_reason
            FROM closed_contracts
            WHERE project_id = :p
              AND closed_at >= :s AND closed_at < :e
              AND closure_reason <> 'ASSIGNED'
            ORDER BY closed_at ASC, closure_id ASC
        """), {"p": project_id, "s": start, "e": end}).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        ticker = r[0]
        phase = r[1] or ""
        strike = float(r[3] or 0)
        qty = int(r[4] or 1)
        premium = float(r[5] or 0)
        close_cost = float(r[6] or 0)
        realized = float(r[7] or 0)
        days = int(r[8] or 0)
        opened_at = r[9]
        closed_at = r[10]
        reason = r[11] or ""
        # Proceeds = premium collected * 100 * qty (the dollar amount
        # received when the short option was opened).
        proceeds = round(premium * 100 * qty, 2)
        # Basis = close cost * 100 * qty (zero if expired worthless).
        basis = round(close_cost * 100 * qty, 2)
        # Sanity: closed_contracts.realized_pnl is the source of truth.
        # If it disagrees materially from proceeds-basis (e.g. legacy
        # rows missing close_cost) prefer the stored realized_pnl and
        # backfill basis.
        if abs((proceeds - basis) - realized) > 0.01:
            basis = round(proceeds - realized, 2)
        term = "long" if days > 365 else "short"
        side = "PUT" if "PUT" in phase.upper() or phase == "CASH_SECURED_PUT" else "CALL"
        desc = (f"{qty} short {side} {ticker} ${strike:.2f} "
                f"({reason.lower()})")
        out.append({
            "ticker": ticker,
            "term": term,
            "quantity": qty,
            "proceeds": proceeds,
            "basis": basis,
            "realized_pnl": realized,
            "holding_days": days,
            "opened_at": opened_at,
            "closed_at": closed_at,
            "description": desc,
            "source": "option",
            "closure_reason": reason,
        })
    return out


def capital_gains_summary(project_id: str, year: int) -> dict[str, Any]:
    """Per-year cap-gains summary, broken out by ticker and term.

    Combines BOTH sources of realized capital gains:
      - stock tax-lot consumptions (CSP-assigned shares later sold/called)
      - option-premium realizations on EXPIRED / BOUGHT_TO_CLOSE /
        STOPPED_OUT short options (premium kept = cap gain).

    For wheel strategies, the option side is usually the larger of the
    two — every expired-worthless short put is a taxable event.
    """
    start = datetime(year, 1, 1, tzinfo=timezone.utc)
    end = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
    with session_scope() as s:
        stock_rows = s.execute(text("""
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
    for r in stock_rows:
        t = r[0]
        term = r[1]
        pnl = float(r[5] or 0)
        by_ticker.setdefault(t, {"ticker": t, "short": 0.0, "long": 0.0,
                                  "stock_short": 0.0, "stock_long": 0.0,
                                  "option_short": 0.0, "option_long": 0.0})
        by_ticker[t][term] = (by_ticker[t].get(term) or 0.0) + pnl
        by_ticker[t]["stock_" + term] = pnl
        if term == "long":
            long_total += pnl
        else:
            short_total += pnl

    # Now add option premium realizations.
    opt_rows = option_realizations_for_year(project_id, year)
    option_short_total = 0.0
    option_long_total = 0.0
    for r in opt_rows:
        t = r["ticker"]
        term = r["term"]
        pnl = float(r["realized_pnl"] or 0)
        entry = by_ticker.setdefault(t, {
            "ticker": t, "short": 0.0, "long": 0.0,
            "stock_short": 0.0, "stock_long": 0.0,
            "option_short": 0.0, "option_long": 0.0,
        })
        entry[term] = (entry.get(term) or 0.0) + pnl
        entry["option_" + term] = (entry.get("option_" + term) or 0.0) + pnl
        if term == "long":
            long_total += pnl
            option_long_total += pnl
        else:
            short_total += pnl
            option_short_total += pnl

    return {
        "year": year,
        "short_term_total": round(short_total, 2),
        "long_term_total": round(long_total, 2),
        "grand_total": round(short_total + long_total, 2),
        "by_ticker": sorted(by_ticker.values(), key=lambda x: x["ticker"]),
        "breakdown": {
            "stock_short_term": round(short_total - option_short_total, 2),
            "stock_long_term": round(long_total - option_long_total, 2),
            "option_short_term": round(option_short_total, 2),
            "option_long_term": round(option_long_total, 2),
        },
    }
