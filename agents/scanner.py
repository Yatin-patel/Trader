"""Agent 1 — Market Scanner / Mover Analyzer.

Pulls a configurable universe from Alpaca and ranks the top movers passing the
project's liquidity + price filters. Nothing about the filter is hardcoded —
every threshold comes from project_settings.

Emits a plain-English `narrative` alongside the raw event payload so the user
can see exactly which tickers were considered and why each was kept or dropped.
"""
from __future__ import annotations

import logging
from typing import Any

from db.repositories import EventsRepo, ProjectsRepo
from db.settings_store import ProjectSettings
from execution import AlpacaClient

logger = logging.getLogger(__name__)

# A small, reasonable default universe used only if the project has no override.
# Real production deployments should populate `watchlist` in project_settings.
_DEFAULT_UNIVERSE = [
    "SPY", "QQQ", "IWM", "DIA",
    "AAPL", "MSFT", "NVDA", "AMD", "META", "GOOGL", "AMZN", "TSLA", "NFLX",
    "JPM", "BAC", "GS", "WFC", "C",
    "XOM", "CVX", "OXY",
    "JNJ", "PFE", "MRK", "LLY",
    "WMT", "COST", "TGT", "HD",
    "DIS", "BA", "F", "GM",
    "COIN", "PLTR", "SHOP", "PYPL",
    # NB: removed SQ — Block renamed its ticker to XYZ on Nasdaq;
    # Alpaca rejects SQ with 42210000 "invalid underlying symbols"
    # and the Strategist used to error every cycle on it. Add "XYZ"
    # if you want exposure to the renamed entity.
]


def _quarantined_symbols(project_id: str) -> set[str]:
    """Per-project blocklist. Tickers here are dropped from the universe
    before any filters run and never reach the Strategist. Use for
    delisted/renamed symbols (SQ → XYZ), names you don't want exposure
    to, or as a temporary backoff after a bad fill."""
    raw = ProjectSettings.get(project_id, "quarantined_symbols", default="")
    if not raw:
        return set()
    if isinstance(raw, list):
        items = raw
    else:
        items = str(raw).split(",")
    return {str(x).strip().upper() for x in items if str(x).strip()}


def _load_universe(project_id: str) -> list[str]:
    custom = ProjectSettings.get(project_id, "watchlist", default=None)
    if not custom:
        universe = list(_DEFAULT_UNIVERSE)
    elif isinstance(custom, str):
        universe = [s.strip().upper() for s in custom.split(",") if s.strip()]
    elif isinstance(custom, list):
        universe = [str(s).upper() for s in custom]
    else:
        universe = list(_DEFAULT_UNIVERSE)
    blocked = _quarantined_symbols(project_id)
    if blocked:
        universe = [t for t in universe if t.upper() not in blocked]
    return universe


def scan_movers_node(state: dict[str, Any]) -> dict[str, Any]:
    project_id = state["project_id"]
    project = ProjectsRepo.get(project_id)
    if project is None or not project.is_active:
        return {"target_tickers": [], "execution_status": "SCANNING"}

    volume_threshold = ProjectSettings.get(project_id, "volume_threshold")
    min_price = ProjectSettings.get(project_id, "scanner_min_price")
    max_price = ProjectSettings.get(project_id, "scanner_max_price")
    min_pct = ProjectSettings.get(project_id, "scanner_min_pct_change")
    top_n = ProjectSettings.get(project_id, "scanner_top_n")

    client = AlpacaClient(project)
    universe = _load_universe(project_id)
    try:
        snaps = client.snapshots(universe)
    except Exception as e:
        EventsRepo.log(project_id, "Scanner", "ERROR", {"err": str(e)})
        return {"target_tickers": [], "execution_status": "SCANNING"}

    # Walk every ticker, build a verdict per-ticker.
    candidates: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for sym in universe:
        snap = snaps.get(sym)
        if snap is None:
            rejected.append({"ticker": sym, "reason": "no snapshot data"})
            continue
        if snap.last_price <= 0 or snap.prev_close <= 0:
            rejected.append({"ticker": sym,
                             "reason": f"missing price (last={snap.last_price}, prev={snap.prev_close})"})
            continue
        if not (min_price <= snap.last_price <= max_price):
            rejected.append({"ticker": sym,
                             "reason": f"price ${snap.last_price:.2f} outside band [{min_price}-{max_price}]"})
            continue
        if snap.volume < volume_threshold:
            rejected.append({"ticker": sym,
                             "reason": f"volume {snap.volume:,} below threshold {volume_threshold:,}"})
            continue
        if abs(snap.pct_change) < min_pct:
            rejected.append({"ticker": sym,
                             "reason": f"%-change {snap.pct_change:+.2f}% below floor {min_pct}%"})
            continue
        candidates.append({
            "ticker": snap.symbol,
            "price": snap.last_price,
            "prev_close": snap.prev_close,
            "volume": snap.volume,
            "pct_change": snap.pct_change,
            "abs_pct_change": abs(snap.pct_change),
        })

    candidates.sort(key=lambda c: c["abs_pct_change"], reverse=True)
    top = candidates[:top_n]
    tickers = [c["ticker"] for c in top]

    # ---------------- Build plain-English narrative ----------------
    narrative: list[str] = []
    narrative.append(
        f"Scanned {len(universe)} tickers in the watchlist."
    )
    narrative.append(
        f"Filters in effect: price ${min_price:.0f}-${max_price:.0f}, "
        f"volume ≥ {volume_threshold:,}, "
        f"absolute %-change ≥ {min_pct}%."
    )
    narrative.append(
        f"{len(candidates)} ticker(s) passed all filters; "
        f"{len(rejected)} were dropped."
    )

    if top:
        narrative.append(
            f"Top {len(top)} movers (ranked by absolute %-change) advance "
            f"to the Strategist:"
        )
        for rank, c in enumerate(top, 1):
            narrative.append(
                f"  #{rank} {c['ticker']}: "
                f"price ${c['price']:.2f}, "
                f"{c['pct_change']:+.2f}% vs prev close ${c['prev_close']:.2f}, "
                f"volume {c['volume']:,}"
            )
    else:
        narrative.append(
            "No ticker passed all filters this cycle — nothing to send to the Strategist."
        )

    # Brief summary of why the loudest rejections happened (cap at 5)
    if rejected:
        narrative.append("Examples of rejections this cycle:")
        for r in rejected[:5]:
            narrative.append(f"  • {r['ticker']}: {r['reason']}")
        if len(rejected) > 5:
            narrative.append(f"  …and {len(rejected) - 5} more rejected for similar reasons.")

    EventsRepo.log(project_id, "Scanner", "SCAN", {
        "universe_size": len(universe),
        "passed_filters": len(candidates),
        "selected": tickers,
        "filters": {
            "volume_threshold": volume_threshold,
            "price_band": [min_price, max_price],
            "min_pct_change": min_pct,
        },
        "candidates": top,
        "rejected_sample": rejected[:10],
        "narrative": narrative,
    })

    return {
        "target_tickers": tickers,
        "candidate_details": top,
        "execution_status": "SCANNING",
    }
