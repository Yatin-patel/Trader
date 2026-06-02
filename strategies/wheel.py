"""WheelStrategy — wraps the existing strategist logic as a Strategy object.

This is the default. The strategist node calls into the existing inline
implementation, so this wrapper is mostly a marker for the registry; the
real wheel logic still lives in agents/strategist.py.
"""
from __future__ import annotations

from typing import Any


class WheelStrategy:
    name = "wheel"
    description = "Cash-secured puts and covered calls (default)."

    def evaluate(self, *, project_id: str, ticker: str,
                 last_price: float, settings: dict[str, Any]) -> dict[str, Any] | None:
        # The wheel evaluation is inline in agents/strategist.py. This wrapper
        # is here so the multi-strategy router knows wheel exists and so a UI
        # can display "active strategy = wheel".
        return None
