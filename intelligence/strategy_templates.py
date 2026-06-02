"""Strategy templates / wizard (Cat 9.3).

Curated presets. Each template = a dict of project_settings to apply.
Users see them in the New-Project dialog and can apply with one click.
"""
from __future__ import annotations

from typing import Any

TEMPLATES: dict[str, dict[str, Any]] = {
    "conservative_wheel": {
        "name": "Conservative Wheel",
        "description": (
            "Disciplined wheel for slow, steady income. Wide delta band, "
            "moderate DTE, automatic profit-taking, hard stop loss."
        ),
        "settings": {
            "csp_delta_min": 0.15,
            "csp_delta_max": 0.25,
            "csp_min_dte": 21,
            "csp_max_dte": 45,
            "cc_delta_min": 0.20,
            "cc_delta_max": 0.30,
            "max_collateral_pct": 0.50,
            "contracts_per_csp": 1,
            "max_open_contracts": 3,
            "max_open_positions": 2,
            "stop_loss_dollars": 5.0,
            "scanner_min_pct_change": 0.5,
            "volume_threshold": 5_000_000,
            "take_profit_enabled": True,
            "close_at_profit_pct": 0.50,
            "auto_roll_enabled": True,
            "auto_roll_dte_threshold": 3,
            "max_concentration_per_ticker": 0.20,
            "avoid_earnings_within_dte": 7,
            "min_iv_rank": 0.30,
            "dry_run": True,
        },
    },
    "aggressive_income": {
        "name": "Aggressive Income",
        "description": (
            "Higher-yield deltas and shorter DTE for max premium. Larger "
            "concentration, looser stops. Higher drawdown risk. Cycles "
            "every 2 minutes; trades live (no dry-run)."
        ),
        "settings": {
            "csp_delta_min": 0.30,
            "csp_delta_max": 0.45,
            "csp_min_dte": 0,
            "csp_max_dte": 14,
            "cc_delta_min": 0.40,
            "cc_delta_max": 0.55,
            "max_collateral_pct": 0.90,
            "contracts_per_csp": 3,
            "max_open_contracts": 15,
            "max_open_positions": 10,
            "stop_loss_dollars": 8.0,
            "scanner_min_pct_change": 1.5,
            "volume_threshold": 3_000_000,
            "take_profit_enabled": True,
            "close_at_profit_pct": 0.40,
            "auto_roll_enabled": True,
            "auto_roll_dte_threshold": 1,
            "max_concentration_per_ticker": 0.40,
            "avoid_earnings_within_dte": 2,
            "min_iv_rank": 0.0,
            "loop_interval_seconds": 120,
            "dry_run": False,
        },
    },
    "iv_disciplined": {
        "name": "IV-Disciplined",
        "description": (
            "Only sells premium when realized vol is in the upper half of "
            "its 1-year range. Quality over quantity."
        ),
        "settings": {
            "csp_delta_min": 0.20,
            "csp_delta_max": 0.30,
            "csp_min_dte": 14,
            "csp_max_dte": 30,
            "cc_delta_min": 0.25,
            "cc_delta_max": 0.35,
            "max_collateral_pct": 0.70,
            "contracts_per_csp": 1,
            "max_open_contracts": 5,
            "stop_loss_dollars": 6.0,
            "scanner_min_pct_change": 1.0,
            "min_iv_rank": 0.50,
            "avoid_earnings_within_dte": 7,
            "news_sentiment_filter": True,
            "news_sentiment_min": -0.20,
            "take_profit_enabled": True,
            "close_at_profit_pct": 0.50,
            "dry_run": True,
        },
    },
}


def list_templates() -> list[dict[str, Any]]:
    return [{"id": tid, "name": t["name"], "description": t["description"],
             "settings": t["settings"]}
            for tid, t in TEMPLATES.items()]


def get_template(tid: str) -> dict[str, Any] | None:
    t = TEMPLATES.get(tid)
    if t is None:
        return None
    return {"id": tid, "name": t["name"], "description": t["description"],
            "settings": t["settings"]}


def apply_template(project_id: str, tid: str) -> dict[str, Any]:
    from db.settings_store import ProjectSettings
    t = TEMPLATES.get(tid)
    if t is None:
        return {"error": "unknown template"}
    applied = {}
    for k, v in t["settings"].items():
        try:
            ProjectSettings.set(project_id, k, v)
            applied[k] = v
        except Exception:
            pass
    return {"applied": applied, "template": t["name"]}
