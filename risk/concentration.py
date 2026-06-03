"""Per-ticker concentration limit.

Checks whether a proposed trade would push cumulative collateral on its
underlying above the configured ``max_concentration_per_ticker`` setting.

The cap is a fraction of a STABLE reference (the project's
``max_equity_allocation``), NOT of the broker's current options buying
power. Why: options_buying_power shrinks as you open positions, which
made a 15%-of-options_bp cap shrink from $15K → $2K over a day's
trading and silently blocked every subsequent CSP on mid-priced names.
Using the static budget means a "15% concentration cap" stays
semantically constant for the life of the project.

For a real broker-fit check (does the collateral actually fit in the
account?), see the ``max_collateral_pct`` gate in agents/guardrail.py
— that one still uses options_buying_power.
"""
from __future__ import annotations

from typing import Any

from db.repositories import ProjectsRepo, WheelRepo
from db.settings_store import ProjectSettings


def _collateral_required(trade: dict[str, Any]) -> float:
    if trade["type"] == "CSP":
        return float(trade["strike"]) * 100.0 * int(trade.get("quantity", 1))
    return 0.0   # CCs are share-backed


def _resolve_reference(project_id: str, fallback_bp: float) -> tuple[float, str]:
    """Return (reference_amount, source_name).

    Priority:
      1. project.max_equity_allocation if set and > 0
      2. fallback_bp passed in by the caller (typically options_bp or cash)
    """
    try:
        proj = ProjectsRepo.get(project_id)
        if proj is not None:
            allocation = float(getattr(proj, "max_equity_allocation", 0) or 0)
            if allocation > 0:
                return (allocation, "max_equity_allocation")
    except Exception:
        pass
    return (max(0.0, float(fallback_bp)), "fallback_bp")


def check_concentration_limit(project_id: str, proposed: dict[str, Any],
                              buying_power: float,
                              already_approved: list[dict[str, Any]]) -> tuple[bool, str]:
    """Return (allowed, reason) for whether `proposed` fits the concentration cap.

    ``already_approved`` is the list of trades already approved this cycle so we
    accumulate them in the running total. ``buying_power`` is the caller's
    real-broker-fit value, used only as a fallback when the project has no
    max_equity_allocation set.
    """
    cap_pct = ProjectSettings.get(project_id, "max_concentration_per_ticker",
                                  default=None)
    if cap_pct is None or float(cap_pct) <= 0:
        return (True, "")

    reference, source = _resolve_reference(project_id, buying_power)
    if reference <= 0:
        return (True, "")

    ticker = proposed["ticker"]
    cap = float(cap_pct) * reference

    # Existing open contracts on this ticker contribute strike * 100 * qty.
    open_contracts = WheelRepo.list_open(project_id)
    used = 0.0
    for c in open_contracts:
        if c["ticker"] != ticker:
            continue
        if c["strategy_phase"] == "CASH_SECURED_PUT":
            used += float(c["strike_price"]) * 100.0 * int(c.get("quantity") or 1)

    # Approved-but-not-yet-executed trades this cycle.
    for t in already_approved:
        if t.get("ticker") == ticker:
            used += _collateral_required(t)

    needed = _collateral_required(proposed)
    if used + needed <= cap:
        return (True, "")
    return (
        False,
        f"concentration cap: {ticker} would use ${used + needed:,.0f} "
        f"(cap ${cap:,.0f}, {cap_pct*100:.0f}% of {source} "
        f"${reference:,.0f})",
    )
