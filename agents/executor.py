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

    for trade in trades:
        try:
            if trade["type"] == "CSP":
                if dry_run:
                    results.append({"trade": trade, "status": "DRY_RUN", "qty": csp_qty})
                else:
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
            results.append({"trade": trade, "status": "ERROR", "err": str(e)})
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
