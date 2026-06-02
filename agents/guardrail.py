"""Agent 3 — Risk & Execution Guardrail.

Deterministic, language-model-free. Enforces:
  * Equity stop loss: liquidate if current price <= entry - stop_loss_dollars
  * Option collateral cap: required collateral must fit inside the configured
    fraction of buying power.
"""
from __future__ import annotations

import logging
from typing import Any

from db.repositories import EventsRepo, PositionsRepo, ProjectsRepo
from db.settings_store import ProjectSettings
from execution import AlpacaClient
from risk.concentration import check_concentration_limit
from risk.kill_switch import evaluate_kill_switches

logger = logging.getLogger(__name__)


def risk_guardrail_node(state: dict[str, Any]) -> dict[str, Any]:
    project_id = state["project_id"]
    project = ProjectsRepo.get(project_id)
    if project is None:
        return {"risk_clearance": False, "guardrail_actions": []}

    # ---- 0. KILL SWITCHES — evaluate first; if any breach, halt the project.
    breaches = evaluate_kill_switches(project_id)
    if breaches:
        return {
            "risk_clearance": False,
            "guardrail_actions": [{"action": "kill_switch", "breaches": breaches}],
            "selected_trades": [],
        }

    client = AlpacaClient(project)
    stop_loss = float(ProjectSettings.get(project_id, "stop_loss_dollars"))
    max_collateral_pct = float(ProjectSettings.get(project_id, "max_collateral_pct"))

    actions: list[dict[str, Any]] = []

    # ---- 1. Enforce equity stop loss on live positions ---------------------
    try:
        live_positions = client.list_positions()
    except Exception as e:
        EventsRepo.log(project_id, "Guardrail", "ERROR", {"err": str(e)})
        return {"risk_clearance": False, "guardrail_actions": []}

    for pos in live_positions:
        if pos["asset_class"] != "us_equity":
            continue
        entry = pos["avg_entry_price"]
        current = pos["current_price"]
        if current is None:
            continue
        if (entry - current) >= stop_loss:
            try:
                if ProjectSettings.get(project_id, "dry_run"):
                    actions.append({"action": "would_liquidate", "symbol": pos["symbol"],
                                    "entry": entry, "current": current})
                else:
                    result = client.liquidate_position(pos["symbol"])
                    actions.append({"action": "liquidated", "symbol": pos["symbol"],
                                    "entry": entry, "current": current, "order": result})
                # Find matching DB row and mark STOPPED_OUT
                for row in PositionsRepo.list_open(project_id):
                    if row["ticker"] == pos["symbol"]:
                        PositionsRepo.close(row["position_id"], final_status="STOPPED_OUT")
                EventsRepo.log(project_id, "Guardrail", "RISK", actions[-1])
            except Exception as e:
                EventsRepo.log(project_id, "Guardrail", "ERROR",
                               {"liquidation_failed": pos["symbol"], "err": str(e)})

    # ---- 2. Collateral check on pending option trades ----------------------
    try:
        account = client.get_account()
    except Exception as e:
        EventsRepo.log(project_id, "Guardrail", "ERROR", {"err": str(e)})
        return {"risk_clearance": False, "guardrail_actions": actions}

    buying_power = account["buying_power"]
    proposed = state.get("selected_trades") or []
    approved: list[dict[str, Any]] = []

    cumulative_collateral = 0.0
    cap = max_collateral_pct * buying_power
    csp_qty = max(1, int(ProjectSettings.get(project_id, "contracts_per_csp")))
    rejections: list[dict[str, Any]] = []
    for trade in proposed:
        # Tag quantity onto the trade for concentration calc consistency.
        if trade["type"] == "CSP":
            trade["quantity"] = csp_qty
        required = 0.0
        if trade["type"] == "CSP":
            required = float(trade["strike"]) * 100.0 * csp_qty
        elif trade["type"] == "CC":
            required = 0.0  # shares already collateralize

        # Concentration check (per-ticker)
        ok, reason = check_concentration_limit(project_id, trade,
                                               buying_power, approved)
        if not ok:
            rejections.append({"trade": trade, "reason": reason})
            EventsRepo.log(project_id, "Guardrail", "RISK", {
                "rejected": trade,
                "reason": reason,
            })
            continue

        if cumulative_collateral + required <= cap:
            cumulative_collateral += required
            approved.append(trade)
        else:
            rejections.append({"trade": trade, "reason": "collateral cap"})
            EventsRepo.log(project_id, "Guardrail", "RISK", {
                "rejected": trade,
                "reason": "collateral cap",
                "required": required,
                "cap": cap,
                "cumulative": cumulative_collateral,
            })

    EventsRepo.log(project_id, "Guardrail", "RISK", {
        "stop_loss_dollars": stop_loss,
        "actions": actions,
        "buying_power": buying_power,
        "approved_trades": approved,
    })

    return {
        "risk_clearance": True,
        "guardrail_actions": actions,
        "selected_trades": approved,
    }
