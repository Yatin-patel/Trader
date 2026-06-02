"""Take-profit auto-close.

For each open short option, fetch the current mid-price. If the profit
captured so far is ≥ `close_at_profit_pct * premium_collected`, submit a
buy-to-close limit order.

Example: sold a put for $1.00. close_at_profit_pct=0.50 → buy back when
the option trades at ≤ $0.50 (we've kept $0.50 of premium = 50% of max).
"""
from __future__ import annotations

import logging
import time
from typing import Any

from db.repositories import EventsRepo, ProjectsRepo, WheelRepo
from db.settings_store import ProjectSettings
from execution import AlpacaClient
from risk.greeks_agg import _extract_underlying

logger = logging.getLogger(__name__)

# Back-off tracker — keyed by (project_id, option_symbol).
# After 3 consecutive errors on the same contract, suppress further
# attempts for _BACKOFF_SECONDS. Cleared in-process only (lost on restart),
# which is fine: a restart usually means something changed anyway.
_BACKOFF_FAILS = 3
_BACKOFF_SECONDS = 1800   # 30 min
_backoff: dict[tuple[str, str], dict[str, float]] = {}


def _backoff_check(project_id: str, sym: str) -> tuple[bool, float]:
    """Return (allowed, retry_after_seconds)."""
    state = _backoff.get((project_id, sym))
    if not state:
        return True, 0.0
    if state["fails"] < _BACKOFF_FAILS:
        return True, 0.0
    elapsed = time.time() - state["paused_at"]
    if elapsed >= _BACKOFF_SECONDS:
        # Reset and allow retry.
        _backoff.pop((project_id, sym), None)
        return True, 0.0
    return False, _BACKOFF_SECONDS - elapsed


def _backoff_record(project_id: str, sym: str, ok: bool) -> None:
    key = (project_id, sym)
    if ok:
        _backoff.pop(key, None)
        return
    state = _backoff.setdefault(key, {"fails": 0, "paused_at": 0.0})
    state["fails"] += 1
    if state["fails"] >= _BACKOFF_FAILS:
        state["paused_at"] = time.time()


def evaluate_take_profit(project_id: str) -> list[dict[str, Any]]:
    """Return list of close attempts (one per qualifying contract)."""
    if not ProjectSettings.get(project_id, "take_profit_enabled", default=True):
        return []
    target_pct = float(ProjectSettings.get(project_id, "close_at_profit_pct", default=0.50))
    if target_pct <= 0 or target_pct >= 1:
        return []

    project = ProjectsRepo.get(project_id)
    if project is None:
        return []
    client = AlpacaClient(project)
    open_contracts = WheelRepo.list_open(project_id)
    if not open_contracts:
        return []

    dry_run = bool(ProjectSettings.get(project_id, "dry_run"))
    tif = str(ProjectSettings.get(project_id, "order_time_in_force") or "day")

    # Group by underlying so we make one chain call per ticker.
    by_underlying: dict[str, list[dict[str, Any]]] = {}
    for c in open_contracts:
        sym = c.get("option_symbol")
        if not sym:
            continue
        by_underlying.setdefault(_extract_underlying(sym), []).append(c)

    actions: list[dict[str, Any]] = []
    for underlying, contracts in by_underlying.items():
        try:
            chain = client.option_chain_quotes(underlying)
        except Exception as e:
            logger.warning("take-profit chain fetch failed for %s: %s", underlying, e)
            continue
        for c in contracts:
            sym = c["option_symbol"]
            quote = chain.get(sym) or {}
            ask = quote.get("ask") or 0
            bid = quote.get("bid") or 0
            if ask <= 0:
                continue
            mid = (bid + ask) / 2
            premium_open = float(c["premium_collected"])
            qty = int(c.get("quantity") or 1)
            target_close_price = premium_open * (1 - target_pct)

            if mid > target_close_price:
                continue

            # Back-off: skip contracts that have failed repeatedly.
            allowed, retry_in = _backoff_check(project_id, sym)
            if not allowed:
                continue

            # Time to take profit.
            attempt = {
                "ticker": c["ticker"],
                "option_symbol": sym,
                "qty": qty,
                "premium_open": premium_open,
                "current_mid": mid,
                "target_close_price": target_close_price,
                "profit_pct_so_far": ((premium_open - mid) / premium_open) if premium_open else 0,
            }
            if dry_run:
                attempt["status"] = "DRY_RUN"
            else:
                try:
                    order = client.submit_limit_option(
                        option_symbol=sym, qty=qty, side="buy",
                        limit_price=round(mid, 2), time_in_force=tif,
                    )
                    attempt["status"] = "SUBMITTED"
                    attempt["order"] = order
                    _backoff_record(project_id, sym, ok=True)
                except Exception as e:
                    attempt["status"] = "ERROR"
                    attempt["error"] = str(e)
                    _backoff_record(project_id, sym, ok=False)
                    state = _backoff.get((project_id, sym), {})
                    if state.get("fails", 0) >= _BACKOFF_FAILS:
                        attempt["backoff_until_seconds"] = _BACKOFF_SECONDS
            actions.append(attempt)
            tp_payload = {
                **attempt,
                "narrative": [
                    f"Take-profit fired on {c['ticker']} {sym}.",
                    f"Sold for ${premium_open:.2f}, now ${mid:.2f} mid → "
                    f"captured {attempt['profit_pct_so_far']*100:.0f}% of max "
                    f"(target {target_pct*100:.0f}%).",
                    f"Buying back {qty} contract(s) at ${mid:.2f}.",
                ],
            }
            EventsRepo.log(project_id, "TakeProfit", "BUY_TO_CLOSE", tp_payload)
            if not dry_run and attempt.get("status") == "SUBMITTED":
                try:
                    from notifications.dispatcher import notify_event
                    notify_event(project_id, "BUY_TO_CLOSE", tp_payload)
                except Exception:
                    logger.exception("notifier failed on BUY_TO_CLOSE")
    return actions
