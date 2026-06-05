"""Intraday Scanner Agent for day trading signals.

Scans the market for intraday opportunities using RSI, MACD, and VWAP indicators.
Integrates with the 0DTE/1DTE options strategy.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from analytics.intraday_signals import generate_intraday_signal, scan_intraday_opportunities
from db.repositories import EventsRepo, ProjectsRepo
from db.settings_store import ProjectSettings
from execution import get_broker

logger = logging.getLogger(__name__)


def intraday_scan_node(state: dict[str, Any]) -> dict[str, Any]:
    """LangGraph node for intraday signal scanning.

    Runs the intraday scanner and filters results based on project settings.
    Only activates if intraday_scanner_enabled is True.

    Args:
        state: Pipeline state with project_id

    Returns:
        Updated state with intraday_signals
    """
    project_id = state["project_id"]
    project = ProjectsRepo.get(project_id)
    if project is None:
        return {"intraday_signals": []}

    # Check if intraday scanner is enabled
    scanner_enabled = bool(ProjectSettings.get(project_id, "intraday_scanner_enabled", False))
    if not scanner_enabled:
        return {"intraday_signals": []}

    # Check if market is open
    client = get_broker(project)
    if not client.is_market_open():
        logger.debug("Market closed, skipping intraday scan")
        return {"intraday_signals": []}

    try:
        signals = scan_intraday_opportunities(project_id)

        # Log the scan results
        EventsRepo.log(project_id, "IntradayScanner", "SCAN", {
            "signals_found": len(signals),
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
            "top_signals": signals[:5],  # Log top 5
        })

        return {"intraday_signals": signals}

    except Exception as e:
        logger.exception("Intraday scan failed: %s", e)
        EventsRepo.log(project_id, "IntradayScanner", "ERROR", {"err": str(e)})
        return {"intraday_signals": []}


def evaluate_0dte_opportunity(
    project_id: str,
    ticker: str,
    signal: dict[str, Any]
) -> dict[str, Any] | None:
    """Evaluate a 0DTE options trade opportunity based on intraday signal.

    Args:
        project_id: Trading project ID
        ticker: Stock symbol
        signal: Intraday signal from scanner

    Returns:
        Trade opportunity dict or None if not viable
    """
    project = ProjectsRepo.get(project_id)
    if project is None:
        return None

    # Check 0DTE settings
    allow_0dte = bool(ProjectSettings.get(project_id, "allow_0dte", False))
    allow_1dte = bool(ProjectSettings.get(project_id, "allow_1dte", False))

    if not allow_0dte and not allow_1dte:
        return None

    # Strong signals only for 0DTE
    if signal["strength"] < 0.7:
        return None

    client = get_broker(project)

    try:
        snap = client.snapshots([ticker]).get(ticker)
        if snap is None or snap.last_price <= 0:
            return None

        # Determine trade direction
        if signal["signal"] == "BUY":
            contract_type = "call"
            side = "buy"
        elif signal["signal"] == "SELL":
            contract_type = "put"
            side = "buy"
        else:
            return None

        # Get 0DTE or 1DTE contracts
        min_dte = 0 if allow_0dte else 1
        max_dte = 1 if allow_1dte else 0

        # ATM or slightly OTM strikes
        if contract_type == "call":
            min_strike = snap.last_price * 0.99
            max_strike = snap.last_price * 1.02
        else:
            min_strike = snap.last_price * 0.98
            max_strike = snap.last_price * 1.01

        contracts = client.list_option_contracts(
            ticker, contract_type,
            min_dte=min_dte, max_dte=max_dte,
            min_strike=min_strike, max_strike=max_strike,
            limit=10
        )

        if not contracts:
            return None

        # Get quotes and select best contract
        quotes = client.option_chain_quotes(ticker)

        best_contract = None
        best_score = -1

        for c in contracts:
            sym = c["symbol"]
            q = quotes.get(sym, {})

            bid = q.get("bid") or 0
            ask = q.get("ask") or 0

            if bid <= 0 or ask <= 0:
                continue

            mid = (bid + ask) / 2
            spread_ratio = (ask - bid) / mid if mid > 0 else 1

            # Skip wide spreads
            if spread_ratio > 0.20:
                continue

            # Score based on liquidity and delta
            delta = abs(q.get("delta") or 0)
            oi = c.get("open_interest", 0)

            score = (1 - spread_ratio) * 0.4 + min(oi / 1000, 1) * 0.3 + (1 - abs(delta - 0.40)) * 0.3

            if score > best_score:
                best_score = score
                best_contract = {
                    **c,
                    **q,
                    "mid": mid,
                    "spread_ratio": spread_ratio,
                }

        if best_contract is None:
            return None

        profit_target = float(ProjectSettings.get(project_id, "0dte_profit_target_pct", 0.30))
        stop_loss = float(ProjectSettings.get(project_id, "0dte_stop_loss_pct", 0.50))

        return {
            "ticker": ticker,
            "type": "0DTE" if min_dte == 0 else "1DTE",
            "direction": signal["signal"],
            "contract_type": contract_type,
            "option_symbol": best_contract["symbol"],
            "strike": best_contract["strike"],
            "expiration": str(best_contract["expiration"]),
            "delta": best_contract.get("delta"),
            "entry_price": best_contract["mid"],
            "underlying_price": snap.last_price,
            "profit_target_pct": profit_target,
            "stop_loss_pct": stop_loss,
            "signal_strength": signal["strength"],
            "rationale": signal["rationale"],
        }

    except Exception as e:
        logger.warning("Failed to evaluate 0DTE for %s: %s", ticker, e)
        return None
