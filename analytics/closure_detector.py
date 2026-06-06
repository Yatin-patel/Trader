"""Detects when open contracts/positions have closed on Alpaca's side
and records them into closed_contracts / closed_positions.

The detector is deliberately conservative — when in doubt about the closure
reason, it picks the most benign label. The premium captured / realized P&L
math is straightforward:

    realized_pnl = premium_collected * 100 * quantity - close_cost

For contracts that simply expired worthless, close_cost = 0.
For bought-to-close, close_cost = exit fill price * 100 * qty.
For assignment, close_cost = 0 (the contract was exercised, not bought back).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text

from db.analytics_repos import ClosedContractsRepo, ClosedPositionsRepo
from db.connection import session_scope
from db.repositories import EventsRepo, PositionsRepo, ProjectsRepo, WheelRepo
from execution import AlpacaClient

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


def _aware(dt):
    """SQL Server returns naive datetimes; assume UTC and attach tzinfo."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _alpaca_option_set(positions: list[dict[str, Any]]) -> set[str]:
    """Set of option symbols currently held on Alpaca."""
    return {p["symbol"] for p in positions if p.get("asset_class") != "us_equity"}


def _alpaca_equity_map(positions: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {p["symbol"]: p for p in positions if p.get("asset_class") == "us_equity"}


def detect_closures(project_id: str) -> dict[str, int]:
    """Run a single closure-detection pass for one project.

    Returns a dict with counts of detected closures by type.
    """
    project = ProjectsRepo.get(project_id)
    if project is None:
        return {"contracts": 0, "positions": 0}

    try:
        client = AlpacaClient(project)
        live_positions = client.list_positions()
    except Exception as e:
        logger.warning("closure detector: alpaca fetch failed for %s: %s", project_id, e)
        return {"contracts": 0, "positions": 0, "error": str(e)}

    closed_contracts = 0
    closed_positions = 0

    # ---- Contracts ----
    live_options = _alpaca_option_set(live_positions)
    live_equities = _alpaca_equity_map(live_positions)
    open_contracts = WheelRepo.list_open(project_id)
    for c in open_contracts:
        symbol = c.get("option_symbol")
        if not symbol:
            continue
        if symbol in live_options:
            continue   # still open
        # Contract is gone from Alpaca → it closed. Decide why.
        ticker = c["ticker"]
        opened_at = _aware(c["opened_at"]) or _utcnow()
        quantity = int(c.get("quantity") or 1)
        strike = float(c["strike_price"])
        premium_per_contract = float(c["premium_collected"])
        gross_premium = premium_per_contract * 100.0 * quantity

        # Heuristic: if equity shares appeared in matching qty since opened_at,
        # likely assigned. Otherwise treat as expired worthless.
        reason = "EXPIRED"
        if c["strategy_phase"] == "CASH_SECURED_PUT":
            held = live_equities.get(ticker)
            if held and float(held.get("qty") or 0) >= (100 * quantity):
                reason = "ASSIGNED"
        elif c["strategy_phase"] == "COVERED_CALL":
            held = live_equities.get(ticker)
            if not held or float(held.get("qty") or 0) < (100 * quantity):
                reason = "CALLED_AWAY"

        closed_at_ts = _utcnow()
        ClosedContractsRepo.insert(
            project_id=project_id,
            contract_id=c.get("contract_id"),
            ticker=ticker,
            option_symbol=symbol,
            strategy_phase=c["strategy_phase"],
            opened_at=opened_at,
            closed_at=closed_at_ts,
            strike_price=strike,
            quantity=quantity,
            premium_collected=gross_premium,
            close_cost=0.0,
            closure_reason=reason,
            delta_at_entry=c.get("delta_at_entry"),
            underlying_at_entry=c.get("underlying_at_entry"),
            settings_snapshot=c.get("settings_snapshot"),
        )
        # PDT day-trade tracking: any same-day open+close counts
        # against the FINRA 4-in-5-days cap on sub-$25k accounts.
        try:
            from risk.pdt_guard import log_same_day_closure
            log_same_day_closure(
                project_id=project_id,
                symbol=symbol or ticker,
                opened_at=opened_at,
                closed_at=closed_at_ts,
            )
        except Exception:
            logger.exception("PDT same-day log failed for %s", symbol)

        # Mark wheel_contracts.is_closed = 1
        with session_scope() as s:
            s.execute(text("""
                UPDATE wheel_contracts
                SET is_closed = 1, updated_at = UTC_TIMESTAMP()
                WHERE contract_id = :c
            """), {"c": c["contract_id"]})
            s.commit()

        # ---- Wheel cycle bookkeeping -----------------------------------
        try:
            from analytics.wheel_cycles import (close_cycle, record_assignment,
                                                record_pnl)
            # On assignment, store adjusted cost basis on stock_positions and
            # record the assignment against the open cycle.
            if reason == "ASSIGNED" and c["strategy_phase"] == "CASH_SECURED_PUT":
                # Compute adjusted basis from cycle so far.
                from analytics.wheel_cycles import get_open_cycle
                record_assignment(project_id, ticker, strike, quantity,
                                  gross_premium)
                cycle = get_open_cycle(project_id, ticker)
                adjusted = cycle["cost_basis_adjusted"] if cycle else None
                # Open a stock_positions row for the assigned shares.
                from db.connection import session_scope as _ss
                with _ss() as s:
                    s.execute(text("""
                        INSERT INTO stock_positions
                            (project_id, ticker, entry_price, current_price,
                             max_loss_threshold, quantity, status,
                             adjusted_cost_basis)
                        VALUES (:p, :t, :ep, :ep, :mlt, :q, 'OPEN', :acb)
                    """), {
                        "p": project_id, "t": ticker, "ep": strike,
                        "mlt": (adjusted if adjusted is not None else strike) - 2.0,
                        "q": 100 * quantity, "acb": adjusted,
                    })
                    s.commit()
            elif reason == "CALLED_AWAY":
                # Stock leaves at strike; cycle is done.
                record_pnl(project_id, ticker, gross_premium)
                close_cycle(project_id, ticker, outcome="CALLED_AWAY",
                            final_exit_price=strike)
            elif reason == "EXPIRED" and c["strategy_phase"] == "CASH_SECURED_PUT":
                # CSP expired worthless — premium kept, cycle may continue.
                record_pnl(project_id, ticker, gross_premium)
            elif reason == "EXPIRED" and c["strategy_phase"] == "COVERED_CALL":
                # CC expired worthless — keep premium, cycle continues with
                # shares still on the books for the next CC.
                record_pnl(project_id, ticker, gross_premium)
        except Exception:
            logger.exception("wheel-cycle hook failed on closure")

        EventsRepo.log(project_id, "Analytics", "CLOSURE", {
            "kind": "contract",
            "ticker": ticker,
            "phase": c["strategy_phase"],
            "reason": reason,
            "realized_pnl": gross_premium,
            "quantity": quantity,
            "narrative": [
                f"{ticker} {c['strategy_phase']} (strike ${strike:.2f}) closed: {reason}.",
                f"Premium captured: ${gross_premium:.2f} across {quantity} contract(s).",
            ],
        })
        closed_contracts += 1

    # ---- Equity positions ----
    open_positions = PositionsRepo.list_open(project_id)
    for p in open_positions:
        ticker = p["ticker"]
        if ticker in live_equities:
            continue
        entry = float(p["entry_price"])
        qty = int(p["quantity"])
        opened_at = _aware(p.get("opened_at")) or _utcnow()
        # Exit price: use last price if we have it, else entry (we lost track)
        exit_price = float(p.get("current_price") or entry)
        reason = p.get("status") or "SOLD"
        if reason == "OPEN":
            reason = "SOLD"
        ClosedPositionsRepo.insert(
            project_id=project_id,
            position_id=p["position_id"],
            ticker=ticker,
            quantity=qty,
            entry_price=entry,
            exit_price=exit_price,
            opened_at=opened_at,
            closed_at=_utcnow(),
            closure_reason=reason,
        )
        PositionsRepo.close(p["position_id"], final_status=reason)
        EventsRepo.log(project_id, "Analytics", "CLOSURE", {
            "kind": "position",
            "ticker": ticker,
            "reason": reason,
            "realized_pnl": (exit_price - entry) * qty,
        })
        closed_positions += 1

    return {"contracts": closed_contracts, "positions": closed_positions}
