"""Order execution node.

Reads `selected_trades` from state, submits each via Alpaca, persists the
result, and signals TRADE_COMPLETED when the cycle finishes so the graph
loops back to the Scanner.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from db.repositories import EventsRepo, PositionsRepo, ProjectsRepo, WheelRepo
from db.settings_store import ProjectSettings
from execution import AlpacaClient

logger = logging.getLogger(__name__)


def execute_orders_node(state: dict[str, Any]) -> dict[str, Any]:
    project_id = state["project_id"]
    project = ProjectsRepo.get(project_id)
    if project is None:
        return {"execution_status": "TRADE_COMPLETED"}

    trades = state.get("selected_trades") or []
    if not trades:
        return {"execution_status": "TRADE_COMPLETED",
                "target_tickers": [], "selected_trades": []}

    dry_run = bool(ProjectSettings.get(project_id, "dry_run"))
    tif = str(ProjectSettings.get(project_id, "order_time_in_force") or "day")
    extended = bool(ProjectSettings.get(project_id, "use_extended_hours"))
    stop_loss = float(ProjectSettings.get(project_id, "stop_loss_dollars"))
    csp_qty = max(1, int(ProjectSettings.get(project_id, "contracts_per_csp")))
    max_qty = max(1, int(ProjectSettings.get(project_id, "max_contracts_per_ticker")))
    csp_qty = min(csp_qty, max_qty)

    client = AlpacaClient(project)
    results: list[dict[str, Any]] = []

    # Snapshot of strategy params at trade time — copied onto each
    # opened contract for later attribution analysis.
    settings_snapshot = {
        "csp_delta_min": ProjectSettings.get(project_id, "csp_delta_min"),
        "csp_delta_max": ProjectSettings.get(project_id, "csp_delta_max"),
        "csp_min_dte":   ProjectSettings.get(project_id, "csp_min_dte"),
        "csp_max_dte":   ProjectSettings.get(project_id, "csp_max_dte"),
        "cc_delta_min":  ProjectSettings.get(project_id, "cc_delta_min"),
        "cc_delta_max":  ProjectSettings.get(project_id, "cc_delta_max"),
        "max_collateral_pct": ProjectSettings.get(project_id, "max_collateral_pct"),
        "contracts_per_csp":  ProjectSettings.get(project_id, "contracts_per_csp"),
    }

    # Caches one fresh broker account snapshot per cycle for the BP
    # recheck below. The Guardrail's BP value comes from earlier in
    # the cycle and can be stale by the time we reach this point —
    # e.g. an earlier trade in this same cycle consumed all the BP.
    _live_account: dict[str, Any] | None = None

    def _live_options_bp() -> float:
        nonlocal _live_account
        if _live_account is None:
            try:
                _live_account = client.get_account()
            except Exception:
                _live_account = {}
        try:
            return float(_live_account.get("options_buying_power") or 0)
        except Exception:
            return 0.0

    for trade in trades:
        try:
            if trade["type"] == "CSP":
                if dry_run:
                    results.append({"trade": trade, "status": "DRY_RUN", "qty": csp_qty})
                else:
                    # Defensive BP check right before submission. The
                    # Guardrail saw a snapshot from the start of the
                    # cycle; if an earlier trade in this same cycle
                    # consumed the available BP, Alpaca would reject
                    # with 40310000. Catch it here and surface as a
                    # routine REJECTED rather than a system ERROR.
                    required = float(trade["strike"]) * 100.0 * csp_qty
                    live_bp = _live_options_bp()
                    if live_bp > 0 and required > live_bp:
                        results.append({
                            "trade": trade, "status": "REJECTED",
                            "reason": (
                                f"live options BP exhausted: required "
                                f"${required:,.0f}, available "
                                f"${live_bp:,.0f}"
                            ),
                        })
                        continue
                    order = client.submit_limit_option(
                        option_symbol=trade["option_symbol"],
                        qty=csp_qty,
                        side="sell",
                        limit_price=float(trade["premium"]),
                        time_in_force=tif,
                    )
                    contract_id = WheelRepo.open_contract(
                        project_id=project_id,
                        ticker=trade["ticker"],
                        phase="CASH_SECURED_PUT",
                        option_symbol=trade["option_symbol"],
                        strike=float(trade["strike"]),
                        premium=float(trade["premium"]),
                        expiration=datetime.fromisoformat(trade["expiration"]).date(),
                        delta=trade.get("delta"),
                        quantity=csp_qty,
                        underlying_at_entry=trade.get("underlying_price"),
                        settings_snapshot=settings_snapshot,
                    )
                    # Attach contract to wheel cycle and accumulate premium.
                    try:
                        from analytics.wheel_cycles import record_csp_sold
                        premium_dollars = float(trade["premium"]) * 100.0 * csp_qty
                        record_csp_sold(project_id, trade["ticker"],
                                        contract_id, premium_dollars)
                    except Exception:
                        logger.exception("wheel cycle CSP tracking failed")
                    # Track order lifecycle.
                    try:
                        from ops.orders_tracker import record_submission
                        record_submission(
                            project_id,
                            alpaca_order_id=str(order.get("id") or ""),
                            symbol=str(order.get("symbol") or ""),
                            side=str(order.get("side") or ""),
                            order_type=str(order.get("type") or ""),
                            qty=float(order.get("qty") or 0),
                            limit_price=float(trade.get("premium") or 0)
                                if trade.get("type") in ("CSP", "CC") else None,
                            status=str(order.get("status") or "new"),
                        )
                    except Exception:
                        logger.exception("orders tracker failed")
                    results.append({"trade": trade, "status": "SUBMITTED", "order": order})

            elif trade["type"] == "CC":
                if dry_run:
                    results.append({"trade": trade, "status": "DRY_RUN"})
                else:
                    order = client.submit_limit_option(
                        option_symbol=trade["option_symbol"],
                        qty=1,
                        side="sell",
                        limit_price=float(trade["premium"]),
                        time_in_force=tif,
                    )
                    contract_id = WheelRepo.open_contract(
                        project_id=project_id,
                        ticker=trade["ticker"],
                        phase="COVERED_CALL",
                        option_symbol=trade["option_symbol"],
                        strike=float(trade["strike"]),
                        premium=float(trade["premium"]),
                        expiration=datetime.fromisoformat(trade["expiration"]).date(),
                        delta=trade.get("delta"),
                        quantity=1,
                        underlying_at_entry=trade.get("underlying_price"),
                        settings_snapshot=settings_snapshot,
                    )
                    try:
                        from analytics.wheel_cycles import record_cc_sold
                        record_cc_sold(project_id, trade["ticker"], contract_id,
                                       float(trade["premium"]) * 100.0)
                    except Exception:
                        logger.exception("wheel cycle CC tracking failed")
                    # Track order lifecycle.
                    try:
                        from ops.orders_tracker import record_submission
                        record_submission(
                            project_id,
                            alpaca_order_id=str(order.get("id") or ""),
                            symbol=str(order.get("symbol") or ""),
                            side=str(order.get("side") or ""),
                            order_type=str(order.get("type") or ""),
                            qty=float(order.get("qty") or 0),
                            limit_price=float(trade.get("premium") or 0)
                                if trade.get("type") in ("CSP", "CC") else None,
                            status=str(order.get("status") or "new"),
                        )
                    except Exception:
                        logger.exception("orders tracker failed")
                    results.append({"trade": trade, "status": "SUBMITTED", "order": order})

            elif trade["type"] == "STOCK_BUY":
                if dry_run:
                    results.append({"trade": trade, "status": "DRY_RUN"})
                else:
                    order = client.submit_market_equity(
                        symbol=trade["ticker"],
                        qty=int(trade["quantity"]),
                        side="buy",
                        time_in_force=tif,
                        extended_hours=extended,
                    )
                    PositionsRepo.open_position(
                        project_id=project_id,
                        ticker=trade["ticker"],
                        entry_price=float(trade["entry_price"]),
                        quantity=int(trade["quantity"]),
                        stop_loss_dollars=stop_loss,
                    )
                    # Track order lifecycle.
                    try:
                        from ops.orders_tracker import record_submission
                        record_submission(
                            project_id,
                            alpaca_order_id=str(order.get("id") or ""),
                            symbol=str(order.get("symbol") or ""),
                            side=str(order.get("side") or ""),
                            order_type=str(order.get("type") or ""),
                            qty=float(order.get("qty") or 0),
                            limit_price=float(trade.get("premium") or 0)
                                if trade.get("type") in ("CSP", "CC") else None,
                            status=str(order.get("status") or "new"),
                        )
                    except Exception:
                        logger.exception("orders tracker failed")
                    results.append({"trade": trade, "status": "SUBMITTED", "order": order})

        except Exception as e:
            err_text = str(e)
            # Classify broker rejections as REJECTED, not ERROR.
            # Alpaca returns JSON with a "code":4XXXXXXX number for any
            # business-rule rejection (insufficient BP, conflicting
            # position, halted symbol, market closed for that asset
            # class, etc.). These are NOT system errors — they're the
            # broker saying "no". Showing them as ERROR×N on the
            # dashboard creates alert fatigue and makes real bugs
            # harder to spot.
            is_broker_rejection = (
                '"code":4' in err_text
                or 'insufficient' in err_text.lower()
                or 'cannot open' in err_text.lower()
                or 'halted' in err_text.lower()
                or 'market_closed' in err_text.lower()
            )
            if is_broker_rejection:
                # Try to pull a human-readable reason out of the JSON.
                reason = err_text[:200]
                try:
                    import json as _json
                    import re as _re
                    m = _re.search(r'\{.*\}', err_text)
                    if m:
                        body = _json.loads(m.group(0))
                        reason = (body.get("message")
                                  or body.get("error")
                                  or reason)
                except Exception:
                    pass
                results.append({
                    "trade": trade, "status": "REJECTED",
                    "reason": str(reason)[:300],
                })
                # Don't burn a full ERROR-level log on a routine
                # broker NO. Info-level keeps it in the activity
                # feed for debugging without raising alarm.
                logger.info("broker rejected trade %s: %s",
                            trade.get("ticker"), reason)
            else:
                results.append({"trade": trade, "status": "ERROR",
                                "err": err_text})
                logger.exception("execute trade failed: %s", trade)

    exec_payload = {"results": results, "dry_run": dry_run}
    EventsRepo.log(project_id, "Executor", "EXECUTE", exec_payload)
    # Notify if any real order was submitted (not just dry-run noise).
    submitted = [r for r in results if r.get("status") == "SUBMITTED"]
    if submitted:
        try:
            from notifications.dispatcher import notify_event
            notify_event(project_id, "EXECUTE", exec_payload)
        except Exception:
            logger.exception("notifier failed on EXECUTE")

    return {
        "execution_status": "TRADE_COMPLETED",
        "execution_results": results,
        "target_tickers": [],
        "selected_trades": [],
    }
