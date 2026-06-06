"""Defensive stop-loss for short options.

Why this exists
---------------
Selling a put for $1.00 and watching it run to $3-5 is the wheel
trader's worst nightmare. The platform was taking profit at 50% on
winners but had NO mechanism to cut losers — meaning realized P&L
looked great (every closed trade booked a profit) while unrealized
losses on still-open positions grew quietly past the realized
gains. User dashboard showed this exact pattern: +$2,747 realized
but −$3,358 unrealized = net loss.

Rule
----
For each open short option, fetch the current mid-price. If the
current MID is ≥ option_stop_loss_multiple × premium_collected,
buy-to-close immediately. Default multiple = 2.0 (i.e. close when
the option would cost 2× what we sold it for to unwind).

A 2× stop is the textbook "max defensive loss" for a credit
trade: by the time the option doubles in price, statistically it
implies the underlying has moved well past your short strike and
the gamma is still working against you. Holding past 2× usually
turns a defined-risk-by-design trade into an undefined one.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from db.repositories import EventsRepo, ProjectsRepo, WheelRepo
from db.settings_store import ProjectSettings
from execution import get_broker
from risk.greeks_agg import _extract_underlying

logger = logging.getLogger(__name__)


# Same back-off pattern as take_profit — after 3 consecutive errors
# on the same contract, suppress further attempts for 30 min so a
# halted symbol or network issue doesn't spam the broker.
_BACKOFF_FAILS = 3
_BACKOFF_SECONDS = 1800
_backoff: dict[tuple[str, str], dict[str, float]] = {}


def _backoff_check(project_id: str, sym: str) -> bool:
    state = _backoff.get((project_id, sym))
    if not state:
        return True
    if state["fails"] < _BACKOFF_FAILS:
        return True
    if (time.time() - state["paused_at"]) >= _BACKOFF_SECONDS:
        _backoff.pop((project_id, sym), None)
        return True
    return False


def _backoff_record(project_id: str, sym: str, ok: bool) -> None:
    key = (project_id, sym)
    if ok:
        _backoff.pop(key, None)
        return
    state = _backoff.setdefault(key, {"fails": 0, "paused_at": 0.0})
    state["fails"] += 1
    if state["fails"] >= _BACKOFF_FAILS:
        state["paused_at"] = time.time()


def evaluate_stop_loss(project_id: str) -> list[dict[str, Any]]:
    """Walk every open short option for ``project_id`` and close
    the ones whose mid-price has crossed the stop-loss threshold.
    Returns a list of close attempts so the cycle can log them
    alongside take-profit actions."""
    if not bool(ProjectSettings.get(
            project_id, "option_stop_loss_enabled", default=True)):
        return []
    stop_multiple = float(ProjectSettings.get(
        project_id, "option_stop_loss_multiple", default=2.0) or 2.0)
    if stop_multiple <= 1.0:
        # ≤1 would close immediately or never — invalid; refuse to act.
        return []

    project = ProjectsRepo.get(project_id)
    if project is None:
        return []
    try:
        client = get_broker(project)
    except Exception as e:
        logger.warning("stop-loss broker fetch failed for %s: %s",
                       project_id, e)
        return []

    # Sweep any stale buy-to-close orders before we re-evaluate.
    # Without this, a previous stop fired at MID that sat unfilled
    # would block this cycle (via order_guard) AND wouldn't fill
    # because the underlying kept moving. Cancelling stale closes
    # lets the new aggressive-pricing path resubmit at ASK × 1.02.
    try:
        from risk.order_guard import sweep_stale_close_orders
        max_age = int(ProjectSettings.get(
            project_id, "stale_close_max_age_seconds",
            default=300) or 300)
        sweep_stale_close_orders(client, project_id,
                                 max_age_seconds=max_age)
    except Exception:
        logger.exception("stale-order sweep failed; continuing")

    open_contracts = WheelRepo.list_open(project_id)
    if not open_contracts:
        return []

    dry_run = bool(ProjectSettings.get(project_id, "dry_run"))
    # Stop-loss closes prefer GTC so a buy-to-close submitted at 3:55pm
    # doesn't die at 4pm and leave the position unprotected over a
    # weekend. The operator can override per project.
    tif = str(ProjectSettings.get(
        project_id, "defensive_close_tif", default="gtc") or "gtc")
    aggressive = bool(ProjectSettings.get(
        project_id, "defensive_close_aggressive_pricing",
        default=True))

    # Group by underlying so we make one chain call per ticker.
    by_underlying: dict[str, list[dict[str, Any]]] = {}
    for c in open_contracts:
        sym = c.get("option_symbol")
        if not sym:
            continue
        by_underlying.setdefault(
            _extract_underlying(sym), []).append(c)

    actions: list[dict[str, Any]] = []
    for underlying, contracts in by_underlying.items():
        try:
            chain = client.option_chain_quotes(underlying)
        except Exception as e:
            logger.warning(
                "stop-loss chain fetch failed for %s: %s", underlying, e)
            continue
        for c in contracts:
            sym = c["option_symbol"]
            quote = chain.get(sym) or {}
            ask = float(quote.get("ask") or 0)
            bid = float(quote.get("bid") or 0)
            if ask <= 0:
                continue
            mid = (bid + ask) / 2
            premium_open = float(c["premium_collected"])
            if premium_open <= 0:
                continue
            qty = int(c.get("quantity") or 1)
            stop_price = premium_open * stop_multiple

            # Only trigger when we'd close at a LOSS larger than the
            # configured stop multiple. mid <= stop_price means the
            # position isn't in stop-loss territory yet.
            if mid < stop_price:
                continue
            if not _backoff_check(project_id, sym):
                continue
            # Don't double-submit if a buy-to-close on this exact
            # symbol is already pending at the broker. Stuck day-orders
            # over a weekend can otherwise pile up 10+ duplicates.
            try:
                from risk.order_guard import has_pending_close_for_symbol
                if has_pending_close_for_symbol(
                        client, project_id, sym, "buy"):
                    continue
            except Exception:
                pass

            unrealized_loss = (mid - premium_open) * 100 * qty
            attempt: dict[str, Any] = {
                "ticker":          c["ticker"],
                "option_symbol":   sym,
                "qty":             qty,
                "premium_open":    premium_open,
                "current_mid":     mid,
                "stop_multiple":   stop_multiple,
                "stop_trigger_price": round(stop_price, 2),
                "unrealized_loss": round(unrealized_loss, 2),
            }
            # Aggressive pricing: when in stop-loss territory we WANT
            # to fill. Saving $0.05/contract isn't worth holding a
            # losing position another minute. Bid at ask × 1.02 so
            # the order is marketable; falls back to mid when ask
            # is unreasonably wide vs bid.
            close_price = round(mid, 2)
            if aggressive and ask > 0:
                # Cap the cross at 5% above ask to avoid pathological
                # bids on illiquid 1-strike-wide chains.
                marketable = round(min(ask * 1.02, ask + 0.50), 2)
                close_price = marketable
            attempt["close_limit_price"] = close_price
            if dry_run:
                attempt["status"] = "DRY_RUN"
            else:
                try:
                    order = client.submit_limit_option(
                        option_symbol=sym, qty=qty, side="buy",
                        limit_price=close_price,
                        time_in_force=tif,
                    )
                    attempt["status"] = "SUBMITTED"
                    attempt["order"] = order
                    _backoff_record(project_id, sym, ok=True)
                except Exception as e:
                    attempt["status"] = "ERROR"
                    attempt["error"] = str(e)[:200]
                    _backoff_record(project_id, sym, ok=False)
            actions.append(attempt)
            EventsRepo.log(
                project_id, "Defense", "STOP_LOSS", {
                    **attempt,
                    "narrative": [
                        f"Stop-loss fired on {c['ticker']} {sym}.",
                        f"Sold for ${premium_open:.2f}, now ${mid:.2f} "
                        f"mid (≥ stop multiple {stop_multiple}× = "
                        f"${stop_price:.2f}).",
                        f"Unrealized loss ${unrealized_loss:,.0f}. "
                        f"Buying back {qty} contract(s) to cap it.",
                    ],
                })
            if (not dry_run
                    and attempt.get("status") == "SUBMITTED"):
                try:
                    from notifications.dispatcher import notify_event
                    notify_event(project_id, "STOP_LOSS", attempt)
                except Exception:
                    logger.exception("notifier failed on STOP_LOSS")
    return actions
