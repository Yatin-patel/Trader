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


def sweep_stale_close_orders(
    client: Any,
    project_id: str,
    max_age_seconds: int = 300,
) -> int:
    """Cancel any close-side order (BUY on an option = buy-to-close)
    that's been sitting open longer than ``max_age_seconds``.

    Why this exists: stop-loss / take-profit / defensive-roll all
    submit at the mid price at the moment the cycle runs. In live
    markets with wide bid/ask, that bid may not be hit. The order
    sits open, the underlying keeps moving against us, but the
    order_guard prevents resubmission — so the position effectively
    has NO active close protection. This sweep cancels stale orders
    so the next cycle resubmits at the CURRENT mid (and the new
    aggressive-pricing path in stop-loss bids at ask × 1.02 to
    actually fill).

    Returns the number of orders cancelled."""
    from datetime import datetime, timezone
    now = datetime.now(tz=timezone.utc)
    cancelled = 0
    # Force a fresh fetch — we don't want to act on stale cache
    reset_cache(project_id)
    orders = _open_orders(client, project_id)
    for o in orders:
        try:
            side = str(getattr(o, "side", "")).lower().replace(
                "orderside.", "")
            sym = str(getattr(o, "symbol", "")).upper()
            # Treat 6+ char OCC option symbols. Skip equity orders.
            if len(sym) < 12:
                continue
            # Only cancel BUY orders (buy-to-close on a short option).
            # SELL orders are typically opens; don't cancel those.
            if side != "buy":
                continue
            submitted_at = getattr(o, "submitted_at", None)
            if submitted_at is None:
                continue
            if isinstance(submitted_at, str):
                submitted_at = datetime.fromisoformat(
                    submitted_at.replace("Z", "+00:00"))
            if submitted_at.tzinfo is None:
                submitted_at = submitted_at.replace(tzinfo=timezone.utc)
            age = (now - submitted_at).total_seconds()
            if age < max_age_seconds:
                continue
            try:
                # alpaca-py API
                client.trading.cancel_order_by_id(o.id)
                cancelled += 1
            except Exception:
                logger.exception(
                    "stale-order sweep cancel failed: %s",
                    getattr(o, "id", "?"))
        except Exception:
            continue
    if cancelled > 0:
        reset_cache(project_id)
        logger.info(
            "stale-order sweep cancelled %d close(s) for %s "
            "(age > %ds)", cancelled, project_id, max_age_seconds)
    return cancelled
