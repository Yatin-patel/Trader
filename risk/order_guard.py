"""Pre-submission duplicate-order guard.

Why this exists
---------------
Friday 2026-06-05: ten duplicate buy-to-close orders for the same
WFC260618P00080000 contract queued up on Yatin-Test1 across one
trading afternoon. Take-profit / stop-loss / defensive-roll each
re-evaluated the position every cycle and re-submitted a new close
order because:
  1. The first close hadn't filled yet (liquidity / market closed)
  2. The defense module had no visibility into pending orders
  3. The DB still showed the contract as OPEN

On Monday morning all 10 orders would try to fill simultaneously,
resulting in a flood of "insufficient qty available" rejections at
best, or partial-fill chaos at worst.

This module is a single source of truth: BEFORE submitting any
close (buy-to-close on a short, sell-to-close on a long), call
``has_pending_close_for_symbol`` to confirm one isn't already
sitting in the broker's order book.
"""
from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)


# Per-process cache so we don't slam the broker's get_orders endpoint
# on every contract iteration. Keyed by project_id, TTL 10s. The
# cache is invalidated automatically when the cycle starts a new
# evaluation pass (caller should reset_cache between runs if they
# need fresh data).
_CACHE: dict[str, tuple[float, list[Any]]] = {}
_CACHE_TTL_SECONDS = 10.0


def _open_orders(client: Any, project_id: str) -> list[Any]:
    """Fetch open orders for the project's broker. Cached briefly so
    iterating 10 contracts doesn't hit the broker 10 times."""
    now = time.monotonic()
    cached = _CACHE.get(project_id)
    if cached and (now - cached[0]) < _CACHE_TTL_SECONDS:
        return cached[1]
    try:
        # Alpaca SDK: client.trading.get_orders() returns open orders.
        # ETrade: would need a different call — for now fall back to
        # treating "no info" as "no pending order" since ETrade's
        # market-close window is shorter and the same bug is less
        # likely to compound.
        if hasattr(client, "trading"):
            orders = list(client.trading.get_orders())
        else:
            orders = []
    except Exception as e:
        logger.warning(
            "order_guard fetch failed for %s: %s", project_id, e)
        orders = []
    _CACHE[project_id] = (now, orders)
    return orders


def reset_cache(project_id: str | None = None) -> None:
    """Invalidate the cache. Call at the start of a cycle when fresh
    data matters more than the 10s TTL."""
    if project_id:
        _CACHE.pop(project_id, None)
    else:
        _CACHE.clear()


def has_pending_close_for_symbol(
    client: Any,
    project_id: str,
    option_symbol: str,
    side_we_will_submit: str = "buy",
) -> bool:
    """True if there's already an open order for this exact OCC
    option symbol on the same side we're about to submit.

    The side check matters: we DON'T want to block a sell-to-open
    on a new strike just because an old buy-to-close on a different
    contract is pending. We DO want to block re-submitting a
    buy-to-close when one is already queued.
    """
    want_side = side_we_will_submit.lower()
    sym_upper = (option_symbol or "").upper()
    for o in _open_orders(client, project_id):
        try:
            o_sym = str(getattr(o, "symbol", "")).upper()
            o_side = str(getattr(o, "side", "")).lower()
            # alpaca-py enum stringifies as 'OrderSide.BUY' or 'buy'
            # depending on version. Normalize both forms.
            o_side = o_side.replace("orderside.", "")
            if o_sym == sym_upper and o_side == want_side:
                return True
        except Exception:
            continue
    return False


def count_pending_for_symbol(
    client: Any,
    project_id: str,
    option_symbol: str,
) -> int:
    """Total open orders for a symbol, both sides. Useful for the
    operator-facing audit endpoint that surfaces 'stuck close'
    situations."""
    sym_upper = (option_symbol or "").upper()
    n = 0
    for o in _open_orders(client, project_id):
        try:
            if str(getattr(o, "symbol", "")).upper() == sym_upper:
                n += 1
        except Exception:
            continue
    return n
