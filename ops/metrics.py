"""Lightweight health metrics + Prometheus text exposition.

We don't pull in the prometheus_client library — we synthesize the metrics
on demand from DB counts so the endpoint stays trivial.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text

from db.connection import session_scope
from db.repositories import ProjectsRepo


def collect_metrics() -> dict[str, Any]:
    now = datetime.now(tz=timezone.utc)
    cutoff_5m = now - timedelta(minutes=5)
    cutoff_60m = now - timedelta(minutes=60)
    cutoff_day = now - timedelta(days=1)

    out: dict[str, Any] = {
        "as_of": now.isoformat(),
        "projects": {"active": 0, "total": 0},
        "events": {"last_5m": 0, "last_60m": 0, "errors_last_60m": 0},
        "orders": {"open": 0, "filled_today": 0, "rejected_today": 0},
        "contracts": {"open": 0},
        "positions": {"open": 0},
        "notifications": {"queued": 0, "sent_today": 0, "failed_today": 0},
        "kill_switches": {"configured": 0, "breached_today": 0},
        "backups": {"completed_today": 0, "last_status": None},
    }

    projects = ProjectsRepo.list_all()
    out["projects"]["total"] = len(projects)
    out["projects"]["active"] = sum(1 for p in projects if p.is_active)

    with session_scope() as s:
        out["events"]["last_5m"] = int(s.execute(text(
            "SELECT COUNT(*) FROM dbo.agent_events WHERE created_at >= :c"
        ), {"c": cutoff_5m}).fetchone()[0] or 0)
        out["events"]["last_60m"] = int(s.execute(text(
            "SELECT COUNT(*) FROM dbo.agent_events WHERE created_at >= :c"
        ), {"c": cutoff_60m}).fetchone()[0] or 0)
        out["events"]["errors_last_60m"] = int(s.execute(text(
            "SELECT COUNT(*) FROM dbo.agent_events "
            "WHERE event_type = 'ERROR' AND created_at >= :c"
        ), {"c": cutoff_60m}).fetchone()[0] or 0)

        out["orders"]["open"] = int(s.execute(text(
            "SELECT COUNT(*) FROM dbo.orders WHERE terminal = 0"
        )).fetchone()[0] or 0)
        out["orders"]["filled_today"] = int(s.execute(text(
            "SELECT COUNT(*) FROM dbo.orders "
            "WHERE status = 'filled' AND submitted_at >= :c"
        ), {"c": cutoff_day}).fetchone()[0] or 0)
        out["orders"]["rejected_today"] = int(s.execute(text(
            "SELECT COUNT(*) FROM dbo.orders "
            "WHERE status IN ('rejected','canceled','cancelled') "
            "  AND submitted_at >= :c"
        ), {"c": cutoff_day}).fetchone()[0] or 0)

        out["contracts"]["open"] = int(s.execute(text(
            "SELECT COUNT(*) FROM dbo.wheel_contracts WHERE is_closed = 0"
        )).fetchone()[0] or 0)
        out["positions"]["open"] = int(s.execute(text(
            "SELECT COUNT(*) FROM dbo.stock_positions WHERE status = 'OPEN'"
        )).fetchone()[0] or 0)

        out["notifications"]["queued"] = int(s.execute(text(
            "SELECT COUNT(*) FROM dbo.notifications WHERE status = 'queued'"
        )).fetchone()[0] or 0)
        out["notifications"]["sent_today"] = int(s.execute(text(
            "SELECT COUNT(*) FROM dbo.notifications "
            "WHERE status = 'sent' AND created_at >= :c"
        ), {"c": cutoff_day}).fetchone()[0] or 0)
        out["notifications"]["failed_today"] = int(s.execute(text(
            "SELECT COUNT(*) FROM dbo.notifications "
            "WHERE status = 'failed' AND created_at >= :c"
        ), {"c": cutoff_day}).fetchone()[0] or 0)

        out["kill_switches"]["configured"] = int(s.execute(text(
            "SELECT COUNT(*) FROM dbo.risk_limits WHERE enabled = 1"
        )).fetchone()[0] or 0)
        out["kill_switches"]["breached_today"] = int(s.execute(text(
            "SELECT COUNT(*) FROM dbo.risk_limits "
            "WHERE last_breached_at >= :c"
        ), {"c": cutoff_day}).fetchone()[0] or 0)

        last_backup = s.execute(text("""
            SELECT TOP 1 status FROM dbo.backup_log ORDER BY backup_id DESC
        """)).fetchone()
        if last_backup:
            out["backups"]["last_status"] = last_backup[0]
        out["backups"]["completed_today"] = int(s.execute(text(
            "SELECT COUNT(*) FROM dbo.backup_log "
            "WHERE status = 'COMPLETE' AND started_at >= :c"
        ), {"c": cutoff_day}).fetchone()[0] or 0)
    return out


def prometheus_text() -> str:
    m = collect_metrics()
    lines: list[str] = []

    def add(name: str, value: int | float, help_text: str = "") -> None:
        if help_text:
            lines.append(f"# HELP {name} {help_text}")
            lines.append(f"# TYPE {name} gauge")
        lines.append(f"{name} {value}")

    add("trader_projects_active", m["projects"]["active"], "Active projects")
    add("trader_projects_total", m["projects"]["total"], "Total projects")
    add("trader_events_5m", m["events"]["last_5m"], "Events in last 5m")
    add("trader_events_60m", m["events"]["last_60m"], "Events in last 60m")
    add("trader_errors_60m", m["events"]["errors_last_60m"], "Errors in last 60m")
    add("trader_orders_open", m["orders"]["open"], "Open orders")
    add("trader_orders_filled_today", m["orders"]["filled_today"])
    add("trader_orders_rejected_today", m["orders"]["rejected_today"])
    add("trader_contracts_open", m["contracts"]["open"])
    add("trader_positions_open", m["positions"]["open"])
    add("trader_notifications_queued", m["notifications"]["queued"])
    add("trader_notifications_sent_today", m["notifications"]["sent_today"])
    add("trader_notifications_failed_today", m["notifications"]["failed_today"])
    add("trader_kill_switches_configured", m["kill_switches"]["configured"])
    add("trader_kill_switches_breached_today", m["kill_switches"]["breached_today"])
    add("trader_backups_completed_today", m["backups"]["completed_today"])
    return "\n".join(lines) + "\n"
