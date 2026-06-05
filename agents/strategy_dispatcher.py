"""Strategy dispatcher — single graph node that routes between strategies.

Replaces the hard-coded `analyze_wheel_node` slot in the graph. Reads
`strategy_mode` for the project and routes:

  wheel | wheel_plus_dca | dca_only (legacy)  → analyze_wheel_node
  bull_put_spread                              → analyze_spread_node('BULL_PUT_SPREAD')
  bear_call_spread                             → analyze_spread_node('BEAR_CALL_SPREAD')
  bull_call_spread                             → analyze_spread_node('BULL_CALL_SPREAD')
  bear_put_spread                              → analyze_spread_node('BEAR_PUT_SPREAD')
  iron_condor                                  → analyze_iron_condor_node
  calendar_spread                              → analyze_calendar_spread_node
  intraday_momentum                            → analyze_intraday_node

The dispatcher does NOT duplicate wheel-level filters (earnings, IV-rank,
news sentiment, economic-event gate, recent-failure backoff) — those are
specific to the wheel and the strategist owns them. Spread / intraday
strategies have their own selection logic embedded in their
`find_setup()` methods. Risk Guardrail still runs after this node in
every case.
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Any

from db.repositories import EventsRepo, ProjectsRepo
from db.settings_store import ProjectSettings

from .strategist import analyze_wheel_node

logger = logging.getLogger(__name__)


# Map UI strategy_mode strings to (strategy_class_module, trade_type label).
# The dispatcher imports the strategy class lazily so a missing optional
# dep doesn't break import of this module.
_SPREAD_DISPATCH: dict[str, tuple[str, str, str]] = {
    "bull_put_spread":  ("strategies.vertical_spreads",
                         "BullPutSpreadStrategy",  "BULL_PUT_SPREAD"),
    "bear_call_spread": ("strategies.vertical_spreads",
                         "BearCallSpreadStrategy", "BEAR_CALL_SPREAD"),
    "bull_call_spread": ("strategies.vertical_spreads",
                         "BullCallSpreadStrategy", "BULL_CALL_SPREAD"),
    "bear_put_spread":  ("strategies.vertical_spreads",
                         "BearPutSpreadStrategy",  "BEAR_PUT_SPREAD"),
    "iron_condor":      ("strategies.iron_condor",
                         "IronCondorStrategy",     "IRON_CONDOR"),
    "calendar_spread":  ("strategies.calendar_spread",
                         "CalendarSpreadStrategy", "CALENDAR_SPREAD"),
}


def _serialize_expiration(exp: Any) -> str | None:
    if exp is None:
        return None
    if isinstance(exp, date):
        return exp.isoformat()
    return str(exp)


def _setup_to_trade(setup: dict[str, Any], trade_type: str) -> dict[str, Any]:
    """Convert a strategy `find_setup()` dict into the standard trade
    shape the executor consumes. The full setup is preserved under
    ``setup`` so the executor can hand it back to the strategy class's
    ``execute()`` method without rebuilding leg lists.

    The trade dict carries enough surface fields (ticker, premium,
    underlying_price, expiration, strikes) for the Guardrail's collateral
    and net-vega checks to read without unwrapping ``setup``."""
    trade: dict[str, Any] = {
        "ticker": setup.get("ticker"),
        "type": trade_type,
        "underlying_price": setup.get("underlying_price"),
        "expiration": _serialize_expiration(setup.get("expiration")),
        "setup": setup,
    }

    # Two-leg verticals + calendars: lift short_leg / long_leg.
    if "short_leg" in setup and "long_leg" in setup:
        short = setup["short_leg"] or {}
        long_ = setup["long_leg"] or {}
        # For credit spreads the "premium" the user collects net is
        # net_credit; for debit spreads it's the net_debit (negative).
        if "net_credit" in setup:
            premium = float(setup["net_credit"])
        elif "net_debit" in setup:
            premium = -float(setup["net_debit"])
        else:
            premium = (float(short.get("mid") or 0)
                       - float(long_.get("mid") or 0)) * 100.0
        trade.update({
            "option_symbol": short.get("symbol") or long_.get("symbol"),
            "strike":        short.get("strike") or long_.get("strike"),
            "short_leg":     short,
            "long_leg":      long_,
            "premium":       round(premium, 2),
            "net_credit":    setup.get("net_credit"),
            "net_debit":     setup.get("net_debit"),
            "max_loss":      setup.get("max_loss"),
            "max_profit":    setup.get("max_profit"),
            "width":         setup.get("width"),
        })

    # Four-leg iron condor: keep all legs together so the executor
    # can submit them via IronCondorStrategy.execute().
    if "legs" in setup:
        legs = setup["legs"] or {}
        trade.update({
            "option_symbol": (
                (legs.get("short_put") or {}).get("symbol")
                or (legs.get("short_call") or {}).get("symbol")
            ),
            "strike": (
                (legs.get("short_put") or {}).get("strike")
                or (legs.get("short_call") or {}).get("strike")
            ),
            "legs":          legs,
            "premium":       setup.get("net_credit"),
            "net_credit":    setup.get("net_credit"),
            "max_loss":      setup.get("max_loss"),
            "max_profit":    setup.get("max_profit"),
        })

    return trade


def _analyze_spread_node(state: dict[str, Any], mode: str) -> dict[str, Any]:
    """Run a 2-leg vertical or 4-leg iron condor over every candidate
    ticker the Scanner surfaced. Each setup that survives the strategy's
    own filters becomes one trade in `selected_trades`."""
    project_id = state["project_id"]
    tickers: list[str] = state.get("target_tickers") or []
    if not tickers:
        return {"selected_trades": []}

    dispatch = _SPREAD_DISPATCH.get(mode)
    if dispatch is None:
        # Should be unreachable — graph dispatcher already validated.
        return {"selected_trades": []}
    module_name, class_name, trade_type = dispatch

    project = ProjectsRepo.get(project_id)
    if project is None:
        return {"selected_trades": []}

    try:
        module = __import__(module_name, fromlist=[class_name])
        StrategyCls = getattr(module, class_name)
    except Exception:
        logger.exception("strategy class %s.%s not importable",
                         module_name, class_name)
        return {"selected_trades": []}

    try:
        strat = StrategyCls(project_id)
    except Exception as e:
        EventsRepo.log(project_id, "Strategist", "ERROR", {
            "err": f"failed to instantiate {class_name}: {e}",
            "mode": mode,
        })
        return {"selected_trades": []}

    # Per-project tuning knobs (fall back to the strategy class defaults).
    target_delta = float(ProjectSettings.get(
        project_id, "spread_target_delta", default=0.25) or 0.25)
    wing_or_width = float(ProjectSettings.get(
        project_id, "spread_width", default=5.0) or 5.0)
    min_dte = int(ProjectSettings.get(
        project_id, "spread_min_dte", default=21) or 21)
    max_dte = int(ProjectSettings.get(
        project_id, "spread_max_dte", default=45) or 45)
    max_concurrent = int(ProjectSettings.get(
        project_id, "max_open_contracts", default=10) or 10)

    trades: list[dict[str, Any]] = []
    rejections: list[dict[str, Any]] = []

    for ticker in tickers:
        if len(trades) >= max_concurrent:
            break
        try:
            if mode == "iron_condor":
                setup = strat.find_setup(
                    ticker,
                    target_delta=target_delta,
                    wing_width=wing_or_width,
                    min_dte=min_dte,
                    max_dte=max_dte,
                )
            elif mode == "calendar_spread":
                opt_type = str(ProjectSettings.get(
                    project_id, "calendar_option_type",
                    default="call") or "call")
                short_dte = int(ProjectSettings.get(
                    project_id, "calendar_short_dte", default=14) or 14)
                long_dte = int(ProjectSettings.get(
                    project_id, "calendar_long_dte", default=45) or 45)
                setup = strat.find_setup(
                    ticker,
                    option_type=opt_type,
                    short_dte=short_dte,
                    long_dte=long_dte,
                )
            else:
                setup = strat.find_setup(
                    ticker,
                    target_delta=target_delta,
                    spread_width=wing_or_width,
                    min_dte=min_dte,
                    max_dte=max_dte,
                )
        except Exception as e:
            logger.exception(
                "%s.find_setup failed for %s: %s",
                class_name, ticker, e,
            )
            rejections.append({"ticker": ticker, "reason": str(e)[:200]})
            continue

        if setup is None:
            rejections.append({"ticker": ticker, "reason": "no viable setup"})
            continue

        trade = _setup_to_trade(setup, trade_type)
        # Skip setups whose credit/debit math went negative — almost
        # always means the chain is stale or the spread crosses the
        # market the wrong way.
        if mode in ("bull_put_spread", "bear_call_spread", "iron_condor"):
            if (trade.get("net_credit") or 0) <= 0:
                rejections.append({
                    "ticker": ticker,
                    "reason": f"non-positive net_credit {trade.get('net_credit')}",
                })
                continue
        trades.append(trade)
        EventsRepo.log(project_id, "Strategist", "SELECTION", {
            "ticker": ticker,
            "kind": trade_type,
            "outcome": "approved",
            "underlying_price": setup.get("underlying_price"),
            "narrative": [
                f"{trade_type} setup found on {ticker}: "
                f"net credit ${trade.get('net_credit') or 0}, "
                f"max loss ${trade.get('max_loss') or 0}.",
            ],
        })

    EventsRepo.log(project_id, "Strategist", "DECIDE", {
        "mode": mode,
        "candidates": tickers,
        "selected": [t["ticker"] for t in trades],
        "rejections": rejections,
    })
    return {"selected_trades": trades}


def strategy_dispatcher_node(state: dict[str, Any]) -> dict[str, Any]:
    """Graph-facing dispatcher. Reads strategy_mode and routes."""
    project_id = state["project_id"]
    mode = str(ProjectSettings.get(
        project_id, "strategy_mode", default="wheel") or "wheel").lower()

    # Wheel-family modes route to the legacy wheel strategist (which
    # already implements every per-ticker filter the wheel needs).
    if mode in ("wheel", "wheel_plus_dca", "dca_only", ""):
        return analyze_wheel_node(state)

    # Spreads.
    if mode in _SPREAD_DISPATCH:
        return _analyze_spread_node(state, mode)

    # Intraday momentum — separate module so this file doesn't depend
    # on intraday code at import time.
    if mode == "intraday_momentum":
        try:
            from .intraday_strategist import analyze_intraday_node
        except Exception:
            logger.exception("intraday_strategist not importable")
            return {"selected_trades": []}
        return analyze_intraday_node(state)

    # Unknown mode — log and skip cleanly rather than crashing the cycle.
    EventsRepo.log(project_id, "Strategist", "DECIDE", {
        "mode": mode,
        "skipped": "unknown_strategy_mode",
        "narrative": [
            f"strategy_mode='{mode}' is not a known mode; "
            "no trades selected this cycle.",
        ],
    })
    return {"selected_trades": []}
