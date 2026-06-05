"""Single-tenant async worker.

Owns an isolated LangGraph compiled instance and an independent loop.

Market-hours enforcement
------------------------
The worker REFUSES to run a cycle outside US equity/options trading hours.
The check happens at two layers:

  1. Hard gate at the top of every cycle: query Alpaca's market clock. If
     `is_open` is false, the worker normally sleeps until `next_open`.
     Exception: when the project's `use_extended_hours` setting is True
     AND the current US/Eastern time is inside the extended-hours window
     (04:00-20:00 ET) on a trading day, the cycle is allowed to run so
     the executor can submit `extended_hours=True` orders.
  2. A short process-level cache (~10s) keeps simultaneous tenants from
     hammering the Alpaca clock endpoint.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, time as dtime, timezone
from typing import Any

try:
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo("America/New_York")
except Exception:
    _ET = None

from db.repositories import EventsRepo, ProjectsRepo
from db.settings_store import AppSettings, ProjectSettings
from execution import BrokerClient, BrokerReauthRequired, get_broker
from orchestration import build_graph

logger = logging.getLogger(__name__)


# Process-wide clock cache: tenants share it to avoid hammering Alpaca.
_CLOCK_CACHE: dict[str, Any] = {"ts": 0.0, "value": None}
_CLOCK_TTL_SECONDS = 10.0

# Safety cap on a single wait. Kept short so a settings change (e.g. enabling
# use_extended_hours during pre-market) propagates within a few minutes.
_MAX_WAIT_SECONDS = 300


def _get_cached_clock(client: BrokerClient) -> dict[str, Any]:
    now = time.monotonic()
    if _CLOCK_CACHE["value"] is not None and (now - _CLOCK_CACHE["ts"]) < _CLOCK_TTL_SECONDS:
        return _CLOCK_CACHE["value"]
    clock = client.get_market_clock()
    _CLOCK_CACHE["value"] = clock
    _CLOCK_CACHE["ts"] = now
    return clock


# Process-wide trading-calendar cache (per AlpacaClient instance, keyed by
# base URL) — refreshed daily.
_CAL_CACHE: dict[str, tuple[float, set[str]]] = {}
_CAL_TTL_SECONDS = 6 * 3600


def _trading_days(client: BrokerClient) -> set[str]:
    """ISO-date strings (YYYY-MM-DD) for the next few trading sessions."""
    key = getattr(client.project, "alpaca_base_url", "") or "default"
    now = time.monotonic()
    hit = _CAL_CACHE.get(key)
    if hit is not None and (now - hit[0]) < _CAL_TTL_SECONDS:
        return hit[1]
    try:
        cal = client.get_calendar(days=7)
        days = {str(c.get("date")) for c in cal if c.get("date")}
    except Exception:
        days = set()
    _CAL_CACHE[key] = (now, days)
    return days


def _in_extended_hours_window(client: BrokerClient) -> bool:
    """True if current US/Eastern time is in [04:00, 20:00) on a trading day.

    Returns False if zoneinfo is unavailable or the calendar lookup fails —
    we'd rather skip a cycle than submit during a holiday.
    """
    if _ET is None:
        return False
    et = datetime.now(_ET)
    if not (dtime(4, 0) <= et.time() < dtime(20, 0)):
        return False
    today_iso = et.date().isoformat()
    return today_iso in _trading_days(client)


def _seconds_until(next_open: Any) -> int:
    if next_open is None:
        return _MAX_WAIT_SECONDS
    try:
        if isinstance(next_open, datetime):
            target = next_open
        else:
            target = datetime.fromisoformat(str(next_open).replace("Z", "+00:00"))
        if target.tzinfo is None:
            target = target.replace(tzinfo=timezone.utc)
        delta = (target - datetime.now(tz=timezone.utc)).total_seconds()
        if delta < 0:
            return 0
        return min(int(delta) + 1, _MAX_WAIT_SECONDS)
    except Exception:
        return _MAX_WAIT_SECONDS


class TenantWorker:
    def __init__(self, project_id: str):
        self.project_id = project_id
        self._graph = build_graph()
        self._stop_event = asyncio.Event()
        self._cycle_count = 0

    def stop(self) -> None:
        self._stop_event.set()

    async def run_forever(self) -> None:
        """Never let an exception escape this loop. Yesterday three
        workers silently died after 20:53 ET and didn't tick again for
        12 hours — anything that would crash the task gets logged and
        we sleep + continue. The runner's watchdog still restarts a
        task that .done() returns True for, so if the process is so
        deeply hosed it can't even reach this except, the watchdog
        catches it. Belt + suspenders."""
        logger.info("tenant worker started: %s", self.project_id)
        try:
            while not self._stop_event.is_set():
                try:
                    wait_seconds = await self._tick()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # _tick() already catches its own exceptions and
                    # logs a Worker.ERROR row. If something gets past
                    # it (network blip during the EventsRepo.log call,
                    # DB connection drop, etc.) we still want the loop
                    # alive. Sleep one cycle and try again.
                    logger.exception(
                        "tenant worker tick crashed for %s — "
                        "loop continues",
                        self.project_id,
                    )
                    try:
                        EventsRepo.log(
                            self.project_id, "Worker", "ERROR",
                            {"err": "unhandled exception in tick"},
                        )
                    except Exception:
                        pass
                    wait_seconds = self._cycle_interval()
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(), timeout=wait_seconds)
                except asyncio.TimeoutError:
                    pass
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception(
                        "tenant worker sleep crashed for %s — "
                        "loop continues",
                        self.project_id,
                    )
        except asyncio.CancelledError:
            logger.info("tenant worker cancelled: %s", self.project_id)
            raise
        finally:
            logger.info("tenant worker stopped: %s", self.project_id)

    async def _tick(self) -> int:
        """Run one cycle if the market is open; otherwise return seconds to wait."""
        try:
            return await self._run_one_cycle()
        except BrokerReauthRequired as e:
            # ETrade tokens past renewal — the cycle can't possibly run
            # without re-OAuth. Log a clean LOOP-skip (NOT an ERROR) so
            # the activity feed shows "waiting for reconnect" rather
            # than a stream of stack traces, and so the watchdog
            # doesn't try to restart a worker that's actually healthy.
            EventsRepo.log(self.project_id, "Worker", "LOOP", {
                "skipped": "broker_reauth_required",
                "detail": str(e),
            })
            return max(self._cycle_interval(), 300)
        except Exception as e:
            logger.exception("cycle failed for %s: %s", self.project_id, e)
            EventsRepo.log(self.project_id, "Worker", "ERROR", {"err": str(e)})
            return self._cycle_interval()

    def _cycle_interval(self) -> int:
        per_project = ProjectSettings.get(self.project_id, "loop_interval_seconds", default=None)
        if per_project:
            return int(per_project)
        return int(AppSettings.get("loop_interval_seconds", 60))

    async def _run_one_cycle(self) -> int:
        project = ProjectsRepo.get(self.project_id)
        if project is None or not project.is_active:
            return self._cycle_interval()

        # --- Strategy mode gate ----------------------------------------------
        # The Scanner→Strategist→Guardrail→Executor pipeline runs for any
        # mode whose strategist produces option trades. DCA-only and
        # Paused short-circuit here; everything else flows through.
        # Reconcile + DCA + Rebalancer + Optimizer all run on their own
        # MultiTenantRunner schedules so they keep working regardless.
        mode = str(ProjectSettings.get(self.project_id, "strategy_mode",
                                       default="wheel") or "wheel").lower()
        if mode == "paused":
            EventsRepo.log(self.project_id, "Worker", "LOOP", {
                "skipped": "strategy_mode_paused",
                "mode": mode,
            })
            return self._cycle_interval()
        if mode == "dca_only":
            EventsRepo.log(self.project_id, "Worker", "LOOP", {
                "skipped": "strategy_mode_dca_only",
                "mode": mode,
                "note": "DCA buys still run on the hourly scheduler",
            })
            return self._cycle_interval()
        # Every other mode (wheel, wheel_plus_dca, bull_put_spread,
        # bear_call_spread, bull_call_spread, bear_put_spread,
        # iron_condor, calendar_spread, intraday_momentum) flows through
        # the same graph — the dispatcher inside the Strategist node
        # routes on strategy_mode to the right per-strategy logic.

        client = get_broker(project)

        # --- Broker auth pre-flight ------------------------------------------
        # One light call to surface BrokerReauthRequired BEFORE the
        # graph runs. Otherwise Scanner / Strategist / Guardrail each
        # independently hit the broker, each catches the exception, and
        # the activity feed gets three duplicate ERROR rows for what is
        # really a single "tokens expired" condition. The exception
        # propagates up to _tick() which logs a single clean LOOP-skip.
        # On a healthy broker this is one balance.json round-trip per
        # cycle (~200ms) — negligible vs the agent work that follows.
        client.get_account()

        # --- HARD GATE: market hours -----------------------------------------
        # Allow the cycle if either:
        #   (a) Alpaca's clock says RTH is open, OR
        #   (b) The project opted into extended-hours trading AND we are
        #       inside the 04:00-20:00 ET extended-hours window on a
        #       trading day.
        clock = _get_cached_clock(client)
        rth_open = bool(clock.get("is_open"))
        allow_ext = bool(ProjectSettings.get(self.project_id,
                                             "use_extended_hours",
                                             default=False))
        ext_open = allow_ext and _in_extended_hours_window(client)
        if not (rth_open or ext_open):
            wait = _seconds_until(clock.get("next_open"))
            EventsRepo.log(self.project_id, "Worker", "LOOP", {
                "skipped": "market_closed",
                "next_open": str(clock.get("next_open")),
                "sleeping_seconds": wait,
                "use_extended_hours": allow_ext,
            })
            return max(wait, 30)  # never check more often than every 30s when closed

        # --- Market is open: run a full cycle --------------------------------
        self._cycle_count += 1
        initial: dict[str, Any] = {
            "project_id": self.project_id,
            "cycle_count": self._cycle_count,
            "target_tickers": [],
            "selected_trades": [],
            "risk_clearance": False,
            "execution_status": "SCANNING",
        }
        final_state = await asyncio.to_thread(self._graph.invoke, initial)
        # NB: the executor clears `selected_trades` from state when it
        # returns (so the graph doesn't loop and re-submit). The trade
        # count for this LOOP must therefore come from execution_results,
        # not selected_trades, or this counter will always read 0 even
        # when orders were actually submitted.
        exec_results = final_state.get("execution_results") or []
        submitted_count = sum(
            1 for r in exec_results
            if str(r.get("status") or "").upper() in ("SUBMITTED", "DRY_RUN")
        )
        EventsRepo.log(self.project_id, "Worker", "LOOP", {
            "cycle": self._cycle_count,
            "final_status": final_state.get("execution_status"),
            "trades": submitted_count,
            "results_count": len(exec_results),
        })

        # --- Analytics: closure detection + portfolio snapshot ---------------
        try:
            from analytics.closure_detector import detect_closures
            from analytics.snapshotter import take_snapshot
            await asyncio.to_thread(detect_closures, self.project_id)
            await asyncio.to_thread(take_snapshot, self.project_id)
        except Exception as e:
            logger.exception("analytics step failed: %s", e)

        # --- Position management: defense layer + take-profit + auto-roll ---
        # Order matters. We run defensive risk BEFORE take-profit so a
        # tested short gets either stop-loss-closed or rolled before the
        # take-profit check, which can prevent both from firing on the
        # same contract in the same cycle.
        try:
            from risk.option_stop_loss import evaluate_stop_loss
            from risk.defensive_roll import evaluate_defensive_roll
            from risk.take_profit import evaluate_take_profit
            from risk.auto_roll import evaluate_auto_roll
            await asyncio.to_thread(evaluate_stop_loss, self.project_id)
            await asyncio.to_thread(
                evaluate_defensive_roll, self.project_id)
            await asyncio.to_thread(evaluate_take_profit, self.project_id)
            await asyncio.to_thread(evaluate_auto_roll, self.project_id)
        except Exception as e:
            logger.exception("position-mgmt step failed: %s", e)

        # If market closes mid-cycle, sleep until next open instead of looping fast.
        # When extended-hours is enabled and we're still inside the ET 04-20
        # window, keep cycling at the normal cadence.
        clock_after = _get_cached_clock(client)
        if not clock_after.get("is_open"):
            if allow_ext and _in_extended_hours_window(client):
                return self._cycle_interval()
            next_open = clock_after.get("next_open")
            if next_open:
                return max(_seconds_until(next_open), 30)
            # Unknown next_open: just re-check at the regular cycle cadence.
            return self._cycle_interval()
        return self._cycle_interval()
