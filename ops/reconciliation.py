"""Position reconciliation — compare DB ground truth vs Alpaca.

Mismatches we detect:
  * DB has an OPEN wheel_contract that Alpaca no longer holds
  * Alpaca holds an option position that has no matching wheel_contracts row
  * DB has an OPEN stock_positions row that Alpaca no longer holds
  * Alpaca holds shares (>= 100) that have no matching stock_positions row

When `auto_sync` is true and a DB row is missing from Alpaca, mark it CLOSED
so the rest of the system doesn't keep waiting on a phantom contract.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy import text

from db.connection import session_scope
from db.repositories import EventsRepo, PositionsRepo, ProjectsRepo, WheelRepo
from db.settings_store import ProjectSettings
from execution import AlpacaClient

logger = logging.getLogger(__name__)


def _record(project_id: str, mismatches: list[dict[str, Any]],
            auto_sync: bool) -> int:
    with session_scope() as s:
        row = s.execute(text("""
            INSERT INTO reconciliation_log
                (project_id, mismatches, auto_sync, details)
            OUTPUT INSERTED.recon_id
            VALUES (:p, :n, :as, :d)
        """), {"p": project_id, "n": len(mismatches),
               "as": 1 if auto_sync else 0,
               "d": json.dumps(mismatches, default=str)}).fetchone()
        s.commit()
        return int(row[0])


def run_reconciliation(project_id: str, *, auto_sync: bool | None = None) -> dict[str, Any]:
    project = ProjectsRepo.get(project_id)
    if project is None:
        return {"error": "project not found"}
    if auto_sync is None:
        auto_sync = bool(ProjectSettings.get(project_id, "reconcile_auto_sync",
                                             default=False))

    try:
        client = AlpacaClient(project)
        live = client.list_positions()
    except Exception as e:
        return {"error": str(e), "mismatches": []}

    alpaca_options = {p["symbol"]: p for p in live
                      if p.get("asset_class") != "us_equity"}
    alpaca_equities = {p["symbol"]: p for p in live
                       if p.get("asset_class") == "us_equity"}

    open_contracts = WheelRepo.list_open(project_id)
    open_positions = PositionsRepo.list_open(project_id)

    mismatches: list[dict[str, Any]] = []

    # --- DB contract that Alpaca no longer holds -----------------------
    for c in open_contracts:
        sym = c.get("option_symbol")
        if not sym:
            continue
        if sym not in alpaca_options:
            entry = {
                "type": "contract_missing_in_alpaca",
                "ticker": c["ticker"],
                "option_symbol": sym,
                "contract_id": c["contract_id"],
                "strategy_phase": c["strategy_phase"],
            }
            mismatches.append(entry)
            if auto_sync:
                with session_scope() as s:
                    s.execute(text("""
                        UPDATE wheel_contracts
                        SET is_closed = 1, updated_at = UTC_TIMESTAMP()
                        WHERE contract_id = :c
                    """), {"c": c["contract_id"]})
                    s.commit()
                entry["action"] = "marked_closed"

    # --- Alpaca option that DB doesn't track --------------------------
    tracked_syms = {c.get("option_symbol") for c in open_contracts}
    for sym, p in alpaca_options.items():
        if sym in tracked_syms:
            continue
        mismatches.append({
            "type": "alpaca_option_untracked",
            "symbol": sym,
            "qty": p.get("qty"),
        })

    # --- DB stock_positions vs Alpaca shares --------------------------
    for pos in open_positions:
        if pos["ticker"] not in alpaca_equities:
            entry = {
                "type": "position_missing_in_alpaca",
                "ticker": pos["ticker"],
                "position_id": pos["position_id"],
                "quantity": pos["quantity"],
            }
            mismatches.append(entry)
            if auto_sync:
                PositionsRepo.close(pos["position_id"], final_status="CLOSED")
                entry["action"] = "marked_closed"

    tracked_tickers = {p["ticker"] for p in open_positions}
    for tk, p in alpaca_equities.items():
        if tk in tracked_tickers:
            continue
        try:
            qty = float(p.get("qty") or 0)
        except Exception:
            qty = 0
        if qty < 100:
            continue   # too small to be a wheel position; ignore
        mismatches.append({
            "type": "alpaca_shares_untracked",
            "ticker": tk,
            "qty": qty,
        })

    recon_id = _record(project_id, mismatches, auto_sync)
    EventsRepo.log(project_id, "Ops", "RECONCILE", {
        "recon_id": recon_id,
        "mismatch_count": len(mismatches),
        "auto_sync": auto_sync,
        "narrative": [
            f"Reconciled DB vs Alpaca: {len(mismatches)} mismatch(es) "
            f"({'auto-sync ON' if auto_sync else 'auto-sync OFF'}).",
        ],
    })
    return {"recon_id": recon_id, "mismatches": mismatches,
            "auto_sync": auto_sync}


def list_recon_history(project_id: str, limit: int = 20) -> list[dict[str, Any]]:
    with session_scope() as s:
        rows = s.execute(text("""
            SELECT TOP (:lim) recon_id, ran_at, mismatches, auto_sync, details
            FROM reconciliation_log
            WHERE project_id = :p
            ORDER BY ran_at DESC
        """), {"p": project_id, "lim": int(limit)}).fetchall()
    out = []
    for r in rows:
        try:
            details = json.loads(r[4]) if r[4] else []
        except Exception:
            details = []
        out.append({
            "recon_id": int(r[0]),
            "ran_at": r[1].isoformat() if r[1] else None,
            "mismatches": int(r[2]),
            "auto_sync": bool(r[3]),
            "details": details,
        })
    return out
