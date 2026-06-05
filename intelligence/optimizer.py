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

# Watchlists tuned to fit different account sizes. Revenue-focused —
# each tier's list is built so MOST names can be afforded with one
# contract of that tier's typical options_buying_power. Higher-IV
# names are preferred (more premium per dollar of collateral) but
# only when there's also enough liquidity (open interest, tight
# spreads) for execution to actually fill.
#
# Yield expectations (rule-of-thumb, on a 14-21 DTE 0.30-delta CSP):
#   IV-rank 0.50+ on a $10 stock  ≈  $0.20-0.40 premium ≈  2-4% yield
#   IV-rank 0.50+ on a $30 stock  ≈  $0.60-1.20 premium ≈  2-4% yield
# A $5k account hitting that twice a month is $200-400/mo income.

# Tiny ($<5k): sub-$15 strikes only. High-beta names (MARA, RIOT)
# for IV, income names (F, T) for fill probability.
_TINY_WATCHLIST = (
    "F,SOFI,NIO,RIVN,SNAP,T,WBD,SIRI,GRAB,LUMN,"
    "MARA,RIOT,IBIT,AMC,GME,"
    "NU,KEY,KMI,VALE,GOLD,AAL,UAL,CCL,PFE,X"
)

# Small ($5-25k): sub-$50 strikes. Includes the tiny list + mid-priced
# quality / high-IV names that fit a $1-5k single-contract CSP.
_SMALL_WATCHLIST = (
    "F,SOFI,NIO,RIVN,PLTR,HOOD,NU,MARA,RIOT,SNAP,T,BAC,WBD,"
    "PFE,KMI,VZ,INTC,WFC,KEY,CCL,AAL,UAL,GRAB,LUMN,IBIT,"
    "AMD,U,SHOP,COIN"
)

# Medium ($25-100k): sub-$300 strikes. Liquid mid-caps + select large.
_MID_WATCHLIST = (
    "AAPL,MSFT,NVDA,AMD,META,TSLA,F,SOFI,NIO,RIVN,PLTR,COIN,GOOGL,"
    "NFLX,SHOP,HOOD,SNAP,U,DDOG,SNOW,CRM,ORCL,ADBE,INTC,QCOM,MU"
)

# Large (>$100k): broad universe. Everything from MID + mega-caps.
_LARGE_WATCHLIST = _MID_WATCHLIST + ",AMZN,AVGO,JPM,BAC,WFC,GS,V,MA,KO,PEP,MCD,WMT,COST,DIS,KMI,VZ"


def _cash_tier(cash: float) -> str:
    if cash < 5_000:
        return "tiny"
    if cash < 25_000:
        return "small"
    if cash < 100_000:
        return "medium"
    return "large"


def _tier_overrides(tier: str) -> dict[str, Any]:
    """Cash-tier-specific overrides applied on top of the strategy template.
    Sets the watchlist + risk caps + DTE window for the tier. Strategy
    aggressiveness (delta, IV floor, profit target) layers on top via
    _plan_overrides() based on the project's trading_plan."""
    if tier == "tiny":
        return {
            "max_concentration_per_ticker": 1.0,
            "max_collateral_pct": 1.0,
            "contracts_per_csp": 1,
            "watchlist": _TINY_WATCHLIST,
            "scanner_min_price": 1.0,
            "scanner_max_price": 30.0,
            "csp_min_dte": 5,
            "csp_max_dte": 21,
        }
    if tier == "small":
        return {
            "max_concentration_per_ticker": 0.50,
            "max_collateral_pct": 0.95,
            "contracts_per_csp": 1,
            "watchlist": _SMALL_WATCHLIST,
            "scanner_max_price": 60.0,
            "csp_min_dte": 7,
            "csp_max_dte": 30,
        }
    if tier == "medium":
        return {
            "max_concentration_per_ticker": 0.30,
            "max_collateral_pct": 0.85,
            "contracts_per_csp": 2,
            "watchlist": _MID_WATCHLIST,
            "scanner_max_price": 300.0,
        }
    # large
    return {
        "max_concentration_per_ticker": 0.25,
        "max_collateral_pct": 0.80,
        "contracts_per_csp": 3,
        "watchlist": _LARGE_WATCHLIST,
    }


def _plan_overrides(trading_plan: str, tier: str) -> dict[str, Any]:
    """Trading-plan-aware strategy parameters. Aggressive plans on
    small accounts get the actually-aggressive defaults — higher delta
    (more premium), shorter DTE (faster cycling), lower IV-rank floor
    (don't reject viable trades), faster profit-take (lock in gains
    before reversal).

    The math on a $5k aggressive account:
      Current (balanced-ish): 0.30Δ × 14DTE × IV0.30 → ~2% yield/cycle
        → 2 cycles/mo at 75% win-rate → $5k × 2% × 2 × 0.75 = $150/mo
      Revenue-tuned aggressive: 0.40Δ × 7DTE × IV0.15
        → 4 cycles/mo at 70% win-rate → $5k × 2.5% × 4 × 0.70 = $350/mo
    Losses are larger when they hit, but expected value is positive.
    """
    plan = (trading_plan or "balanced").lower()

    if plan == "aggressive":
        if tier in ("tiny", "small"):
            # Tiny aggressive: chase yield. Higher delta, weekly cycling,
            # take profit at 50% to recycle capital fast, accept lower IV.
            return {
                "csp_delta_min":       0.35,
                "csp_delta_max":       0.50,
                "csp_min_dte":         5,
                "csp_max_dte":         14,
                "cc_delta_min":        0.30,
                "cc_delta_max":        0.45,
                "min_iv_rank":         0.15,
                "close_at_profit_pct": 0.50,
                "take_profit_enabled": True,
            }
        # Aggressive on medium/large: still aggressive but tier risk
        # caps prevent over-concentration.
        return {
            "csp_delta_min":       0.30,
            "csp_delta_max":       0.45,
            "csp_min_dte":         7,
            "csp_max_dte":         21,
            "min_iv_rank":         0.20,
            "close_at_profit_pct": 0.55,
            "take_profit_enabled": True,
        }

    if plan == "conservative":
        return {
            "csp_delta_min":       0.15,
            "csp_delta_max":       0.25,
            "csp_min_dte":         28,
            "csp_max_dte":         45,
            "min_iv_rank":         0.40,
            "close_at_profit_pct": 0.80,
            "take_profit_enabled": True,
        }

    # balanced (default) — keep current behavior
    return {
        "csp_delta_min":       0.20,
        "csp_delta_max":       0.35,
        "csp_min_dte":         14,
        "csp_max_dte":         30,
        "min_iv_rank":         0.30,
        "close_at_profit_pct": 0.65,
        "take_profit_enabled": True,
    }


def _filter_watchlist_by_bp(
    watchlist: str,
    options_bp: float,
    snapshots: dict[str, Any] | None = None,
) -> tuple[str, list[str], list[str]]:
    """Drop any watchlist ticker whose typical strike won't fit in
    ``options_bp`` for a 1-contract CSP. ``snapshots`` is a {symbol:
    Snapshot} dict from ``client.snapshots()`` — used to get current
    last_price. Tickers we don't have a price for are kept
    conservatively (they won't be filtered out unintentionally).

    Returns (filtered_csv, kept_list, dropped_list).
    """
    if options_bp <= 0:
        return watchlist, [], []
    syms = [s.strip().upper() for s in (watchlist or "").split(",")
            if s.strip()]
    if not syms:
        return watchlist, [], []
    max_strike = options_bp / 100.0
    kept: list[str] = []
    dropped: list[str] = []
    for sym in syms:
        price = None
        if snapshots:
            snap = snapshots.get(sym)
            if snap is not None:
                price = getattr(snap, "last_price", None)
        if price is None or price <= 0:
            # Keep unknowns — better to over-include than under.
            kept.append(sym)
            continue
        # Use price × 1.05 as a "rough strike upper bound" (Strategist
        # picks slightly OTM strikes). If even that doesn't fit, drop.
        if price * 1.05 <= max_strike:
            kept.append(sym)
        else:
            dropped.append(sym)
    return ",".join(kept), kept, dropped


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
    account: dict[str, Any] = {}
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
    # Use options_buying_power for the BP-fit check — that's the actual
    # collateral budget for CSPs, distinct from cash. Fall back to bp
    # when the broker doesn't return options_buying_power separately.
    options_bp = float(account.get("options_buying_power") or 0) or bp
    trading_plan = str(ProjectSettings.get(
        project_id, "trading_plan", default="balanced") or "balanced")

    # Compose layers (later wins):
    #   1. Strategy template baseline
    #   2. Cash-tier risk caps (concentration, collateral, contracts_per_csp)
    #   3. Trading-plan strategy aggressiveness (delta, DTE, IV floor,
    #      take-profit) — actually delivers on the user's stated risk
    #      identity. Aggressive plans on small accounts get the
    #      revenue-tuned defaults.
    #   4. Mode-specific seed defaults (spread width / delta / DTE,
    #      intraday flags) — only fills in keys the wheel-template
    #      doesn't carry; tier risk caps remain authoritative.
    strategy_mode = str(ProjectSettings.get(
        project_id, "strategy_mode", default="wheel") or "wheel").lower()
    combined: dict[str, Any] = dict(tpl["settings"])
    combined.update(_tier_overrides(tier))
    combined.update(_plan_overrides(trading_plan, tier))
    combined.update(_mode_overrides(strategy_mode, tier))

    # Step 5: BP-fit the watchlist. Drop any ticker whose typical strike
    # won't fit in the account's options_buying_power for a 1-contract
    # CSP. This is the fix for the "watchlist exists but nothing
    # trades" trap that was burning $5k accounts. We snapshot the
    # candidates first to make the filter price-aware rather than
    # using a static price table.
    dropped_for_bp: list[str] = []
    watchlist = combined.get("watchlist") or ""
    if watchlist and options_bp > 0 and strategy_mode in (
            "wheel", "wheel_plus_dca", "intraday_momentum"):
        try:
            from execution import get_broker as _gb
            client = _gb(project)
            syms = [s.strip().upper()
                    for s in str(watchlist).split(",") if s.strip()]
            snaps = client.snapshots(syms) if syms else {}
            filtered, kept, dropped_for_bp = _filter_watchlist_by_bp(
                watchlist, options_bp, snaps)
            if kept:
                combined["watchlist"] = filtered
                # Also auto-set scanner_max_price so the runtime filter
                # matches what we just BP-filtered.
                combined["scanner_max_price"] = round(
                    options_bp / 100.0, 2)
        except Exception as e:
            logger.warning("BP-fit watchlist filtering failed: %s", e)

    notes = _tier_notes(tier, cash, bp, combined)
    if note:
        notes = [note] + notes
    notes.append(
        f"trading_plan='{trading_plan}' on tier '{tier}': delta band "
        f"[{combined.get('csp_delta_min')}-{combined.get('csp_delta_max')}], "
        f"DTE [{combined.get('csp_min_dte')}-{combined.get('csp_max_dte')}], "
        f"take-profit at {int((combined.get('close_at_profit_pct') or 0)*100)}% "
        f"of max."
    )
    if dropped_for_bp:
        notes.append(
            f"Dropped {len(dropped_for_bp)} watchlist ticker(s) too "
            f"expensive for current options BP ${options_bp:,.0f}: "
            f"{', '.join(dropped_for_bp[:8])}"
            + (f" (+{len(dropped_for_bp)-8} more)"
               if len(dropped_for_bp) > 8 else "")
            + ". Add these back when the account grows."
        )
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
