"""Trade journal CSV export (Cat 11.1)."""
from __future__ import annotations

import csv
import io
from datetime import datetime, timezone

from db.analytics_repos import ClosedContractsRepo, ClosedPositionsRepo


def trade_journal_csv(project_id: str, *, since: datetime | None = None) -> str:
    contracts = ClosedContractsRepo.list(project_id, since=since, limit=100000)
    positions = ClosedPositionsRepo  # noqa
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow([
        "kind", "ticker", "option_symbol", "strategy_phase",
        "opened_at", "closed_at", "days_held",
        "strike_price", "quantity",
        "premium_collected", "close_cost", "realized_pnl",
        "closure_reason", "delta_at_entry", "dte_at_entry",
        "underlying_at_entry", "underlying_at_close",
    ])
    for c in contracts:
        w.writerow([
            "option", c.get("ticker"), c.get("option_symbol"),
            c.get("strategy_phase"),
            c.get("opened_at"), c.get("closed_at"), c.get("days_held"),
            c.get("strike_price"), c.get("quantity"),
            c.get("premium_collected"), c.get("close_cost"),
            c.get("realized_pnl"), c.get("closure_reason"),
            c.get("delta_at_entry"), c.get("dte_at_entry"),
            c.get("underlying_at_entry"), c.get("underlying_at_close"),
        ])
    # Closed positions
    from sqlalchemy import text
    from db.connection import session_scope
    where = ["project_id = :p"]
    params: dict = {"p": project_id}
    if since:
        where.append("closed_at >= :since")
        params["since"] = since
    with session_scope() as s:
        rows = s.execute(text(
            "SELECT ticker, quantity, entry_price, exit_price, opened_at, "
            "closed_at, days_held, realized_pnl, closure_reason "
            f"FROM closed_positions WHERE {' AND '.join(where)}"
        ), params).fetchall()
    for r in rows:
        w.writerow([
            "stock", r[0], "", "STOCK",
            r[4], r[5], r[6],
            "", r[1],
            "", "", r[7], r[8], "", "", r[2], r[3],
        ])
    return out.getvalue()
