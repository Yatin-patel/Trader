"""Single-tenant async worker.

Owns an isolated LangGraph compiled instance and an independent loop.

Market-hours enforcement
------------------------
The worker REFUSES to run a cycle outside US equity/options regular trading
hours. The check happens at two layers:

  1. Hard gate at the top of every cycle: query Alpaca's market clock. If
     `is_open` is false, sleep until `next_open` (capped at 1h for resilience
     to clock drift / weekend spans) and try again. No scan, no LLM call,
     no order submission ever occurs while the market is closed.
  2. A short process-level cache (~10s) keeps simultaneous tenants from
     hammering the Alpaca clock endpoint.

The `market_hours_only` global setting controls only whether to *report*
the wait — it is treated as always-on for trade safety.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any

from db.repositories import EventsRepo, ProjectsRepo
from db.settings_store import AppSettings, ProjectSettings
from execution import AlpacaClient
from orchestration import build_graph

logger = logging.getLogger(__name__)


# Process-wide clock cache: tenants share it to avoid hammering Alpaca.
_CLOCK_CACHE: dict[str, Any] = {"ts": 0.0, "value": None}
_CLOCK_TTL_SECONDS = 10.0

# Safety cap: never sleep more than this in one wait, so a stale clock or
# config edit can recover within an hour.
_MAX_WAIT_SECONDS = 3600


def _get_cached_clock(client: AlpacaClient) -> dict[str, Any]:
    now = time.monotonic()
    if _CLOCK_CACHE["value"] is not None and (now - _CLOCK_CACHE["ts"]) < _CLOCK_TTL_SECONDS:
        return _CLOCK_CACHE["value"]
    clock = client.get_market_clock()
    _CLOCK_CACHE["value"] = clock
    _CLOCK_CACHE["ts"] = now
    return clock


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
        logger.info("tenant worker started: %s", self.project_id)
        while not self._stop_event.is_set():
            wait_seconds = await self._tick()
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=wait_seconds)
            except asyncio.TimeoutError:
                pass
        logger.info("tenant worker stopped: %s", self.project_id)

    async def _tick(self) -> int:
        """Run one cycle if the market is open; otherwise return seconds to wait."""
        try:
            return await self._run_one_cycle()
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

        client = AlpacaClient(project)

        # --- HARD GATE: market hours -----------------------------------------
        clock = _get_cached_clock(client)
        if not clock.get("is_open"):
            wait = _seconds_until(clock.get("next_open"))
            EventsRepo.log(self.project_id, "Worker", "LOOP", {
                "skipped": "market_closed",
                "next_open": str(clock.get("next_open")),
                "sleeping_seconds": wait,
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
        EventsRepo.log(self.project_id, "Worker", "LOOP", {
            "cycle": self._cycle_count,
            "final_status": final_state.get("execution_status"),
            "trades": len(final_state.get("selected_trades") or []),
        })

        # --- Analytics: closure detection + portfolio snapshot ---------------
        try:
            from analytics.closure_detector import detect_closures
            from analytics.snapshotter import take_snapshot
            await asyncio.to_thread(detect_closures, self.project_id)
            await asyncio.to_thread(take_snapshot, self.project_id)
        except Exception as e:
            logger.exception("analytics step failed: %s", e)

        # --- Position management: take-profit + auto-roll --------------------
        try:
            from risk.take_profit import evaluate_take_profit
            from risk.auto_roll import evaluate_auto_roll
            await asyncio.to_thread(evaluate_take_profit, self.project_id)
            await asyncio.to_thread(evaluate_auto_roll, self.project_id)
        except Exception as e:
            logger.exception("position-mgmt step failed: %s", e)

        # If market closes mid-cycle, sleep until next open instead of looping fast.
        # But only if we actually have a future `next_open` — a transient clock
        # hiccup with next_open=None should not silence the worker for an hour.
        clock_after = _get_cached_clock(client)
        if not clock_after.get("is_open"):
            next_open = clock_after.get("next_open")
            if next_open:
                return max(_seconds_until(next_open), 30)
            # Unknown next_open: just re-check at the regular cycle cadence.
            return self._cycle_interval()
        return self._cycle_interval()
