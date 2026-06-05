"""Multi-tenant runner.

Spawns one async task per active project and monitors `trading_projects` for
new tenants on a configurable refresh interval.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import timezone

from db.repositories import ProjectsRepo
from db.settings_store import AppSettings, ProjectSettings

from .tenant_worker import TenantWorker

logger = logging.getLogger(__name__)


class MultiTenantRunner:
    def __init__(self) -> None:
        self._workers: dict[str, TenantWorker] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._stop_event = asyncio.Event()
        # Per-project last-run timestamps for ticks that respect a
        # per-project interval override. monotonic() seconds; 0 = never
        # run yet (so the first tick after startup fires immediately).
        self._last_recon_run: dict[str, float] = {}
        self._last_opt_run: dict[str, float] = {}

    def stop(self) -> None:
        self._stop_event.set()
        for w in self._workers.values():
            w.stop()

    async def run_forever(self) -> None:
        logger.info("multi-tenant runner started")
        self._start_scheduler()
        while not self._stop_event.is_set():
            await self._reconcile()
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=15)
            except asyncio.TimeoutError:
                pass
        for t in list(self._tasks.values()):
            t.cancel()
        try:
            if getattr(self, "_scheduler", None):
                self._scheduler.shutdown(wait=False)
        except Exception:
            pass
        logger.info("multi-tenant runner stopped")

    def _start_scheduler(self) -> None:
        """Run notifications.send_daily_digest for every active project each
        morning at the configured UTC hour. APScheduler is already in
        requirements."""
        try:
            from apscheduler.schedulers.asyncio import AsyncIOScheduler
            from db.repositories import ProjectsRepo
            from db.settings_store import AppSettings
            from notifications.digest import send_daily_digest
        except Exception as e:
            logger.warning("scheduler not started: %s", e)
            return

        sched = AsyncIOScheduler()

        async def _digest_tick():
            if not bool(AppSettings.get("daily_digest_enabled", False)):
                return
            try:
                for proj in ProjectsRepo.list_active():
                    try:
                        send_daily_digest(proj.project_id)
                    except Exception as ex:
                        logger.exception("digest failed for %s: %s",
                                         proj.project_id, ex)
            except Exception as ex:
                logger.exception("digest tick error: %s", ex)

        try:
            hour = int(AppSettings.get("daily_digest_hour_utc", 13))
        except Exception:
            hour = 13
        sched.add_job(_digest_tick, "cron", hour=hour, minute=0,
                      id="daily_digest", replace_existing=True)

        # ---- Nightly DB backup + prune --------------------------------
        async def _backup_tick():
            if not bool(AppSettings.get("backup_enabled", True)):
                return
            try:
                from ops.backups import prune_old_backups, run_backup
                await asyncio.to_thread(run_backup)
                await asyncio.to_thread(prune_old_backups)
            except Exception as ex:
                logger.exception("backup tick error: %s", ex)
        try:
            backup_hour = int(AppSettings.get("backup_hour_utc", 7))
        except Exception:
            backup_hour = 7
        sched.add_job(_backup_tick, "cron", hour=backup_hour, minute=0,
                      id="daily_backup", replace_existing=True)

        # ---- Position reconciliation ---------------------------------
        # Per-project interval: ProjectSettings.reconcile_interval_min
        # falls back to AppSettings.reconcile_interval_min (default 15).
        # The APScheduler tick itself fires every minute and a per-
        # project guard skips projects that aren't due yet — that way
        # one project on a 5-min cadence and another on a 30-min cadence
        # can coexist without running separate scheduler jobs.
        import time as _runner_time

        async def _recon_tick():
            from db.repositories import ProjectsRepo as _PR
            try:
                global_min = int(AppSettings.get(
                    "reconcile_interval_min", 15) or 15)
            except Exception:
                global_min = 15
            now = _runner_time.monotonic()
            try:
                for proj in _PR.list_active():
                    try:
                        per_proj = int(ProjectSettings.get(
                            proj.project_id,
                            "reconcile_interval_min", default=0) or 0)
                    except Exception:
                        per_proj = 0
                    interval_min = per_proj if per_proj > 0 else global_min
                    if interval_min <= 0:
                        continue
                    last = self._last_recon_run.get(proj.project_id, 0.0)
                    if (now - last) < interval_min * 60:
                        continue
                    try:
                        from ops.reconciliation import run_reconciliation
                        await asyncio.to_thread(run_reconciliation,
                                                proj.project_id)
                        self._last_recon_run[proj.project_id] = now
                    except Exception as ex:
                        logger.exception("reconcile failed for %s: %s",
                                         proj.project_id, ex)
            except Exception as ex:
                logger.exception("recon tick error: %s", ex)
        sched.add_job(_recon_tick, "interval", minutes=1,
                      id="reconciliation", replace_existing=True)

        # ---- Continuous Optimizer Agent (every N minutes) ---------
        # Runs intelligence/recommendations against each active project
        # on a fixed cadence. When the project has
        # ``optimizer_auto_apply=True``, safe changes get auto-applied;
        # otherwise the recommendation stays pending for human review
        # in /intelligence. Interval is configurable globally via
        # AppSettings 'optimizer_interval_minutes' (default 30, 0 to
        # disable).
        #
        # MARKET-HOURS GATE: only fire 04:00 - 20:00 ET on trading
        # days (regular session + extended hours). Outside that
        # window the Optimizer is idle — running the LLM on settings
        # during dead hours wastes API spend and the metrics it reads
        # don't change between, say, 23:00 ET and 03:00 ET anyway.
        # The user explicitly asked for this gate.
        # Per-project interval: ProjectSettings.optimizer_interval_minutes
        # falls back to AppSettings.optimizer_interval_minutes (default
        # 30). Same per-project gating pattern as the recon tick.
        async def _optimizer_tick():
            from workers.tenant_worker import _in_extended_hours_window
            from db.repositories import ProjectsRepo as _PR
            from execution import get_broker
            try:
                active = _PR.list_active()
                if not active:
                    return
                # ET-window check is global — calendar doesn't vary by
                # project, just need *a* broker for the calendar query.
                in_window = await asyncio.to_thread(
                    _in_extended_hours_window,
                    get_broker(active[0]),
                )
                if not in_window:
                    logger.debug(
                        "optimizer tick skipped: outside ET 04:00-20:00 "
                        "window or non-trading day"
                    )
                    return
                try:
                    global_min = int(AppSettings.get(
                        "optimizer_interval_minutes", 30) or 30)
                except Exception:
                    global_min = 30
                now = _runner_time.monotonic()
                from intelligence.optimizer_agent import run_for_project
                for proj in active:
                    try:
                        per_proj = int(ProjectSettings.get(
                            proj.project_id,
                            "optimizer_interval_minutes",
                            default=0) or 0)
                    except Exception:
                        per_proj = 0
                    interval_min = (per_proj if per_proj > 0
                                    else global_min)
                    if interval_min <= 0:
                        continue
                    last = self._last_opt_run.get(
                        proj.project_id, 0.0)
                    if (now - last) < interval_min * 60:
                        continue
                    try:
                        await asyncio.to_thread(run_for_project,
                                                proj.project_id)
                        self._last_opt_run[proj.project_id] = now
                    except Exception as ex:
                        logger.exception(
                            "optimizer failed for %s: %s",
                            proj.project_id, ex)
            except Exception as ex:
                logger.exception("optimizer tick error: %s", ex)
        sched.add_job(_optimizer_tick, "interval", minutes=1,
                      id="optimizer_agent", replace_existing=True)

        # ---- Broker fee sync (every 15 min) ----------------------
        # closed_contracts.brokerage_fee starts NULL when the closure
        # is recorded. This job polls each active project's broker
        # (/v2/account/activities on Alpaca, /v1/.../transactions on
        # ETrade), matches each fee row to a pending closure, and
        # stamps the column. The P&L report's Brokerage Fee + Net P&L
        # columns read from there.
        async def _fees_sync_tick():
            try:
                active = ProjectsRepo.list_active()
            except Exception:
                active = []
            if not active:
                return
            from fees import sync_fees_for_project
            for proj in active:
                try:
                    await asyncio.to_thread(
                        sync_fees_for_project, proj.project_id)
                except Exception as ex:
                    logger.exception(
                        "fees sync failed for %s: %s",
                        proj.project_id, ex)
        sched.add_job(_fees_sync_tick, "interval", minutes=15,
                      id="fees_sync", replace_existing=True)

        # ---- Dynamic watchlist refresh (daily at market open) ------
        # Replaces the static tier-baseline watchlists with a market-
        # aware refresh: stable tier anchors + today's top % movers
        # + IV-rich names, all BP-fit and earnings-filtered. Fires
        # at 09:35 ET (5 min after open so today's snapshots have
        # tradeable data). Each project can opt out via
        # dynamic_watchlist_enabled.
        async def _watchlist_refresh_tick():
            try:
                active = ProjectsRepo.list_active()
            except Exception:
                active = []
            if not active:
                return
            from watchlists import refresh_watchlist
            for proj in active:
                try:
                    res = await asyncio.to_thread(
                        refresh_watchlist, proj.project_id)
                    if res.get("status") == "refreshed":
                        logger.info(
                            "dynamic watchlist refreshed %s: %d names",
                            proj.project_id, res.get("count"))
                except Exception as ex:
                    logger.exception(
                        "watchlist refresh failed for %s: %s",
                        proj.project_id, ex)
        sched.add_job(
            _watchlist_refresh_tick, "cron",
            hour=13, minute=35, timezone=timezone.utc,
            id="dynamic_watchlist", replace_existing=True)

        # ---- News RSS aggregator refresh (every 30 min) -----------
        # Pulls free-tier RSS feeds (MarketWatch, CNBC, Yahoo,
        # Seeking Alpha, Reddit WSB) and caches per-ticker mention
        # counts so the dynamic watchlist's news-aware scoring layer
        # is O(1) per lookup. The fetcher tolerates failure per feed
        # so a single dead URL doesn't break aggregation.
        async def _news_refresh_tick():
            try:
                from news import refresh_news_cache
                # Use the dynamic watchlist's curated pool as the
                # valid-ticker filter so bare-word matches don't
                # surface false positives (GDP, USA, etc.).
                from watchlists.dynamic import _LIQUID_OPTIONABLE_POOL
                valid = {s.strip().upper()
                         for s in _LIQUID_OPTIONABLE_POOL.split(",")
                         if s.strip()}
                res = await asyncio.to_thread(
                    refresh_news_cache, valid, force=True)
                if res.get("status") == "refreshed":
                    logger.info(
                        "news cache refreshed: %d tickers found, "
                        "per-source counts %s",
                        res.get("tickers_found"),
                        res.get("per_source"))
            except Exception as ex:
                logger.exception("news refresh failed: %s", ex)
        sched.add_job(_news_refresh_tick, "interval", minutes=30,
                      id="news_refresh", replace_existing=True)

        # ---- Deep position reconciliation (twice daily) -----------
        # The 15-min light pass above only detects PRESENCE mismatches
        # (DB has it / broker doesn't, or vice versa). It does NOT catch
        # qty drift or long-vs-short flips, which is how NIO went from
        # short-1 to long-12 today without anyone noticing.
        # This deeper job catches those AND auto-fixes when the project
        # has reconcile_auto_sync enabled. Runs at 10:00 ET and 15:30 ET
        # (post-open and pre-close) so it sees stable broker state.
        async def _deep_recon_tick():
            from db.repositories import ProjectsRepo as _PR
            try:
                for proj in _PR.list_active():
                    try:
                        from ops.reconciliation import run_reconciliation
                        await asyncio.to_thread(
                            run_reconciliation, proj.project_id,
                            deep_sync=True,
                        )
                    except Exception as ex:
                        logger.exception(
                            "deep reconcile failed for %s: %s",
                            proj.project_id, ex,
                        )
            except Exception as ex:
                logger.exception("deep recon tick error: %s", ex)
        # 10:00 ET = 14:00 UTC (EDT) / 15:00 UTC (EST). Use UTC and let
        # APScheduler treat it as cron. Both 14:00 and 19:30 UTC fall
        # safely inside RTH for both EDT and EST.
        sched.add_job(_deep_recon_tick, "cron",
                      hour=14, minute=0,
                      id="deep_reconciliation_am",
                      replace_existing=True)
        sched.add_job(_deep_recon_tick, "cron",
                      hour=19, minute=30,
                      id="deep_reconciliation_pm",
                      replace_existing=True)

        # ---- Orders status polling ----------------------------------
        async def _orders_tick():
            from db.repositories import ProjectsRepo as _PR
            try:
                for proj in _PR.list_active():
                    try:
                        from ops.orders_tracker import poll_orders
                        await asyncio.to_thread(poll_orders, proj.project_id)
                    except Exception as ex:
                        logger.exception("order poll failed %s: %s",
                                         proj.project_id, ex)
            except Exception as ex:
                logger.exception("orders tick error: %s", ex)
        try:
            order_sec = int(AppSettings.get("order_poll_interval_sec", 30) or 30)
        except Exception:
            order_sec = 30
        if order_sec > 0:
            sched.add_job(_orders_tick, "interval", seconds=order_sec,
                          id="orders_poll", replace_existing=True)

        # ---- Anomaly detection (every 15 min) -----------------------
        async def _anomaly_tick():
            from db.repositories import ProjectsRepo as _PR
            from intelligence.anomalies import detect_anomalies
            try:
                for proj in _PR.list_active():
                    try:
                        await asyncio.to_thread(detect_anomalies, proj.project_id)
                    except Exception:
                        logger.exception("anomaly detect failed %s",
                                         proj.project_id)
            except Exception:
                logger.exception("anomaly tick error")
        sched.add_job(_anomaly_tick, "interval", minutes=15,
                      id="anomaly_detect", replace_existing=True)

        # ---- AI recommendations (weekly Monday 14 UTC) --------------
        async def _recs_tick():
            from db.repositories import ProjectsRepo as _PR
            from intelligence.recommendations import build_recommendations
            try:
                for proj in _PR.list_active():
                    try:
                        await asyncio.to_thread(build_recommendations,
                                                proj.project_id)
                    except Exception:
                        logger.exception("recs build failed %s",
                                         proj.project_id)
            except Exception:
                logger.exception("recs tick error")
        sched.add_job(_recs_tick, "cron", day_of_week="mon", hour=14,
                      minute=0, id="ai_recommendations",
                      replace_existing=True)

        # ---- DCA execution (hourly check) --------------------------
        # The DCA module decides per-schedule whether NOW is the right
        # moment for a buy based on next_execution_date; checking hourly
        # keeps it cheap and catches the right moment within ~60 min.
        async def _dca_tick():
            from db.repositories import ProjectsRepo as _PR
            from db.settings_store import ProjectSettings as _PS
            try:
                from strategies.dca import execute_due_schedules
            except Exception:
                logger.exception("DCA import failed")
                return
            try:
                for proj in _PR.list_active():
                    # Only run DCA for projects whose strategy_mode
                    # explicitly includes it.
                    mode = str(_PS.get(proj.project_id, "strategy_mode",
                                       default="wheel") or "wheel").lower()
                    if mode not in ("wheel_plus_dca", "dca_only"):
                        continue
                    try:
                        await asyncio.to_thread(execute_due_schedules,
                                                proj.project_id)
                    except Exception:
                        logger.exception("DCA exec failed %s",
                                         proj.project_id)
            except Exception:
                logger.exception("DCA tick error")
        sched.add_job(_dca_tick, "interval", minutes=60,
                      id="dca_execute", replace_existing=True)

        # ---- Portfolio rebalancer (daily at 13:30 UTC = market open) ----
        async def _rebalance_tick():
            from db.repositories import ProjectsRepo as _PR
            try:
                from strategies.rebalancer import execute_rebalance
            except Exception:
                logger.exception("rebalancer import failed")
                return
            try:
                for proj in _PR.list_active():
                    try:
                        await asyncio.to_thread(execute_rebalance,
                                                proj.project_id)
                    except Exception:
                        logger.exception("rebalance failed %s",
                                         proj.project_id)
            except Exception:
                logger.exception("rebalance tick error")
        sched.add_job(_rebalance_tick, "cron", hour=13, minute=30,
                      id="rebalancer", replace_existing=True)

        sched.start()
        self._scheduler = sched
        # NB: recon_min was removed in the per-project-interval refactor —
        # the recon tick now runs every minute and gates per-project, so
        # there's no single 'recon=Nm' value to log. Same for the
        # optimizer.
        logger.info("schedulers running: digest@%02dUTC backup@%02dUTC "
                    "recon=1m(gated) orders=%ds anomalies=15m recs=weekly "
                    "dca=hourly rebalance@13:30UTC optimizer=1m(gated)",
                    hour, backup_hour, order_sec)

    async def _reconcile(self) -> None:
        try:
            all_active = ProjectsRepo.list_active()
        except Exception as e:
            logger.exception("reconcile failed: %s", e)
            return

        # Phase 2: both Alpaca and ETrade projects route through the
        # polymorphic get_broker() factory now, so the runner no longer
        # discriminates by broker_type. ETrade projects with completed
        # OAuth flow through the same TenantWorker.
        active_pids: set[str] = {p.project_id for p in all_active}

        max_concurrent = int(AppSettings.get("max_concurrent_tenants", 8))

        # Stop workers whose project is no longer active.
        for pid in list(self._workers.keys()):
            if pid not in active_pids:
                self._workers[pid].stop()
                task = self._tasks.pop(pid, None)
                if task:
                    task.cancel()
                self._workers.pop(pid, None)

        # --- WATCHDOG --------------------------------------------------
        # Two failure modes we've seen in prod kill cycles silently:
        #   1. The worker's asyncio task finishes (.done() is True)
        #      because of an unhandled exception that escaped both
        #      _tick() and run_forever() catches. The task object stays
        #      in self._tasks but ticks have stopped firing.
        #   2. The task is still "running" but hasn't logged a
        #      Worker.LOOP event in a long time — could be a deadlock,
        #      a blocked Alpaca HTTP call holding the loop, or a
        #      perpetual sleep on a bogus next_open value.
        # Both look like "no trades for 12 hours" from the user side.
        # The watchdog tears down stuck workers and forces a fresh
        # task to be created in the next loop of this method.
        from datetime import datetime, timedelta, timezone
        from db.repositories import EventsRepo
        now_utc = datetime.now(tz=timezone.utc)
        # Stale threshold: 15 min during market hours, 35 min off-hours
        # (covers the 5-min market-closed-sleep clamp plus margin).
        # We use a conservative single threshold here so we don't depend
        # on the broker clock at watchdog time.
        STALE_MINS = 20
        for pid in list(self._workers.keys()):
            reason = None
            task = self._tasks.get(pid)
            if task is not None and task.done():
                reason = "task_dead"
                # Surface the exception if there was one — helpful for
                # post-mortem even when the worker self-restarts.
                exc = None
                try:
                    exc = task.exception()
                except Exception:
                    pass
                if exc is not None:
                    logger.error(
                        "watchdog: worker task for %s died with: %r",
                        pid, exc,
                    )
            else:
                try:
                    events = EventsRepo.recent(pid, limit=12)
                except Exception:
                    events = []
                latest_loop = next(
                    (e for e in events
                     if e["node_name"] == "Worker"
                     and e["event_type"] == "LOOP"),
                    None,
                )
                if latest_loop is not None:
                    ts = latest_loop["created_at"]
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                    if now_utc - ts > timedelta(minutes=STALE_MINS):
                        reason = "stale_heartbeat"
            if reason is None:
                continue
            logger.warning(
                "watchdog: restarting tenant worker %s (reason=%s)",
                pid, reason,
            )
            try:
                EventsRepo.log(pid, "Watchdog", "WORKER_RESTART", {
                    "reason": reason,
                    "narrative": [
                        f"Runner watchdog restarted this project's "
                        f"worker (reason: {reason}). Cycles should "
                        f"resume on the next scheduler tick.",
                    ],
                })
            except Exception:
                pass
            try:
                self._workers[pid].stop()
            except Exception:
                pass
            task = self._tasks.pop(pid, None)
            if task and not task.done():
                task.cancel()
            self._workers.pop(pid, None)

        # Start workers for new active projects (any broker), respecting
        # concurrency. Also catches workers the watchdog just tore down.
        for pid in active_pids:
            if pid in self._workers:
                continue
            if len(self._workers) >= max_concurrent:
                break
            worker = TenantWorker(pid)
            self._workers[pid] = worker
            self._tasks[pid] = asyncio.create_task(worker.run_forever())
