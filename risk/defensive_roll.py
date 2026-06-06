"""Defensive roll on tested-but-not-broken shorts.

When a short option is uncomfortable (high delta, low DTE) but not
yet at the hard stop-loss multiple, rolling down + out for a credit
turns a loser-in-progress into a future winner:

  * lower strike   → less assignment risk
  * later expiry   → more theta to collect + more time for the
                     underlying to mean-revert
  * net credit > 0 → we don't pay to "stay alive"

This is the maneuver every options trader does manually. Now the
platform does it automatically.

Trigger
-------
Short option is in the "defensive zone" when ALL of:
  * abs(delta) >= defensive_roll_delta_threshold
  * DTE <= defensive_roll_max_dte
  * stop-loss has NOT fired (mid < stop_multiple × premium_open)
  * project is not in DRY_RUN

Execution
---------
1. Pick a roll target on the same underlying at a lower strike
   (5% below current price for a put / 5% above for a call) and
   the configured roll-out DTE.
2. Compute net_credit = new_premium − cost_to_close_existing.
3. If net_credit < 0, ABORT — don't pay to stay in a losing trade.
4. Otherwise: sell-to-open new, then buy-to-close old. Log
   Defense.ROLL with the delta + credit numbers.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any

from db.repositories import EventsRepo, ProjectsRepo, WheelRepo
from db.settings_store import ProjectSettings
from execution import get_broker
from risk.greeks_agg import _extract_underlying

logger = logging.getLogger(__name__)


def _find_roll_target(
    client: Any,
    underlying: str,
    is_put: bool,
    underlying_price: float,
    target_strike: float,
    min_dte: int,
    max_dte: int,
    target_delta_lo: float,
    target_delta_hi: float,
) -> dict[str, Any] | None:
    """Find a roll-target contract that:
        * is on the same underlying
        * is on the same side (put or call)
        * has a strike near ``target_strike``
        * has DTE in [min_dte, max_dte]
        * has |delta| in [target_delta_lo, target_delta_hi]
    Returns the highest-mid (most premium-rich) match or None."""
    side_str = "put" if is_put else "call"
    try:
        contracts = client.list_option_contracts(
            underlying, side_str, min_dte, max_dte,
            min_strike=target_strike * 0.92,
            max_strike=target_strike * 1.05,
            limit=80,
        )
    except Exception as e:
        logger.info("roll target list_option_contracts failed: %s", e)
        return None
    if not contracts:
        return None
    try:
        quotes = client.option_chain_quotes(underlying)
    except Exception:
        quotes = {}

    candidates: list[dict[str, Any]] = []
    for c in contracts:
        sym = c.get("symbol")
        if not sym:
            continue
        q = quotes.get(sym) or {}
        delta = q.get("delta")
        if delta is None:
            continue
        d_abs = abs(float(delta))
        if not (target_delta_lo <= d_abs <= target_delta_hi):
            continue
        bid = float(q.get("bid") or 0)
        ask = float(q.get("ask") or 0)
        if bid <= 0 or ask <= 0:
            continue
        mid = (bid + ask) / 2
        # Tight spread check — we don't want to roll into an
        # illiquid contract.
        if (ask - bid) / mid > 0.30:
            continue
        candidates.append({
            **c, **q,
            "mid":   mid,
            "delta_abs": d_abs,
        })

    if not candidates:
        return None
    # Pick the one with the most premium per dollar of strike.
    candidates.sort(
        key=lambda x: -x["mid"] / max(float(x["strike"]), 1.0))
    return candidates[0]


def evaluate_defensive_roll(
    project_id: str,
) -> list[dict[str, Any]]:
    """Roll tested-but-not-broken shorts for credit. Returns one
    action dict per roll attempt."""
    if not bool(ProjectSettings.get(
            project_id, "defensive_roll_enabled", default=True)):
        return []
    delta_threshold = float(ProjectSettings.get(
        project_id, "defensive_roll_delta_threshold",
        default=0.50) or 0.50)
    max_dte = int(ProjectSettings.get(
        project_id, "defensive_roll_max_dte", default=14) or 14)
    roll_to_dte = int(ProjectSettings.get(
        project_id, "defensive_roll_target_dte", default=30) or 30)
    target_strike_pct = float(ProjectSettings.get(
        project_id, "defensive_roll_strike_offset_pct",
        default=0.05) or 0.05)
    # Don't roll if the position is already past the hard stop-loss
    # threshold — that's the stop-loss module's job.
    stop_multiple = float(ProjectSettings.get(
        project_id, "option_stop_loss_multiple",
        default=2.0) or 2.0)

    project = ProjectsRepo.get(project_id)
    if project is None:
        return []
    try:
        client = get_broker(project)
    except Exception as e:
        logger.warning("defensive roll broker fetch failed: %s", e)
        return []
    open_contracts = WheelRepo.list_open(project_id)
    if not open_contracts:
        return []

    dry_run = bool(ProjectSettings.get(project_id, "dry_run"))
    # Defensive close orders use GTC by default so they survive past
    # 4pm if liquidity is thin. The new sell-to-open leg uses normal
    # `order_time_in_force` since it's an opening trade.
    open_tif = str(ProjectSettings.get(
        project_id, "order_time_in_force") or "day")
    close_tif = str(ProjectSettings.get(
        project_id, "defensive_close_tif", default="gtc") or "gtc")
    aggressive = bool(ProjectSettings.get(
        project_id, "defensive_close_aggressive_pricing",
        default=True))

    actions: list[dict[str, Any]] = []
    today = date.today()

    by_underlying: dict[str, list[dict[str, Any]]] = {}
    for c in open_contracts:
        sym = c.get("option_symbol")
        if not sym:
            continue
        by_underlying.setdefault(
            _extract_underlying(sym), []).append(c)

    for underlying, contracts in by_underlying.items():
        try:
            chain = client.option_chain_quotes(underlying)
            snap = client.snapshots([underlying]).get(underlying)
        except Exception as e:
            logger.info("defensive roll fetch failed for %s: %s",
                        underlying, e)
            continue
        if snap is None:
            continue
        underlying_price = float(getattr(snap, "last_price", 0) or 0)
        if underlying_price <= 0:
            continue

        for c in contracts:
            sym = c["option_symbol"]
            phase = c.get("strategy_phase") or ""
            is_put = phase == "CASH_SECURED_PUT"
            is_call = phase == "COVERED_CALL"
            if not (is_put or is_call):
                continue
            quote = chain.get(sym) or {}
            delta = quote.get("delta")
            if delta is None:
                continue
            d_abs = abs(float(delta))
            if d_abs < delta_threshold:
                continue

            exp = c.get("expiration_date")
            if not exp:
                continue
            dte = (exp - today).days
            if dte > max_dte or dte < 0:
                continue

            ask = float(quote.get("ask") or 0)
            bid = float(quote.get("bid") or 0)
            if ask <= 0:
                continue
            mid = (bid + ask) / 2

            premium_open = float(c["premium_collected"])
            # Stop-loss zone — let stop_loss module handle it.
            if (premium_open > 0
                    and mid >= premium_open * stop_multiple):
                continue

            # Pick a roll target strike: 5% OTM from current
            # underlying. Roll-out DTE: today + roll_to_dte.
            if is_put:
                target_strike = underlying_price * (
                    1 - target_strike_pct)
            else:
                target_strike = underlying_price * (
                    1 + target_strike_pct)
            target = _find_roll_target(
                client, underlying, is_put,
                underlying_price, target_strike,
                min_dte=max(7, roll_to_dte - 7),
                max_dte=roll_to_dte + 14,
                target_delta_lo=0.20, target_delta_hi=0.35,
            )
            if target is None:
                continue

            qty = int(c.get("quantity") or 1)
            new_credit_per = float(target["mid"])
            cost_to_close = mid
            net_credit = (new_credit_per - cost_to_close) * 100 * qty
            if net_credit <= 0:
                continue  # not rolling into a loss

            # Skip if either leg of this roll is already pending at
            # the broker — prevents the duplicate-order pile-up that
            # surfaced over the 2026-06-05 weekend (10 stacked
            # buy-to-close orders for the same contract).
            try:
                from risk.order_guard import (
                    has_pending_close_for_symbol)
                if (has_pending_close_for_symbol(
                        client, project_id, sym, "buy")
                    or has_pending_close_for_symbol(
                        client, project_id,
                        target.get("symbol", ""), "sell")):
                    continue
            except Exception:
                pass

            attempt: dict[str, Any] = {
                "ticker":             c["ticker"],
                "old_symbol":         sym,
                "old_strike":         float(c.get("strike_price") or 0),
                "old_delta":          d_abs,
                "old_dte":            dte,
                "old_premium_open":   premium_open,
                "old_mid":            round(mid, 2),
                "new_symbol":         target.get("symbol"),
                "new_strike":         float(target.get("strike") or 0),
                "new_delta":          float(target.get("delta_abs") or 0),
                "new_premium":        round(new_credit_per, 2),
                "qty":                qty,
                "net_credit":         round(net_credit, 2),
            }
            if dry_run:
                attempt["status"] = "DRY_RUN"
                actions.append(attempt)
                EventsRepo.log(
                    project_id, "Defense", "ROLL", {
                        **attempt,
                        "narrative": _narrate(attempt, is_put),
                    })
                continue

            # Submit new short FIRST. If that fails we abort — better
            # to leave the old position open than to be flat-and-naked
            # at a worse moment.
            try:
                new_order = client.submit_limit_option(
                    option_symbol=target["symbol"],
                    qty=qty, side="sell",
                    limit_price=round(new_credit_per, 2),
                    time_in_force=open_tif,
                )
                attempt["new_order"] = new_order
            except Exception as e:
                attempt["status"] = "ERROR_NEW_LEG"
                attempt["error"] = str(e)[:200]
                actions.append(attempt)
                EventsRepo.log(
                    project_id, "Defense", "ROLL", attempt)
                continue

            # Close the old. If THIS fails we have a doubled-up
            # position briefly — log loudly so the user can intervene.
            # Aggressive pricing on the buy-to-close so we actually
            # fill (no point rolling if the old leg sits open at a
            # missed bid).
            close_price = round(mid, 2)
            if aggressive and ask > 0:
                close_price = round(min(ask * 1.02, ask + 0.50), 2)
            attempt["close_limit_price"] = close_price
            try:
                close_order = client.submit_limit_option(
                    option_symbol=sym, qty=qty, side="buy",
                    limit_price=close_price,
                    time_in_force=close_tif,
                )
                attempt["close_order"] = close_order
                attempt["status"] = "ROLLED"
            except Exception as e:
                attempt["status"] = "ERROR_CLOSE_LEG"
                attempt["error"] = str(e)[:200]
            actions.append(attempt)
            EventsRepo.log(project_id, "Defense", "ROLL", {
                **attempt,
                "narrative": _narrate(attempt, is_put),
            })
            try:
                from notifications.dispatcher import notify_event
                notify_event(project_id, "ROLL", attempt)
            except Exception:
                logger.exception("notifier failed on ROLL")
    return actions


def _narrate(a: dict[str, Any], is_put: bool) -> list[str]:
    arrow = "down" if is_put else "up"
    return [
        f"Defensive roll on {a['ticker']}: tested "
        f"{a['old_symbol']} (Δ {a['old_delta']:.2f}, "
        f"{a['old_dte']} DTE)",
        f"  Rolled {arrow} + out to {a['new_symbol']} "
        f"(strike ${a['new_strike']:.2f}, Δ {a['new_delta']:.2f})",
        f"  Buy-to-close cost ${a['old_mid']:.2f}, new credit "
        f"${a['new_premium']:.2f}, NET CREDIT "
        f"${a['net_credit']:+,.2f} for {a['qty']} contract(s).",
    ]
