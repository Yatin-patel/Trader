"""Agent 3 — Risk & Execution Guardrail.

Deterministic, language-model-free. Enforces:
  * Equity stop loss: liquidate if current price <= entry - stop_loss_dollars
  * Option collateral cap: required collateral must fit inside the configured
    fraction of buying power.
  * Per-ticker concentration limit.
  * Portfolio-level net delta and net vega caps (settable per project, off
    by default to preserve existing behavior).
  * Sector concentration limit (max % of BP in any one GICS sector).
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

    # ---- Portfolio-level greek snapshot (zero-cost when limits unset) ---
    max_net_delta = float(ProjectSettings.get(project_id, "max_net_delta",
                                              default=0) or 0)
    max_net_vega = float(ProjectSettings.get(project_id, "max_net_vega",
                                             default=0) or 0)
    current_delta = current_vega = 0.0
    if max_net_delta > 0 or max_net_vega > 0:
        try:
            from risk.greeks_agg import aggregate_greeks
            g = aggregate_greeks(project_id)
            current_delta = float(g.get("net_delta", g.get("delta", 0)) or 0)
            current_vega = float(g.get("net_vega", g.get("vega", 0)) or 0)
        except Exception as e:
            logger.warning("greeks snapshot failed: %s", e)

    # ---- Sector concentration (lazy import; only when cap configured) ---
    max_sector_pct = float(ProjectSettings.get(
        project_id, "max_concentration_per_sector", default=0) or 0)
    sector_used: dict[str, float] = {}
    if max_sector_pct > 0:
        try:
            from risk.sectors import sector_of, sector_used_collateral
            sector_used = sector_used_collateral(project_id)
        except Exception as e:
            logger.warning("sector snapshot failed: %s", e)

    for trade in proposed:
        # Tag quantity onto the trade for concentration calc consistency.
        if trade["type"] == "CSP":
            trade["quantity"] = csp_qty
        required = 0.0
        if trade["type"] == "CSP":
            required = float(trade["strike"]) * 100.0 * csp_qty
        elif trade["type"] == "CC":
            required = 0.0  # shares already collateralize

        # Per-ticker concentration
        ok, reason = check_concentration_limit(project_id, trade,
                                               buying_power, approved)
        if not ok:
            rejections.append({"trade": trade, "reason": reason})
            EventsRepo.log(project_id, "Guardrail", "RISK", {
                "rejected": trade, "reason": reason,
            })
            continue

        # Per-sector concentration
        if max_sector_pct > 0 and buying_power > 0:
            try:
                from risk.sectors import sector_of
                sec = sector_of(trade["ticker"])
                if sec:
                    sec_cap = max_sector_pct * buying_power
                    sec_after = sector_used.get(sec, 0.0) + required
                    if sec_after > sec_cap:
                        reason = (
                            f"sector cap: {sec} would use ${sec_after:,.0f} "
                            f"(cap ${sec_cap:,.0f}, "
                            f"{max_sector_pct*100:.0f}% of BP)"
                        )
                        rejections.append({"trade": trade, "reason": reason})
                        EventsRepo.log(project_id, "Guardrail", "RISK", {
                            "rejected": trade, "reason": reason,
                            "sector": sec,
                        })
                        continue
                    sector_used[sec] = sec_after
            except Exception as e:
                logger.warning("sector check error for %s: %s",
                               trade.get("ticker"), e)

        # Portfolio net-delta cap (CSPs add NEGATIVE delta when sold,
        # because short put delta is negative — adds ~+0.30*100*qty to
        # bullish exposure. We approximate added delta = |trade delta| *
        # 100 * qty since the strategist already validated direction.)
        if max_net_delta > 0:
            d_est = abs(float(trade.get("delta") or 0)) * 100 * csp_qty
            # CSP is bullish (short put = +delta); CC short call = -delta.
            sign = +1 if trade["type"] == "CSP" else -1
            projected_delta = current_delta + sign * d_est
            if abs(projected_delta) > max_net_delta:
                reason = (
                    f"net-delta cap: would put portfolio at "
                    f"{projected_delta:+.0f} (cap ±{max_net_delta:.0f})"
                )
                rejections.append({"trade": trade, "reason": reason})
                EventsRepo.log(project_id, "Guardrail", "RISK", {
                    "rejected": trade, "reason": reason,
                    "current_delta": current_delta,
                    "projected_delta": projected_delta,
                })
                continue

        # Portfolio net-vega cap (short options = -vega; both CSP and CC
        # sold add negative vega to portfolio).
        if max_net_vega > 0:
            # Strategist doesn't always have vega — fall back to a typical
            # short-option vega proxy (~0.10 * underlying for ATM-ish).
            v_est = float(trade.get("vega") or 0)
            if v_est == 0:
                v_est = 0.10 * float(trade.get("underlying_price") or 0)
            v_est = abs(v_est) * 100 * csp_qty
            projected_vega = current_vega - v_est
            if abs(projected_vega) > max_net_vega:
                reason = (
                    f"net-vega cap: would put portfolio at "
                    f"{projected_vega:+.0f} (cap ±{max_net_vega:.0f})"
                )
                rejections.append({"trade": trade, "reason": reason})
                EventsRepo.log(project_id, "Guardrail", "RISK", {
                    "rejected": trade, "reason": reason,
                    "current_vega": current_vega,
                    "projected_vega": projected_vega,
                })
                continue

        # Collateral cap (portfolio-wide)
        if cumulative_collateral + required <= cap:
            cumulative_collateral += required
            approved.append(trade)
            # update rolling greeks so subsequent trades in the same
            # cycle see the cumulative effect
            if max_net_delta > 0:
                d_est = abs(float(trade.get("delta") or 0)) * 100 * csp_qty
                current_delta += (+1 if trade["type"] == "CSP" else -1) * d_est
            if max_net_vega > 0:
                v_est = float(trade.get("vega") or 0)
                if v_est == 0:
                    v_est = 0.10 * float(trade.get("underlying_price") or 0)
                current_vega -= abs(v_est) * 100 * csp_qty
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
