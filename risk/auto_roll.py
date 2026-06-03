"""Auto-roll near-expiration short options.

If an open contract is within `auto_roll_dte_threshold` days of expiry AND
out-of-the-money (no assignment risk), close it and reopen a similar one
further out using the project's configured delta/DTE bands.

Implementation detail: we don't actually submit the "open" leg here — we
just close the expiring leg and emit a marker event so the next Strategist
cycle picks the ticker up naturally and selects a new contract.
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Any

from db.repositories import EventsRepo, ProjectsRepo, WheelRepo
from db.settings_store import ProjectSettings, effective_csp_band
from execution import AlpacaClient
from risk.greeks_agg import _extract_underlying

logger = logging.getLogger(__name__)


def _is_otm(c: dict[str, Any], underlying_price: float) -> bool:
    """OTM check from the perspective of the SHORT option holder (us)."""
    strike = float(c["strike_price"])
    if c["strategy_phase"] == "CASH_SECURED_PUT":
        return underlying_price > strike   # underlying above strike = put OTM
    if c["strategy_phase"] == "COVERED_CALL":
        return underlying_price < strike   # underlying below strike = call OTM
    return False


def evaluate_auto_roll(project_id: str) -> list[dict[str, Any]]:
    if not ProjectSettings.get(project_id, "auto_roll_enabled", default=True):
        return []
    dte_threshold = int(ProjectSettings.get(project_id, "auto_roll_dte_threshold",
                                            default=2))
    project = ProjectsRepo.get(project_id)
    if project is None:
        return []
    client = AlpacaClient(project)
    open_contracts = WheelRepo.list_open(project_id)
    if not open_contracts:
        return []

    today = date.today()
    dry_run = bool(ProjectSettings.get(project_id, "dry_run"))
    tif = str(ProjectSettings.get(project_id, "order_time_in_force") or "day")

    # Cadence-drift roll: if a preset cadence is active and an OPEN CSP's
    # remaining DTE sits outside the preset band, roll it onto the cadence.
    # Only applies to CSPs — CCs follow assignment dynamics, not income cadence.
    band = effective_csp_band(project_id)
    cadence_active = band["cadence"] != "custom"

    # 21-DTE rule (tasty-style): once DTE drops to this threshold, roll any
    # OTM CSP regardless of profit, because gamma risk is no longer worth
    # the dwindling theta. Set to 0 to disable.
    twenty_one_dte = int(ProjectSettings.get(
        project_id, "manage_at_dte", default=21) or 0)

    candidates: list[dict[str, Any]] = []
    for c in open_contracts:
        exp = c.get("expiration_date")
        if not exp:
            continue
        dte = (exp - today).days
        if dte < 0:
            continue
        # Trigger 1: near expiration (existing behaviour)
        near_expiry = dte <= dte_threshold
        # Trigger 2: cadence drift — only for CSPs, only when a preset is set.
        cadence_drift = (
            cadence_active
            and c.get("strategy_phase") == "CASH_SECURED_PUT"
            and (dte < band["min_dte"] or dte > band["max_dte"])
            and not near_expiry
        )
        # Trigger 3: 21-DTE management for CSPs. Don't trigger for CCs —
        # CCs are managed by early-profit logic (separate rule below).
        twenty_one = (
            twenty_one_dte > 0
            and c.get("strategy_phase") == "CASH_SECURED_PUT"
            and dte <= twenty_one_dte
            and dte > dte_threshold  # near_expiry has priority
            and not cadence_drift    # cadence_drift has priority
        )
        if not (near_expiry or cadence_drift or twenty_one):
            continue
        c["_roll_reason"] = (
            "near_expiry" if near_expiry
            else ("cadence_drift" if cadence_drift else "21_dte_management")
        )
        candidates.append(c)

    if not candidates:
        return []

    # Need underlying prices to assess OTM.
    underlyings = {c["ticker"] for c in candidates}
    try:
        snaps = client.snapshots(list(underlyings))
    except Exception as e:
        logger.warning("auto-roll snapshot fetch failed: %s", e)
        return []

    actions: list[dict[str, Any]] = []
    for c in candidates:
        snap = snaps.get(c["ticker"])
        if not snap or snap.last_price <= 0:
            continue
        if not _is_otm(c, snap.last_price):
            # ITM → let it assign / be called away. Don't fight assignment.
            continue
        sym = c["option_symbol"]
        if not sym:
            continue
        # Close the expiring leg.
        underlying = _extract_underlying(sym)
        try:
            chain = client.option_chain_quotes(underlying)
        except Exception as e:
            logger.warning("chain fetch failed for %s: %s", underlying, e)
            continue
        quote = chain.get(sym) or {}
        ask = quote.get("ask") or 0
        bid = quote.get("bid") or 0
        if ask <= 0:
            continue
        mid = (bid + ask) / 2
        qty = int(c.get("quantity") or 1)
        attempt = {
            "ticker": c["ticker"],
            "option_symbol": sym,
            "dte": (c["expiration_date"] - today).days,
            "underlying_price": snap.last_price,
            "qty": qty,
            "close_price": mid,
            "roll_reason": c.get("_roll_reason", "near_expiry"),
            "cadence": band["cadence"],
        }
        if dry_run:
            attempt["status"] = "DRY_RUN"
        else:
            try:
                order = client.submit_limit_option(
                    option_symbol=sym, qty=qty, side="buy",
                    limit_price=round(mid, 2), time_in_force=tif,
                )
                attempt["status"] = "SUBMITTED"
                attempt["order"] = order
            except Exception as e:
                attempt["status"] = "ERROR"
                attempt["error"] = str(e)
        actions.append(attempt)
        if attempt["roll_reason"] == "cadence_drift":
            why = (
                f"DTE {attempt['dte']} is outside the '{band['cadence']}' "
                f"cadence band [{band['min_dte']}-{band['max_dte']}]"
            )
        elif attempt["roll_reason"] == "21_dte_management":
            why = (
                f"DTE {attempt['dte']} dropped to the management threshold "
                f"({twenty_one_dte}); rolling early to dodge gamma risk "
                "(tasty-style 21-DTE rule)"
            )
        else:
            why = (
                f"only {attempt['dte']} day(s) from expiry "
                f"(threshold {dte_threshold})"
            )
        EventsRepo.log(project_id, "AutoRoll", "CLOSE_FOR_ROLL", {
            **attempt,
            "narrative": [
                f"Auto-roll: {c['ticker']} {sym} — {why}; OTM "
                f"(strike ${c['strike_price']}, underlying "
                f"${snap.last_price:.2f}).",
                f"Closing for ${mid:.2f}; Strategist will reopen a new "
                f"contract on the next cycle.",
            ],
        })
    return actions
