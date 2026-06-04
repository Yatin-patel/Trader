"""Multi-tenant runner.

Spawns one async task per active project and monitors `trading_projects` for
new tenants on a configurable refresh interval.
"""
from __future__ import annotations

import asyncio
import logging

from db.repositories import ProjectsRepo
from db.settings_store import AppSettings

from .tenant_worker import TenantWorker

logger = logging.getLogger(__name__)


class MultiTenantRunner:
    def __init__(self) -> None:
        self._workers: dict[str, TenantWorker] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._stop_event = asyncio.Event()
        # Tracks which non-Alpaca projects we've already warned about so we
        # don't spam the log every 15s reconcile.
        self._etrade_warned: dict[str, bool] = {}

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
        async def _recon_tick():
            from db.repositories import ProjectsRepo as _PR
            try:
                for proj in _PR.list_active():
                    try:
                        from ops.reconciliation import run_reconciliation
                        await asyncio.to_thread(run_reconciliation,
                                                proj.project_id)
                    except Exception as ex:
                        logger.exception("reconcile failed for %s: %s",
                                         proj.project_id, ex)
            except Exception as ex:
                logger.exception("recon tick error: %s", ex)
        try:
            recon_min = int(AppSettings.get("reconcile_interval_min", 15) or 15)
        except Exception:
            recon_min = 15
        if recon_min > 0:
            sched.add_job(_recon_tick, "interval", minutes=recon_min,
                          id="reconciliation", replace_existing=True)

        # ---- Continuous Optimizer Agent (every N minutes) ---------
        # Runs intelligence/recommendations against each active project
        # on a fixed cadence. When the project has
        # ``optimizer_auto_apply=True``, safe changes get auto-applied;
        # otherwise the recommendation stays pending for human review
        # in /intelligence. Interval is configurable globally via
        # AppSettings 'optimizer_interval_minutes' (default 30, 0 to
        # disable).
        async def _optimizer_tick():
            try:
                from intelligence.optimizer_agent import run_all_active
                await asyncio.to_thread(run_all_active)
            except Exception as ex:
                logger.exception("optimizer tick error: %s", ex)
        try:
            opt_min = int(AppSettings.get(
                "optimizer_interval_minutes", 30) or 30)
        except Exception:
            opt_min = 30
        if opt_min > 0:
            sched.add_job(_optimizer_tick, "interval", minutes=opt_min,
                          id="optimizer_agent", replace_existing=True)

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
        logger.info("schedulers running: digest@%02dUTC backup@%02dUTC "
                    "recon=%dm orders=%ds anomalies=15m recs=weekly "
                    "dca=hourly rebalance@13:30UTC",
                    hour, backup_hour, recon_min, order_sec)

    async def _reconcile(self) -> None:
        try:
            all_active = ProjectsRepo.list_active()
        except Exception as e:
            logger.exception("reconcile failed: %s", e)
            return

        # Phase-1 broker support: agents are still Alpaca-only. ETrade
        # projects survive in the DB but skip the cycle loop until the
        # full ETrade adapter ships in Phase 2.
        alpaca_active: set[str] = set()
        for p in all_active:
            bt = (getattr(p, "broker_type", "alpaca") or "alpaca")
            if bt == "alpaca":
                alpaca_active.add(p.project_id)
            else:
                if not self._etrade_warned.get(p.project_id):
                    logger.info(
                        "skipping %s — broker_type=%s not yet supported by "
                        "the runner (OAuth tokens present, agents pending)",
                        p.project_id, bt,
                    )
                    self._etrade_warned[p.project_id] = True

        max_concurrent = int(AppSettings.get("max_concurrent_tenants", 8))

        # Stop workers that are no longer active (or switched to ETrade).
        for pid in list(self._workers.keys()):
            if pid not in alpaca_active:
                self._workers[pid].stop()
                task = self._tasks.pop(pid, None)
                if task:
                    task.cancel()
                self._workers.pop(pid, None)

        # Start workers for new active Alpaca projects, respecting concurrency.
        for pid in alpaca_active:
            if pid in self._workers:
                continue
            if len(self._workers) >= max_concurrent:
                break
            worker = TenantWorker(pid)
            self._workers[pid] = worker
            self._tasks[pid] = asyncio.create_task(worker.run_forever())
