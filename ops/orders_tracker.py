"""Order lifecycle tracker.

When the Executor submits an order, it calls `record_submission()`. The
poll loop (driven by APScheduler in the runner) periodically asks Alpaca
for the latest status of every non-terminal order and updates the row.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text

from db.connection import insert_returning_id, session_scope
from db.repositories import ProjectsRepo
from execution import AlpacaClient

logger = logging.getLogger(__name__)

_TERMINAL = {"filled", "canceled", "cancelled", "expired",
             "rejected", "done_for_day"}


def record_submission(project_id: str, *, alpaca_order_id: str,
                      symbol: str, side: str, order_type: str,
                      qty: float, limit_price: float | None,
                      status: str,
                      related_contract_id: int | None = None) -> int:
    is_term = status.lower() in _TERMINAL
    with session_scope() as s:
        existing = s.execute(text("""
            SELECT order_id FROM orders WHERE alpaca_order_id = :a
        """), {"a": alpaca_order_id}).fetchone()
        if existing:
            return int(existing[0])
        order_id = insert_returning_id(s, """
            INSERT INTO orders
                (project_id, alpaca_order_id, symbol, side, order_type,
                 qty, limit_price, status, terminal, related_contract_id)
            VALUES (:p, :a, :s, :sd, :ot, :q, :lp, :st, :t, :rcid)
        """, {"p": project_id, "a": alpaca_order_id, "s": symbol,
              "sd": side, "ot": order_type, "q": qty,
              "lp": limit_price, "st": status,
              "t": 1 if is_term else 0,
              "rcid": related_contract_id})
        s.commit()
        return order_id


def poll_orders(project_id: str) -> dict[str, Any]:
    project = ProjectsRepo.get(project_id)
    if project is None:
        return {"updated": 0, "error": "project not found"}
    with session_scope() as s:
        rows = s.execute(text("""
            SELECT order_id, alpaca_order_id
            FROM orders
            WHERE project_id = :p AND terminal = 0
        """), {"p": project_id}).fetchall()
    if not rows:
        return {"updated": 0}

    client = AlpacaClient(project)
    updated = 0
    errors: list[str] = []
    for row in rows:
        order_id, alpaca_id = int(row[0]), row[1]
        try:
            o = client.trading.get_order_by_id(alpaca_id)
            status = str(o.status.value if hasattr(o.status, "value") else o.status).lower()
            filled_qty = float(o.filled_qty or 0)
            avg = float(o.filled_avg_price) if o.filled_avg_price else None
            is_term = status in _TERMINAL
            with session_scope() as s:
                s.execute(text("""
                    UPDATE orders
                    SET status = :st, filled_qty = :fq,
                        filled_avg_price = :avg,
                        last_polled_at = UTC_TIMESTAMP(),
                        terminal = :t
                    WHERE order_id = :oid
                """), {"st": status, "fq": filled_qty, "avg": avg,
                       "t": 1 if is_term else 0, "oid": order_id})
                s.commit()
            updated += 1
        except Exception as e:
            errors.append(f"{alpaca_id}: {e}")
            with session_scope() as s:
                s.execute(text("""
                    UPDATE orders
                    SET last_polled_at = UTC_TIMESTAMP(),
                        last_error = :err
                    WHERE order_id = :oid
                """), {"err": str(e)[:500], "oid": order_id})
                s.commit()
    return {"updated": updated, "errors": errors}


def list_orders(project_id: str, *, limit: int = 100,
                terminal: bool | None = None) -> list[dict[str, Any]]:
    where = ["project_id = :p"]
    params: dict[str, Any] = {"p": project_id, "lim": int(limit)}
    if terminal is not None:
        where.append("terminal = :t")
        params["t"] = 1 if terminal else 0
    sql = (
        "SELECT TOP (:lim) order_id, alpaca_order_id, symbol, side,"
        " order_type, qty, limit_price, status, filled_qty,"
        " filled_avg_price, submitted_at, last_polled_at, terminal,"
        " last_error "
        "FROM orders "
        f"WHERE {' AND '.join(where)} "
        "ORDER BY order_id DESC"
    )
    with session_scope() as s:
        rows = s.execute(text(sql), params).fetchall()
    return [{
        "order_id": int(r[0]),
        "alpaca_order_id": r[1],
        "symbol": r[2],
        "side": r[3],
        "order_type": r[4],
        "qty": float(r[5]),
        "limit_price": float(r[6]) if r[6] is not None else None,
        "status": r[7],
        "filled_qty": float(r[8]),
        "filled_avg_price": float(r[9]) if r[9] is not None else None,
        "submitted_at": r[10].isoformat() if r[10] else None,
        "last_polled_at": r[11].isoformat() if r[11] else None,
        "terminal": bool(r[12]),
        "last_error": r[13],
    } for r in rows]
