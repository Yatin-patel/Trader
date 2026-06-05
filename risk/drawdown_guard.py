"""Daily-drawdown circuit breaker.

When the account is down more than ``max_daily_drawdown_pct`` of
prior-close equity for the trading day, BLOCK new position opens
until the next UTC midnight. Existing positions can still be
closed (take-profit, stop-loss, manual close) — we never trap
the user; we just stop adding to a losing day.

Rationale
---------
Yatin-Test1 was down $611 today and the platform was still happy
to open more short puts on the next cycle. That's the classic
"averaging into a losing trade" mistake. A circuit breaker forces
a cooling-off period so the user can review.

Trade-off
---------
This will SOMETIMES skip a great setup that appears mid-drawdown.
But the worst-case (sustained loss day, keep opening) is much
worse than the best-case (miss one good open). The breaker is
deliberately ASYMMETRIC.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from db.repositories import EventsRepo, ProjectsRepo
from db.settings_store import ProjectSettings

logger = logging.getLogger(__name__)

# Tiny per-process cache to avoid hitting the broker on every cycle.
# Keyed by project_id, value is (timestamp, last_check_result).
_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_CACHE_TTL_SECONDS = 60.0


def evaluate_drawdown(project_id: str) -> dict[str, Any]:
    """Compute today's account drawdown vs prior close.

    Returns a dict:
        {
          "paused":  bool,              # true -> block new opens
          "today_change": float | None, # USD change since prior close
          "today_pct":    float | None, # % of last_equity
          "threshold_pct": float,
          "narrative": [str, ...],
        }
    Result is cached 60 s per project so the worker doesn't hit
    the broker every cycle.
    """
    import time as _time

    cached = _CACHE.get(project_id)
    now = _time.monotonic()
    if cached and (now - cached[0]) < _CACHE_TTL_SECONDS:
        return cached[1]

    if not bool(ProjectSettings.get(
            project_id, "drawdown_breaker_enabled", default=True)):
        result = {"paused": False, "today_change": None,
                  "today_pct": None,
                  "threshold_pct": 0.0,
                  "narrative": ["drawdown breaker disabled"]}
        _CACHE[project_id] = (now, result)
        return result

    threshold_pct = float(ProjectSettings.get(
        project_id, "max_daily_drawdown_pct", default=0.03) or 0.03)
    if threshold_pct <= 0:
        result = {"paused": False, "today_change": None,
                  "today_pct": None,
                  "threshold_pct": 0.0,
                  "narrative": ["threshold disabled (0)"]}
        _CACHE[project_id] = (now, result)
        return result

    project = ProjectsRepo.get(project_id)
    if project is None:
        result = {"paused": False, "today_change": None,
                  "today_pct": None,
                  "threshold_pct": threshold_pct,
                  "narrative": ["project not found"]}
        _CACHE[project_id] = (now, result)
        return result

    try:
        from execution import BrokerReauthRequired, get_broker
        client = get_broker(project)
        account = client.get_account()
    except BrokerReauthRequired:
        # When the broker can't be reached we err on the side of
        # ALLOWING opens — better to over-open than to hard-block
        # the whole pipeline behind a transient credential issue.
        result = {"paused": False, "today_change": None,
                  "today_pct": None,
                  "threshold_pct": threshold_pct,
                  "narrative": ["broker reauth required; "
                                "allowing opens"]}
        _CACHE[project_id] = (now, result)
        return result
    except Exception as e:
        logger.warning(
            "drawdown breaker broker fetch failed for %s: %s",
            project_id, e)
        result = {"paused": False, "today_change": None,
                  "today_pct": None,
                  "threshold_pct": threshold_pct,
                  "narrative": [f"broker fetch failed: {e}"]}
        _CACHE[project_id] = (now, result)
        return result

    equity = float(account.get("equity") or 0)
    last_equity = float(account.get("last_equity") or 0)
    if equity <= 0 or last_equity <= 0:
        # No prior-close reference (new account, weekend, etc.) —
        # don't pause. Trader can still set a hard cash buffer
        # if they want the safety.
        result = {"paused": False, "today_change": None,
                  "today_pct": None,
                  "threshold_pct": threshold_pct,
                  "narrative": ["no last_equity reference"]}
        _CACHE[project_id] = (now, result)
        return result

    change = equity - last_equity
    pct = change / last_equity
    paused = pct <= -threshold_pct

    today_label = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
    narrative = [
        f"{today_label}: equity ${equity:,.2f}, prior close "
        f"${last_equity:,.2f}, change ${change:+,.2f} "
        f"({pct*100:+.2f}%)."
    ]
    if paused:
        narrative.append(
            f"PAUSED: drawdown {abs(pct)*100:.2f}% has crossed the "
            f"{threshold_pct*100:.2f}% daily cap. New opens blocked "
            f"until UTC midnight. Take-profit + stop-loss still run."
        )
    result = {
        "paused":        paused,
        "today_change":  round(change, 2),
        "today_pct":     round(pct * 100, 4),
        "threshold_pct": round(threshold_pct * 100, 2),
        "narrative":     narrative,
    }
    _CACHE[project_id] = (now, result)

    # Only log the pause event ONCE per cycle transition so we don't
    # spam the activity feed every minute the worker checks.
    if paused:
        try:
            EventsRepo.log(project_id, "Defense", "DRAWDOWN_PAUSE",
                           result)
        except Exception:
            logger.exception("failed to log DRAWDOWN_PAUSE")
    return result


def should_pause_opens(project_id: str) -> tuple[bool, str]:
    """Convenience wrapper used by the strategist / executor at the
    decision point. Returns (paused, human_reason)."""
    r = evaluate_drawdown(project_id)
    if not r.get("paused"):
        return False, ""
    return True, r.get("narrative", ["drawdown cap hit"])[-1]
