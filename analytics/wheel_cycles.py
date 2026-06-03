"""Wheel cycle state machine.

A cycle is the lifetime of a wheel attempt on a single ticker:
  * starts when the first CSP is sold on a ticker (no other open cycle exists)
  * accumulates premium from every CSP and CC sold against that ticker
  * tracks assignments to adjust cost basis
  * closes when shares are called away, manually sold, or the position is
    forcibly stopped

The detector + executor call into this module at well-defined transitions.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text

from db.connection import insert_returning_id, session_scope

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


def get_open_cycle(project_id: str, ticker: str) -> dict[str, Any] | None:
    with session_scope() as s:
        row = s.execute(text("""
            SELECT cycle_id, status, started_at, total_premium,
                   realized_pnl, csp_count, cc_count, assignment_count,
                   cost_basis_adjusted
            FROM wheel_cycles
            WHERE project_id = :p AND ticker = :t AND status = 'OPEN'
            ORDER BY started_at DESC
            LIMIT 1
        """), {"p": project_id, "t": ticker}).fetchone()
    if not row:
        return None
    return {
        "cycle_id": int(row[0]), "status": row[1], "started_at": row[2],
        "total_premium": float(row[3]), "realized_pnl": float(row[4]),
        "csp_count": int(row[5]), "cc_count": int(row[6]),
        "assignment_count": int(row[7]),
        "cost_basis_adjusted": float(row[8]) if row[8] is not None else None,
    }


def open_cycle(project_id: str, ticker: str) -> int:
    """Return the cycle_id for `ticker`, opening a new one if none is OPEN."""
    existing = get_open_cycle(project_id, ticker)
    if existing:
        return existing["cycle_id"]
    with session_scope() as s:
        cycle_id = insert_returning_id(s, """
            INSERT INTO wheel_cycles (project_id, ticker, status)
            VALUES (:p, :t, 'OPEN')
        """, {"p": project_id, "t": ticker})
        s.commit()
        return cycle_id


def record_csp_sold(project_id: str, ticker: str, contract_id: int,
                    premium_dollars: float) -> int:
    cid = open_cycle(project_id, ticker)
    with session_scope() as s:
        s.execute(text("""
            UPDATE wheel_cycles
            SET csp_count = csp_count + 1,
                total_premium = total_premium + :p
            WHERE cycle_id = :cid
        """), {"cid": cid, "p": float(premium_dollars)})
        s.execute(text("""
            UPDATE wheel_contracts SET cycle_id = :cid
            WHERE contract_id = :con
        """), {"cid": cid, "con": int(contract_id)})
        s.commit()
    return cid


def record_cc_sold(project_id: str, ticker: str, contract_id: int,
                   premium_dollars: float) -> int | None:
    """Attach a CC to an OPEN cycle. CCs only happen after assignment, so an
    open cycle must already exist; if not, create one defensively."""
    cid = open_cycle(project_id, ticker)
    with session_scope() as s:
        s.execute(text("""
            UPDATE wheel_cycles
            SET cc_count = cc_count + 1,
                total_premium = total_premium + :p
            WHERE cycle_id = :cid
        """), {"cid": cid, "p": float(premium_dollars)})
        s.execute(text("""
            UPDATE wheel_contracts SET cycle_id = :cid
            WHERE contract_id = :con
        """), {"cid": cid, "con": int(contract_id)})
        s.commit()
    return cid


def record_assignment(project_id: str, ticker: str, strike: float,
                      quantity: int, premium_dollars: float) -> int | None:
    """Increment assignment count + lower cost basis by total premium so far.

    Adjusted cost basis = strike - (total_premium / (100 * quantity)).
    """
    cycle = get_open_cycle(project_id, ticker)
    if cycle is None:
        cid = open_cycle(project_id, ticker)
        cycle = {"cycle_id": cid, "total_premium": 0.0}
    cid = cycle["cycle_id"]
    total_prem = float(cycle["total_premium"]) + float(premium_dollars)
    shares = max(1, 100 * int(quantity))
    adjusted = float(strike) - (total_prem / shares)
    with session_scope() as s:
        s.execute(text("""
            UPDATE wheel_cycles
            SET assignment_count = assignment_count + 1,
                total_premium = :tp,
                cost_basis_adjusted = :cba
            WHERE cycle_id = :cid
        """), {"cid": cid, "tp": total_prem, "cba": adjusted})
        s.commit()
    return cid


def record_pnl(project_id: str, ticker: str, pnl_dollars: float) -> None:
    cycle = get_open_cycle(project_id, ticker)
    if cycle is None:
        return
    with session_scope() as s:
        s.execute(text("""
            UPDATE wheel_cycles
            SET realized_pnl = realized_pnl + :p
            WHERE cycle_id = :cid
        """), {"cid": cycle["cycle_id"], "p": float(pnl_dollars)})
        s.commit()


def close_cycle(project_id: str, ticker: str, *, outcome: str,
                final_exit_price: float | None = None) -> None:
    cycle = get_open_cycle(project_id, ticker)
    if cycle is None:
        return
    with session_scope() as s:
        s.execute(text("""
            UPDATE wheel_cycles
            SET status = 'CLOSED',
                ended_at = UTC_TIMESTAMP(),
                final_outcome = :fo,
                final_exit_price = :fep
            WHERE cycle_id = :cid
        """), {"cid": cycle["cycle_id"], "fo": outcome,
               "fep": final_exit_price})
        s.commit()


def list_cycles(project_id: str, *, status: str | None = None,
                limit: int = 50) -> list[dict[str, Any]]:
    where = ["project_id = :p"]
    params: dict[str, Any] = {"p": project_id, "lim": int(limit)}
    if status:
        where.append("status = :st")
        params["st"] = status
    sql = (
        "SELECT cycle_id, ticker, status, started_at, ended_at,"
        " total_premium, realized_pnl, csp_count, cc_count,"
        " assignment_count, cost_basis_adjusted, final_outcome,"
        " final_exit_price "
        "FROM wheel_cycles "
        f"WHERE {' AND '.join(where)} "
        "ORDER BY started_at DESC "
        "LIMIT :lim"
    )
    with session_scope() as s:
        rows = s.execute(text(sql), params).fetchall()
    out = []
    for r in rows:
        days = None
        try:
            started = r[3]
            ended = r[4] or _utcnow()
            if started and started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
            if ended and ended.tzinfo is None:
                ended = ended.replace(tzinfo=timezone.utc)
            days = max(0, (ended - started).days)
        except Exception:
            pass
        out.append({
            "cycle_id": int(r[0]), "ticker": r[1], "status": r[2],
            "started_at": r[3].isoformat() if r[3] else None,
            "ended_at": r[4].isoformat() if r[4] else None,
            "days_open": days,
            "total_premium": float(r[5]),
            "realized_pnl": float(r[6]),
            "csp_count": int(r[7]),
            "cc_count": int(r[8]),
            "assignment_count": int(r[9]),
            "cost_basis_adjusted": float(r[10]) if r[10] is not None else None,
            "final_outcome": r[11],
            "final_exit_price": float(r[12]) if r[12] is not None else None,
        })
    return out


def get_cycle(project_id: str, cycle_id: int) -> dict[str, Any] | None:
    with session_scope() as s:
        row = s.execute(text("""
            SELECT cycle_id, ticker, status, started_at, ended_at,
                   total_premium, realized_pnl, csp_count, cc_count,
                   assignment_count, cost_basis_adjusted, final_outcome,
                   final_exit_price
            FROM wheel_cycles
            WHERE cycle_id = :cid AND project_id = :p
        """), {"cid": int(cycle_id), "p": project_id}).fetchone()
        if not row:
            return None
        # Fetch contracts attached to this cycle
        contracts = s.execute(text("""
            SELECT contract_id, ticker, strategy_phase, option_symbol,
                   strike_price, premium_collected, expiration_date,
                   is_closed, is_assigned, opened_at, quantity
            FROM wheel_contracts
            WHERE cycle_id = :cid
            ORDER BY opened_at ASC
        """), {"cid": int(cycle_id)}).fetchall()
    return {
        "cycle_id": int(row[0]), "ticker": row[1], "status": row[2],
        "started_at": row[3].isoformat() if row[3] else None,
        "ended_at": row[4].isoformat() if row[4] else None,
        "total_premium": float(row[5]),
        "realized_pnl": float(row[6]),
        "csp_count": int(row[7]),
        "cc_count": int(row[8]),
        "assignment_count": int(row[9]),
        "cost_basis_adjusted": float(row[10]) if row[10] is not None else None,
        "final_outcome": row[11],
        "final_exit_price": float(row[12]) if row[12] is not None else None,
        "contracts": [{
            "contract_id": int(c[0]), "ticker": c[1],
            "strategy_phase": c[2], "option_symbol": c[3],
            "strike_price": float(c[4]),
            "premium_collected": float(c[5]),
            "expiration_date": str(c[6]) if c[6] else None,
            "is_closed": bool(c[7]),
            "is_assigned": bool(c[8]),
            "opened_at": c[9].isoformat() if c[9] else None,
            "quantity": int(c[10] or 1),
        } for c in contracts],
    }