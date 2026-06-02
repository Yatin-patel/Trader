"""BullPutSpreadStrategy — defined-risk credit spread (SCAFFOLD).

Sells a put at delta in [csp_delta_min, csp_delta_max] and buys a further
OTM put for protection. Capital required is (short_strike - long_strike)
* 100, way less than a naked CSP. Win condition: both expire worthless.

This is a SCAFFOLD only — the full implementation hooks into the same
contract-selection + executor pipeline as the wheel but pairs two legs.
For now the strategy returns None so the Strategist's wheel-only flow
remains the active path. Enable by setting strategy = "bull_put_spread"
in project settings AND filling in the legs in evaluate().
"""
from __future__ import annotations

from typing import Any


class BullPutSpreadStrategy:
    name = "bull_put_spread"
    description = (
        "Defined-risk vertical credit spread: sell put @ delta band, "
        "buy further OTM put for protection."
    )

    def evaluate(self, *, project_id: str, ticker: str,
                 last_price: float, settings: dict[str, Any]) -> dict[str, Any] | None:
        # Scaffold — not yet emitting trades.
        return None
