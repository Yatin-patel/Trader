"""Per-ticker concentration limit.

Checks whether a proposed trade would push cumulative collateral on its
underlying above the configured `max_concentration_per_ticker` setting
(a fraction of buying power).
"""
from __future__ import annotations

from typing import Any

from db.repositories import WheelRepo
from db.settings_store import ProjectSettings


def _collateral_required(trade: dict[str, Any]) -> float:
    if trade["type"] == "CSP":
        return float(trade["strike"]) * 100.0 * int(trade.get("quantity", 1))
    return 0.0   # CCs are share-backed


def check_concentration_limit(project_id: str, proposed: dict[str, Any],
                              buying_power: float,
                              already_approved: list[dict[str, Any]]) -> tuple[bool, str]:
    """Return (allowed, reason) for whether `proposed` fits the concentration cap.

    `already_approved` is the list of trades already approved this cycle so we
    accumulate them in the running total.
    """
    cap_pct = ProjectSettings.get(project_id, "max_concentration_per_ticker",
                                  default=None)
    if cap_pct is None or float(cap_pct) <= 0 or buying_power <= 0:
        return (True, "")

    ticker = proposed["ticker"]
    cap = float(cap_pct) * float(buying_power)

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
        f"(cap ${cap:,.0f}, {cap_pct*100:.0f}% of BP)",
    )
