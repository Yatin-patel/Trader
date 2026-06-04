"""Account-size tiers + matching settings bundles.

Why this exists
---------------
The default project settings are tuned for a mid-sized account. On a
~$5 K account they're hostile:

  * volume_threshold=3 000 000 excludes every cheap-stock candidate
  * scanner_max_price=100 excludes everything else
  * net result: Scanner.SCAN passed=0 every cycle → Strategist never
    gets to pick → nothing trades

This module solves it by classifying a project's account size into a
tier (small / mid / large) and shipping an opinionated bundle of
settings for each. The bundle is APPLIED via ``apply_tier`` which
writes the tier values into ``project_settings`` — the user can then
override any individual key via the normal settings UI.

The tier ladder is intentionally coarse — three buckets, not ten —
because the goal is "make it work" not "perfect tuning". A user who
wants finer control just edits the settings; the tier is a starting
point, not a cage.
"""
from __future__ import annotations

from typing import Any

from db.repositories import EventsRepo, ProjectsRepo
from db.settings_store import ProjectSettings


# ---------------------------------------------------------------------------
# Watchlist tiers — these are the underlyings that are reasonable for
# each account size given how much collateral one CSP needs.
# ---------------------------------------------------------------------------
_SMALL_WATCHLIST = [
    # Sub-$30, high enough liquidity that even thin small-cap volume
    # supports a single contract.  Avoids names that consistently trade
    # below 200k volume to keep slippage manageable.
    "F", "SOFI", "NIO", "RIVN", "PLTR", "HOOD",
    "NU", "MARA", "RIOT", "SNAP", "T", "BAC",
    "PFE", "INTC", "PYPL", "UBER",
]

_MID_WATCHLIST = [
    # Mid-priced US large + small caps.  Drops the deep-tech mega-caps
    # whose strikes won't fit in a sub-$50 K options BP envelope without
    # blowing per-ticker concentration.
    # NB: SQ excluded — Block renamed its ticker to XYZ; Alpaca rejects
    # SQ with 42210000.  Add XYZ if you want exposure to renamed entity.
    "F", "SOFI", "NIO", "HOOD", "PLTR", "COIN",
    "MARA", "RIOT", "SHOP", "SNAP", "U",
    "DDOG", "SNOW", "CRM", "ORCL", "ADBE", "PYPL",
    "BAC", "C", "WFC", "JPM", "GS",
    "XOM", "CVX", "OXY",
    "DKNG", "INTC", "AMD", "DIS", "BA", "GM",
]

_LARGE_WATCHLIST = [
    # Full universe — anything that has an actively traded options chain
    # and is liquid enough to support multi-contract positions.
    # NB: SQ excluded (renamed to XYZ on Nasdaq, Alpaca returns 422).
    "SPY", "QQQ", "IWM", "DIA",
    "AAPL", "MSFT", "NVDA", "AMD", "META", "GOOGL", "AMZN", "TSLA", "NFLX",
    "JPM", "BAC", "GS", "WFC", "C",
    "XOM", "CVX", "OXY",
    "JNJ", "PFE", "MRK", "LLY",
    "WMT", "COST", "TGT", "HD",
    "DIS", "BA", "F", "GM",
    "COIN", "PLTR", "SHOP", "PYPL",
]


# ---------------------------------------------------------------------------
# Tier settings bundles.  Each value-type matches ProjectSettings.DEFAULTS.
# Keys NOT in a bundle are left alone (no override).
# ---------------------------------------------------------------------------
TIER_PRESETS: dict[str, dict[str, Any]] = {
    "small": {
        # Universe / scanner
        "watchlist":                       ",".join(_SMALL_WATCHLIST),
        "scanner_min_price":               2.0,
        "scanner_max_price":               30.0,
        "scanner_min_pct_change":          1.0,
        "scanner_top_n":                   10,
        "volume_threshold":                500_000,
        # CSP envelope
        "csp_delta_min":                   0.20,
        "csp_delta_max":                   0.40,
        "csp_min_dte":                     5,
        "csp_max_dte":                     21,
        "min_iv_rank":                     0.20,
        # Sizing
        "contracts_per_csp":               1,
        "max_open_contracts":              5,
        "max_contracts_per_ticker":        2,
        # Risk caps — loose because a $5 K account can't afford the
        # luxury of diversification; the safety comes from small positions.
        "max_collateral_pct":              0.90,
        "max_concentration_per_ticker":    1.0,    # effectively off
        "max_concentration_per_sector":    1.0,    # effectively off
        "max_net_delta":                   500.0,
        "max_net_vega":                    500.0,
        "stop_loss_dollars":               2.0,
    },
    "mid": {
        "watchlist":                       ",".join(_MID_WATCHLIST),
        "scanner_min_price":               3.0,
        "scanner_max_price":               150.0,
        "scanner_min_pct_change":          1.0,
        "scanner_top_n":                   12,
        "volume_threshold":                1_000_000,
        "csp_delta_min":                   0.20,
        "csp_delta_max":                   0.40,
        "csp_min_dte":                     7,
        "csp_max_dte":                     28,
        "min_iv_rank":                     0.30,
        "contracts_per_csp":               1,
        "max_open_contracts":              10,
        "max_contracts_per_ticker":        3,
        "max_collateral_pct":              0.75,
        "max_concentration_per_ticker":    0.30,
        "max_concentration_per_sector":    0.40,
        "max_net_delta":                   2000.0,
        "max_net_vega":                    2000.0,
        "stop_loss_dollars":               5.0,
    },
    "large": {
        "watchlist":                       ",".join(_LARGE_WATCHLIST),
        "scanner_min_price":               5.0,
        "scanner_max_price":               600.0,
        "scanner_min_pct_change":          1.5,
        "scanner_top_n":                   15,
        "volume_threshold":                2_000_000,
        "csp_delta_min":                   0.20,
        "csp_delta_max":                   0.35,
        "csp_min_dte":                     14,
        "csp_max_dte":                     35,
        "min_iv_rank":                     0.40,
        "contracts_per_csp":               2,
        "max_open_contracts":              20,
        "max_contracts_per_ticker":        5,
        "max_collateral_pct":              0.70,
        "max_concentration_per_ticker":    0.20,
        "max_concentration_per_sector":    0.30,
        "max_net_delta":                   5000.0,
        "max_net_vega":                    5000.0,
        "stop_loss_dollars":               10.0,
    },
}


# Value-type lookup so apply_tier persists each value at the correct type.
_VALUE_TYPES = {
    # strings
    "watchlist": "string",
    # ints
    "scanner_top_n": "int",
    "volume_threshold": "int",
    "csp_min_dte": "int",
    "csp_max_dte": "int",
    "contracts_per_csp": "int",
    "max_open_contracts": "int",
    "max_contracts_per_ticker": "int",
    # floats (everything else)
}


def classify_tier(equity_or_alloc: float) -> str:
    """Map a dollar amount (equity OR max_equity_allocation) to a tier."""
    v = float(equity_or_alloc or 0)
    if v < 10_000:
        return "small"
    if v < 100_000:
        return "mid"
    return "large"


def detect_tier_for_project(project_id: str) -> str:
    """Pick the larger of (equity, max_equity_allocation) so a small
    deposit + high allocation doesn't get the small-tier treatment."""
    project = ProjectsRepo.get(project_id)
    if project is None:
        return "small"
    budget = float(getattr(project, "max_equity_allocation", 0) or 0)
    # Try to get live equity too — if it's much larger than the budget,
    # tier should reflect what the user actually has access to.
    live_equity = 0.0
    try:
        from execution import AlpacaClient
        client = AlpacaClient(project)
        acct = client.get_account()
        live_equity = float(acct.get("equity") or 0)
    except Exception:
        pass
    reference = max(budget, live_equity)
    return classify_tier(reference)


def apply_tier(project_id: str, tier: str | None = None, *,
               overwrite: bool = True) -> dict[str, Any]:
    """Persist the tier's setting bundle into project_settings.

    ``overwrite=True`` (the default) writes every key in the bundle,
    replacing any prior user setting. ``overwrite=False`` only fills
    in keys the user hasn't already set (gentle nudge mode).
    """
    if tier is None:
        tier = detect_tier_for_project(project_id)
    if tier not in TIER_PRESETS:
        return {"error": f"unknown tier {tier!r}"}
    bundle = TIER_PRESETS[tier]
    applied: dict[str, Any] = {}
    skipped: list[str] = []
    for key, value in bundle.items():
        if not overwrite:
            existing = ProjectSettings.get(project_id, key, default=None)
            if existing is not None and existing != "":
                skipped.append(key)
                continue
        vt = _VALUE_TYPES.get(key, "float")
        ProjectSettings.set(project_id, key, value, value_type=vt)
        applied[key] = value
    EventsRepo.log(project_id, "Manual", "AUTO_TUNE", {
        "tier": tier, "applied": list(applied.keys()),
        "skipped": skipped,
        "narrative": [
            f"Applied '{tier}' tier preset: {len(applied)} setting(s) "
            f"updated, {len(skipped)} preserved by user.",
        ],
    })
    return {"tier": tier, "applied": applied,
            "skipped": skipped, "preset_size": len(bundle)}
