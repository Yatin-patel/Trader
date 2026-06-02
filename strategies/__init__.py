"""Strategy registry — wheel is the default; alternatives plug in here.

A Strategy is responsible for: given a ticker + snapshot + project context,
return the proposed trade (or None) plus a selection narrative. The
strategist node iterates strategies the project has enabled and routes
each candidate to the first match.
"""
from .base import Strategy
from .wheel import WheelStrategy
from .bull_put_spread import BullPutSpreadStrategy

REGISTRY: dict[str, Strategy] = {
    "wheel":            WheelStrategy(),
    "bull_put_spread":  BullPutSpreadStrategy(),
}


def get_strategy(name: str) -> Strategy | None:
    return REGISTRY.get((name or "wheel").lower())


__all__ = ["Strategy", "WheelStrategy", "BullPutSpreadStrategy",
           "REGISTRY", "get_strategy"]
