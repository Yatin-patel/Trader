"""Cross-tenant portfolio aggregation (Cat 12.2)."""
from __future__ import annotations

from typing import Any

from analytics.pnl_calculator import metrics_summary
from db.analytics_repos import PortfolioSnapshotsRepo
from db.repositories import ProjectsRepo


def aggregate_all() -> dict[str, Any]:
    projects = ProjectsRepo.list_all()
    rows: list[dict[str, Any]] = []
    total_equity = 0.0
    total_realized_month = 0.0
    total_unrealized = 0.0
    total_trades_month = 0
    for p in projects:
        try:
            m = metrics_summary(p.project_id, period="month")
            snap = PortfolioSnapshotsRepo.latest(p.project_id)
        except Exception:
            m = {}; snap = None
        equity = (snap or {}).get("equity") or 0.0
        unrealized = (snap or {}).get("unrealized_pnl") or 0.0
        realized = m.get("realized_pnl") or 0.0
        trades = m.get("trade_count") or 0
        rows.append({
            "project_id": p.project_id,
            "project_name": p.project_name,
            "is_active": p.is_active,
            "equity": equity,
            "unrealized_pnl": unrealized,
            "realized_pnl_month": realized,
            "trade_count_month": trades,
            "win_rate": m.get("win_rate"),
        })
        total_equity += equity
        total_realized_month += realized
        total_unrealized += unrealized
        total_trades_month += trades
    return {
        "projects": rows,
        "totals": {
            "equity": total_equity,
            "unrealized_pnl": total_unrealized,
            "realized_pnl_month": total_realized_month,
            "trade_count_month": total_trades_month,
            "project_count": len(rows),
            "active_count": sum(1 for r in rows if r["is_active"]),
        },
    }
