"""Auto-tune project settings to the account's cash + chosen strategy.

Powers the "Optimize" button. Given the user's available cash on Alpaca
plus the selected strategy template, picks per-tenant overrides so the
strategy is actually executable. Without this, applying "Aggressive" to
a $1k paper account leaves the bot picking trades that the guardrail
will silently reject (collateral cap, concentration limit).

The tier table below is empirical, not arbitrary — it's the same
overrides we ended up applying by hand when debugging Sheel-Test1.
"""
from __future__ import annotations

import logging
from typing import Any

from db.repositories import ProjectsRepo
from db.settings_store import ProjectSettings
from execution import BrokerNotConfigured, get_broker
from intelligence.strategy_templates import TEMPLATES

logger = logging.getLogger(__name__)

# Watchlists tuned to fit different account sizes. For tiny accounts we
# need strike-under-$10 names so a single CSP fits the collateral budget.
_CHEAP_WATCHLIST = "NIO,AMC,SOFI,F,RIVN,PLTR,SNAP,T,BAC,WBD,GME,SIRI"
_MID_WATCHLIST = (
    "AAPL,MSFT,NVDA,AMD,META,TSLA,F,SOFI,NIO,RIVN,PLTR,COIN,GOOGL,"
    "AMZN,NFLX,SHOP,SQ,HOOD,SNAP,U,DDOG,SNOW,CRM,ORCL,ADBE"
)


def _cash_tier(cash: float) -> str:
    if cash < 5_000:
        return "tiny"
    if cash < 25_000:
        return "small"
    if cash < 100_000:
        return "medium"
    return "large"


def _tier_overrides(tier: str) -> dict[str, Any]:
    """Cash-tier-specific overrides applied on top of the strategy template."""
    if tier == "tiny":
        return {
            "max_concentration_per_ticker": 1.0,
            "max_collateral_pct": 1.0,
            "contracts_per_csp": 1,
            "watchlist": _CHEAP_WATCHLIST,
            "scanner_min_price": 1.0,
            "scanner_max_price": 30.0,
            "csp_min_dte": 7,
            "csp_max_dte": 35,
        }
    if tier == "small":
        return {
            "max_concentration_per_ticker": 0.50,
            "max_collateral_pct": 0.95,
            "contracts_per_csp": 1,
            "watchlist": _MID_WATCHLIST,
            "scanner_max_price": 100.0,
        }
    if tier == "medium":
        return {
            "max_concentration_per_ticker": 0.30,
            "max_collateral_pct": 0.85,
            "contracts_per_csp": 2,
        }
    # large
    return {
        "max_concentration_per_ticker": 0.25,
        "max_collateral_pct": 0.80,
        "contracts_per_csp": 3,
    }


# ---------------------------------------------------------------------------
# Mode-specific seed defaults — used when the user clicks Optimize on a
# project running spreads / day-trading. These don't override the cash-
# tier risk caps above (those still apply); they fill in the spread /
# intraday knobs the wheel templates don't carry, so the user doesn't
# have to hand-tune spread_target_delta / spread_width / etc. on first
# run.
# ---------------------------------------------------------------------------
def _mode_overrides(strategy_mode: str, tier: str) -> dict[str, Any]:
    mode = (strategy_mode or "wheel").lower()
    if mode in ("wheel", "wheel_plus_dca", "dca_only", "paused", ""):
        return {}

    # Tier shapes spread width — tiny/small accounts can't afford a
    # $20 spread, large accounts get more credit from wider wings.
    width_by_tier = {
        "tiny": 1.0, "small": 2.5, "medium": 5.0, "large": 10.0,
    }
    width = width_by_tier.get(tier, 5.0)

    if mode in ("bull_put_spread", "bear_call_spread"):
        # CREDIT verticals: short ~0.25 delta, wing ``width`` away.
        return {
            "spread_target_delta": 0.25,
            "spread_width":        width,
            "spread_min_dte":      21,
            "spread_max_dte":      45,
        }
    if mode in ("bull_call_spread", "bear_put_spread"):
        # DEBIT verticals: closer-to-ATM long, OTM short.
        return {
            "spread_target_delta": 0.50,
            "spread_width":        width,
            "spread_min_dte":      21,
            "spread_max_dte":      45,
        }
    if mode == "iron_condor":
        # Same shape as a credit vertical for each wing; the strategy
        # builds both put + call sides automatically.
        return {
            "spread_target_delta": 0.20,
            "spread_width":        width,
            "spread_min_dte":      28,
            "spread_max_dte":      45,
        }
    if mode == "calendar_spread":
        return {
            "calendar_short_dte":   14,
            "calendar_long_dte":    45,
            "calendar_option_type": "call",
            "spread_width":         0.0,
        }
    if mode == "intraday_momentum":
        # Default to 1DTE only — 0DTE is high-variance, opt in explicitly.
        # Trades-per-cycle scales with tier (PDT-safe accounts can take
        # more swings).
        per_cycle_by_tier = {
            "tiny": 1, "small": 1, "medium": 3, "large": 5,
        }
        return {
            "allow_0dte":                    False,
            "allow_1dte":                    True,
            "intraday_scanner_enabled":      True,
            "intraday_max_trades_per_cycle": per_cycle_by_tier.get(
                tier, 3),
        }
    return {}


def preview(project_id: str, strategy_id: str) -> dict[str, Any]:
    """Return the settings that would be applied, without saving anything."""
    project = ProjectsRepo.get(project_id)
    if project is None:
        return {"error": "project not found"}
    tpl = TEMPLATES.get(strategy_id)
    if tpl is None:
        return {"error": f"unknown strategy '{strategy_id}'"}

    # Broker-aware preview. ETrade projects without OAuth tokens don't
    # have a way to query cash, so we fall back to project.max_equity_allocation
    # as the planning amount and surface a friendly hint.
    broker_type = (getattr(project, "broker_type", "alpaca") or "alpaca")
    note: str | None = None
    try:
        account = get_broker(project).get_account()
        cash = float(account.get("cash") or 0)
        bp = float(account.get("buying_power") or 0)
    except BrokerNotConfigured as e:
        cash = float(getattr(project, "max_equity_allocation", 0) or 0)
        bp = 0.0
        if broker_type == "etrade":
            note = ("ETrade isn't fully connected yet — using project "
                    "allocation (${:,.0f}) as the planning amount. "
                    "Complete the OAuth flow to read live cash."
                    .format(cash))
        else:
            note = str(e)
    except NotImplementedError:
        # ETrade tokens present but Phase-2 endpoints aren't wired yet.
        cash = float(getattr(project, "max_equity_allocation", 0) or 0)
        bp = 0.0
        note = ("ETrade trading endpoints land in Phase 2. Optimizing "
                "against your project allocation (${:,.0f}) for now."
                .format(cash))
    except Exception as e:
        broker_label = "ETrade" if broker_type == "etrade" else "Alpaca"
        return {"error": f"{broker_label} account fetch failed: {e}"}

    tier = _cash_tier(cash)

    # Compose layers (later wins):
    #   1. Strategy template baseline
    #   2. Cash-tier risk caps (concentration, collateral, contracts_per_csp)
    #   3. Mode-specific seed defaults (spread width / delta / DTE,
    #      intraday flags) — only fills in keys the wheel-template
    #      doesn't carry; tier risk caps remain authoritative.
    strategy_mode = str(ProjectSettings.get(
        project_id, "strategy_mode", default="wheel") or "wheel").lower()
    combined: dict[str, Any] = dict(tpl["settings"])
    combined.update(_tier_overrides(tier))
    combined.update(_mode_overrides(strategy_mode, tier))

    notes = _tier_notes(tier, cash, bp, combined)
    if note:
        notes = [note] + notes
    if strategy_mode not in ("wheel", "wheel_plus_dca", "dca_only",
                             "paused", ""):
        notes.append(
            f"strategy_mode='{strategy_mode}': also seeded mode-"
            f"specific defaults (spread/intraday knobs) so this mode "
            f"is executable without hand-tuning. Continuous Optimizer "
            f"will keep refining them from live P&L."
        )

    return {
        "strategy": tpl["name"],
        "strategy_id": strategy_id,
        "cash": cash,
        "buying_power": bp,
        "tier": tier,
        "strategy_mode": strategy_mode,
        "broker_type": broker_type,
        "broker_state": "needs_oauth" if (broker_type == "etrade"
                        and not getattr(project, "etrade_access_token", ""))
                        else "ready",
        "settings": combined,
        "notes": notes,
    }


def _tier_notes(tier: str, cash: float, bp: float,
                settings: dict[str, Any]) -> list[str]:
    out: list[str] = []
    out.append(f"Detected cash: ${cash:,.0f}, buying-power: ${bp:,.0f} → tier '{tier}'.")
    if tier == "tiny":
        out.append(
            "Tiny accounts ($<5k): concentration & collateral caps both raised "
            "to 100% so a single CSP fits. Watchlist switched to low-strike "
            "tickers so the collateral budget can actually be filled."
        )
    elif tier == "small":
        out.append(
            "Small accounts ($5k–$25k): keeps moderate concentration. "
            "Wider watchlist than Tiny but excludes ultra-expensive names."
        )
    elif tier == "medium":
        out.append(
            "Medium accounts ($25k–$100k): template defaults respected; "
            "contracts_per_csp lifted to 2 for income scaling."
        )
    else:
        out.append(
            "Large accounts (>$100k): tighter per-ticker concentration to "
            "force diversification, contracts_per_csp = 3."
        )
    out.append(
        f"Will set max_concentration={settings.get('max_concentration_per_ticker')}, "
        f"max_collateral={settings.get('max_collateral_pct')}, "
        f"contracts_per_csp={settings.get('contracts_per_csp')}."
    )
    return out


def optimize(project_id: str, strategy_id: str) -> dict[str, Any]:
    """Apply the previewed settings to the project."""
    plan = preview(project_id, strategy_id)
    if "error" in plan:
        return plan
    settings = plan["settings"]
    applied: dict[str, Any] = {}
    for k, v in settings.items():
        try:
            ProjectSettings.set(project_id, k, v)
            applied[k] = v
        except Exception as e:
            logger.exception("optimize: failed to set %s=%s: %s", k, v, e)
    return {
        "strategy": plan["strategy"],
        "tier": plan["tier"],
        "cash": plan["cash"],
        "applied": applied,
        "notes": plan["notes"],
    }
