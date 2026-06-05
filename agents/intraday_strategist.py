"""Intraday-momentum strategist.

Routed to from agents.strategy_dispatcher when strategy_mode is
``intraday_momentum``. Pipeline:

1. Run the intraday scanner (RSI + MACD + VWAP) over the project's
   watchlist + the Scanner's pre-filtered tickers — whichever gave us
   more candidates.
2. For each signal whose strength is over the per-project threshold,
   evaluate a 0DTE or 1DTE long-option opportunity via
   :func:`agents.intraday_scanner.evaluate_0dte_opportunity`.
3. Convert each opportunity into the standard trade dict the executor
   consumes — trade type ``INTRADAY_LONG_CALL`` or
   ``INTRADAY_LONG_PUT``.

The PDT cap is enforced downstream in the Executor (so a partial fill
that consumes the day-trade budget still gets logged). Net-vega and
collateral checks remain in the Guardrail.
"""
from __future__ import annotations

import logging
from typing import Any

from db.repositories import EventsRepo, ProjectsRepo
from db.settings_store import ProjectSettings

from .intraday_scanner import evaluate_0dte_opportunity, intraday_scan_node

logger = logging.getLogger(__name__)


def analyze_intraday_node(state: dict[str, Any]) -> dict[str, Any]:
    project_id = state["project_id"]
    project = ProjectsRepo.get(project_id)
    if project is None:
        return {"selected_trades": []}

    # Defensive: surface a clean DECIDE event so the activity feed
    # explains why no trades were picked when settings preclude it.
    allow_0dte = bool(ProjectSettings.get(project_id, "allow_0dte", False))
    allow_1dte = bool(ProjectSettings.get(project_id, "allow_1dte", False))
    if not (allow_0dte or allow_1dte):
        EventsRepo.log(project_id, "Strategist", "DECIDE", {
            "mode": "intraday_momentum",
            "skipped": "0dte_and_1dte_both_disabled",
            "narrative": [
                "intraday_momentum mode is on, but neither allow_0dte "
                "nor allow_1dte is enabled. No trades selected this "
                "cycle.",
            ],
        })
        return {"selected_trades": []}

    # Make sure scanner is on for this project; if it isn't, flip the
    # toggle implicitly when the project picked intraday_momentum as
    # their strategy_mode — the user already opted in.
    if not bool(ProjectSettings.get(
            project_id, "intraday_scanner_enabled", False)):
        try:
            ProjectSettings.set(
                project_id, "intraday_scanner_enabled", True)
        except Exception:
            logger.exception(
                "failed to auto-enable intraday_scanner_enabled")

    scan_state = intraday_scan_node(state)
    signals = scan_state.get("intraday_signals") or []
    if not signals:
        EventsRepo.log(project_id, "Strategist", "DECIDE", {
            "mode": "intraday_momentum",
            "skipped": "no_intraday_signals",
            "narrative": [
                "Intraday scanner returned 0 signals this cycle.",
            ],
        })
        return {"selected_trades": []}

    # Limit how many trades we open in a single cycle.
    max_per_cycle = int(ProjectSettings.get(
        project_id, "intraday_max_trades_per_cycle", default=3) or 3)

    trades: list[dict[str, Any]] = []
    rejections: list[dict[str, Any]] = []

    for sig in signals:
        if len(trades) >= max_per_cycle:
            break
        ticker = sig.get("ticker") or sig.get("symbol")
        if not ticker:
            continue
        opp = evaluate_0dte_opportunity(project_id, ticker, sig)
        if opp is None:
            rejections.append({
                "ticker": ticker,
                "reason": "no 0DTE/1DTE contract met the liquidity + delta gate",
            })
            continue

        direction = (opp.get("contract_type") or "").lower()
        trade_type = ("INTRADAY_LONG_CALL" if direction == "call"
                      else "INTRADAY_LONG_PUT")
        trade = {
            "ticker": ticker,
            "type": trade_type,
            "option_symbol": opp["option_symbol"],
            "strike": opp.get("strike"),
            "expiration": opp.get("expiration"),
            "delta": opp.get("delta"),
            "premium": opp.get("entry_price"),
            "underlying_price": opp.get("underlying_price"),
            "rationale": opp.get("rationale"),
            "signal_strength": opp.get("signal_strength"),
            "profit_target_pct": opp.get("profit_target_pct"),
            "stop_loss_pct": opp.get("stop_loss_pct"),
            "intraday_dte_label": opp.get("type"),
        }
        trades.append(trade)
        EventsRepo.log(project_id, "Strategist", "SELECTION", {
            "ticker": ticker,
            "kind": trade_type,
            "outcome": "approved",
            "underlying_price": opp.get("underlying_price"),
            "narrative": [
                f"Intraday signal {sig.get('signal')} (strength "
                f"{sig.get('strength')}): {opp.get('rationale', '')}. "
                f"Picked {opp.get('option_symbol')} @ "
                f"${opp.get('entry_price')} (Δ "
                f"{opp.get('delta')}).",
            ],
        })

    EventsRepo.log(project_id, "Strategist", "DECIDE", {
        "mode": "intraday_momentum",
        "candidates": [s.get("ticker") for s in signals],
        "selected": [t["ticker"] for t in trades],
        "rejections": rejections,
    })
    return {"selected_trades": trades}
