"""Strategy registry — wheel is the default; alternatives plug in here.

A Strategy is responsible for: given a ticker + snapshot + project context,
return the proposed trade (or None) plus a selection narrative. The
strategist node iterates strategies the project has enabled and routes
each candidate to the first match.
"""
from .base import Strategy
from .wheel import WheelStrategy
from .bull_put_spread import BullPutSpreadStrategy

# Advanced strategies
from .iron_condor import IronCondorStrategy
from .vertical_spreads import (
    BullPutSpreadStrategy as BullPutSpread,
    BearCallSpreadStrategy,
    BullCallSpreadStrategy,
    BearPutSpreadStrategy,
)
from .calendar_spread import CalendarSpreadStrategy

# Long-term strategies
from .dca import (
    create_dca_schedule,
    list_dca_schedules,
    execute_dca_purchase,
    execute_due_schedules,
)
from .rebalancer import (
    set_target_allocation,
    get_target_allocations,
    get_current_allocations,
    preview_rebalance,
    execute_rebalance,
)


REGISTRY: dict[str, type] = {
    "wheel":            WheelStrategy,
    "bull_put_spread":  BullPutSpreadStrategy,
    "iron_condor":      IronCondorStrategy,
    "bear_call_spread": BearCallSpreadStrategy,
    "bull_call_spread": BullCallSpreadStrategy,
    "bear_put_spread":  BearPutSpreadStrategy,
    "calendar_spread":  CalendarSpreadStrategy,
}


def get_strategy(name: str) -> type | None:
    """Get strategy class by name."""
    return REGISTRY.get((name or "wheel").lower())


def list_strategies() -> list[dict]:
    """List all registered strategies."""
    return [
        {"name": name, "class": cls.__name__}
        for name, cls in REGISTRY.items()
    ]


__all__ = [
    # Base
    "Strategy",
    "REGISTRY",
    "get_strategy",
    "list_strategies",
    # Wheel
    "WheelStrategy",
    "BullPutSpreadStrategy",
    # Advanced options
    "IronCondorStrategy",
    "BearCallSpreadStrategy",
    "BullCallSpreadStrategy",
    "BearPutSpreadStrategy",
    "CalendarSpreadStrategy",
    # DCA
    "create_dca_schedule",
    "list_dca_schedules",
    "execute_dca_purchase",
    "execute_due_schedules",
    # Rebalancer
    "set_target_allocation",
    "get_target_allocations",
    "get_current_allocations",
    "preview_rebalance",
    "execute_rebalance",
]
