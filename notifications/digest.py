"""Daily-digest builder + sender."""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any

from db.repositories import ProjectsRepo, WheelRepo
from db.settings_store import AppSettings

from .dispatcher import dispatch

logger = logging.getLogger(__name__)


def build_daily_digest(project_id: str) -> dict[str, str]:
    """Return {title, body} for the previous trading day's summary."""
    project = ProjectsRepo.get(project_id)
    if project is None:
        return {"title": "Trader digest", "body": "(no project)"}

    today = date.today()
    yesterday = today - timedelta(days=1)

    # Pull yesterday's closed trades + current open contracts + recent realized.
    from analytics.pnl_calculator import metrics_summary
    summary_today = metrics_summary(project_id, period="today")
    summary_week = metrics_summary(project_id, period="week")
    summary_month = metrics_summary(project_id, period="month")

    open_contracts = WheelRepo.list_open(project_id)

    lines = [
        f"Daily digest for {project.project_name} ({project_id})",
        f"As of {datetime.now(tz=timezone.utc).isoformat()}",
        "",
        "── P&L ──",
        f"Today:      realized ${summary_today['realized_pnl']:,.2f}, "
        f"{summary_today['trade_count']} closed",
        f"This week:  realized ${summary_week['realized_pnl']:,.2f}, "
        f"{summary_week['trade_count']} closed",
        f"This month: realized ${summary_month['realized_pnl']:,.2f}, "
        f"{summary_month['trade_count']} closed",
        f"Total premium captured (month): "
        f"${summary_month['total_premium_captured']:,.2f}",
        f"Current equity: ${summary_month.get('current_equity') or 0:,.2f}",
        f"Unrealized P&L: ${summary_month.get('unrealized_pnl') or 0:,.2f}",
        "",
        "── Open Positions ──",
    ]
    if not open_contracts:
        lines.append("(none)")
    else:
        for c in open_contracts[:10]:
            exp = c.get("expiration_date")
            dte = (exp - today).days if exp else "?"
            lines.append(
                f"  {c['ticker']:<6} {c['strategy_phase']:<20} "
                f"strike ${c['strike_price']:.2f}  exp {exp} (DTE {dte})  "
                f"premium ${c['premium_collected']:.2f}"
            )
        if len(open_contracts) > 10:
            lines.append(f"  …and {len(open_contracts) - 10} more")

    return {
        "title": f"Trader digest · {project.project_name} · {yesterday.isoformat()}",
        "body": "\n".join(lines),
    }


def send_daily_digest(project_id: str) -> list[dict[str, Any]]:
    digest = build_daily_digest(project_id)
    return dispatch(project_id, digest["title"], digest["body"],
                    severity="info", event_type="DIGEST", payload=None)
