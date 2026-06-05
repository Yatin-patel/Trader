"""Build a market-aware watchlist per project, per day.

Why this exists
---------------
Before today's commit the watchlist was a static list of 25-30 names
hardcoded in intelligence.optimizer. A static list locks every project
out of:
  * Today's momentum (the names actually moving today)
  * IV-rich names where premium is worth collecting
  * Capital efficiency — names whose strike doesn't fit account BP
    get silently rejected by the Strategist every cycle

Real brokers refresh their lists daily. So do we now.

Pipeline
--------
1. Build a CORE anchor list (tier-appropriate, stable across days)
2. Pull MOMENTUM candidates from broker's optionable universe
3. Optionally augment with IV-RICH candidates (when IV-rank data exists)
4. Filter by BP fit, earnings risk, liquidity, price band
5. Cap at ``dynamic_watchlist_max_size`` and persist
"""
from __future__ import annotations

import logging
import math
from typing import Any

from db.repositories import EventsRepo, ProjectsRepo
from db.settings_store import ProjectSettings
from execution import BrokerReauthRequired, get_broker

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tier-appropriate CORE anchors. These are the "always-include" names
# for each cash tier. They give the dynamic refresher a stable floor so
# even on a slow day or when the broker's snapshot API misbehaves the
# project still has SOMETHING to trade. The dynamic momentum/IV-rich
# augmentation layers on top.
# ---------------------------------------------------------------------------
_CORE_TINY = "F,SOFI,NIO,RIVN,SNAP,T,WBD,MARA,RIOT,PFE,NU,IBIT"
_CORE_SMALL = (
    "F,SOFI,NIO,RIVN,PLTR,HOOD,NU,MARA,RIOT,SNAP,T,BAC,WBD,"
    "PFE,KMI,INTC,WFC,IBIT,AMD,U"
)
_CORE_MID = (
    "AAPL,MSFT,NVDA,AMD,META,F,SOFI,PLTR,COIN,GOOGL,"
    "HOOD,SNAP,U,INTC,QCOM,MU,SHOP"
)
_CORE_LARGE = _CORE_MID + (
    ",AMZN,AVGO,JPM,BAC,WFC,GS,V,MA,KO,PEP,MCD,WMT,COST,DIS,KMI"
)

# Curated pool of names known to have LIQUID options chains. The
# momentum scorer scans this pool (not the broker's alphabetical
# active-equities list) because the broker's list returns small-cap
# names in alphabetical order that happen to be moving but have no
# real options market. ~200 names spanning sectors + cap sizes.
_LIQUID_OPTIONABLE_POOL = (
    # Mega-cap tech
    "AAPL,MSFT,NVDA,AMZN,GOOGL,GOOG,META,TSLA,AVGO,ORCL,CRM,ADBE,"
    "AMD,QCOM,MU,INTC,TXN,IBM,CSCO,INTU,NOW,UBER,SQ,SHOP,PYPL,"
    # Financials
    "JPM,BAC,WFC,GS,MS,C,USB,PNC,TFC,COF,AXP,V,MA,SCHW,BLK,"
    "BX,KKR,APO,"
    # Consumer
    "KO,PEP,WMT,COST,MCD,SBUX,NKE,DIS,LULU,DKS,TGT,HD,LOW,"
    "BBY,CHWY,EBAY,ETSY,YUM,CMG,ABNB,BKNG,MAR,UBER,LYFT,"
    # Healthcare
    "JNJ,UNH,PFE,MRK,LLY,ABBV,TMO,DHR,ABT,BMY,GILD,AMGN,"
    "MRNA,BNTX,CVS,WBA,HUM,CI,CRISP,VRTX,REGN,BIIB,"
    # Industrial / Energy
    "BA,CAT,GE,HON,LMT,RTX,UNP,DE,F,GM,STLA,XOM,CVX,COP,SLB,"
    "OXY,KMI,EOG,PSX,VLO,MPC,UAL,DAL,AAL,LUV,CCL,RCL,NCLH,"
    # ETFs (very liquid options)
    "SPY,QQQ,IWM,DIA,XLF,XLE,XLK,XLV,XLI,XLU,XLY,XLP,XLB,"
    "GLD,SLV,USO,UNG,EFA,EEM,FXI,VWO,TLT,IEF,HYG,LQD,"
    "ARKK,SOXL,TQQQ,SQQQ,SOXX,SMH,IBIT,FBTC,GBTC,"
    # High-IV small/mid caps
    "PLTR,COIN,RIVN,LCID,NIO,XPEV,LI,ROKU,SNAP,U,RBLX,DASH,"
    "AFRM,UPST,SOFI,HOOD,NU,T,VZ,VALE,X,GME,AMC,WBD,SIRI,"
    "MARA,RIOT,CLSK,CIFR,WULF,IREN,"
    "PFE,KEY,BAC,WFC,KMI,LUMN,GRAB,DISH,"
    # Semis
    "AVGO,MRVL,WDC,STX,NXPI,ON,LRCX,KLAC,AMAT,ASML,ARM,"
    # AI / cloud / cyber
    "PLTR,SNOW,DDOG,NET,CRWD,ZS,OKTA,PANW,FTNT,S,CFLT,MDB,"
    "PATH,AI,SOUN,BBAI"
).strip()


def _cash_tier(cash: float) -> str:
    if cash < 5_000:
        return "tiny"
    if cash < 25_000:
        return "small"
    if cash < 100_000:
        return "medium"
    return "large"


def _core_for(tier: str) -> list[str]:
    if tier == "tiny":
        return _CORE_TINY.split(",")
    if tier == "small":
        return _CORE_SMALL.split(",")
    if tier == "medium":
        return _CORE_MID.split(",")
    return _CORE_LARGE.split(",")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_proposed_watchlist(project_id: str) -> dict[str, Any]:
    """Build a proposed watchlist WITHOUT persisting. Returns:
        {
          "tier": str,
          "options_bp": float,
          "core": [syms],
          "momentum": [syms],
          "iv_rich": [syms],
          "final": [syms],
          "dropped_for_bp": [...],
          "dropped_for_earnings": [...],
        }
    The Optimize-Now button uses this to preview a refresh; the
    market-open scheduler tick uses refresh_watchlist() which calls
    this then persists if changed.
    """
    project = ProjectsRepo.get(project_id)
    if project is None:
        return {"error": "project not found"}
    try:
        client = get_broker(project)
        account = client.get_account()
    except BrokerReauthRequired:
        return {"error": "broker reauth required"}
    except Exception as e:
        return {"error": f"broker fetch failed: {e}"}

    cash = float(account.get("cash") or 0)
    options_bp = float(account.get("options_buying_power") or 0)
    if options_bp <= 0:
        options_bp = float(account.get("buying_power") or 0)

    tier = _cash_tier(cash)
    core = _core_for(tier)
    max_size = int(ProjectSettings.get(
        project_id, "dynamic_watchlist_max_size", default=30) or 30)
    iv_floor = float(ProjectSettings.get(
        project_id, "dynamic_watchlist_min_iv_rank",
        default=0.30) or 0.30)

    # ---- Momentum candidates from curated LIQUID OPTIONABLE pool -------
    # We DON'T use client.active_us_equities() — that returns names in
    # alphabetical (or listing-date) order so it surfaces obscure
    # small-caps that happen to be moving but have no options market.
    # The curated pool is ~200 names known to have liquid options.
    momentum: list[str] = []
    pool = [s.strip().upper() for s in _LIQUID_OPTIONABLE_POOL.split(",")
            if s.strip()]
    # Filter out names we already have in core (no need to re-rank)
    cands = [s for s in pool if s not in core]
    # Snapshot in bulk + score by abs(pct_change) × log(volume)
    snaps: dict[str, Any] = {}
    if cands:
        try:
            snaps = client.snapshots(cands)
        except Exception as e:
            logger.warning("snapshot batch failed: %s", e)
            snaps = {}
    scored: list[tuple[str, float, float, float]] = []
    for sym in cands:
        snap = snaps.get(sym)
        if snap is None:
            continue
        try:
            last_price = float(getattr(snap, "last_price", 0) or 0)
            pct = abs(float(getattr(snap, "pct_change", 0) or 0))
            vol = float(getattr(snap, "volume", 0) or 0)
        except Exception:
            continue
        if last_price <= 0 or vol <= 0 or pct < 0.5:
            continue
        # BP fit (skip names that bust BP).
        if options_bp > 0 and last_price * 1.05 > options_bp / 100:
            continue
        # Liquidity guards — minimum bar to be a "real" momentum
        # candidate (vs an obscure micro-cap that bubbled up because
        # it happened to move + have some volume). The thresholds
        # are intentionally LOW so intraday snapshots (partial-day
        # volume during the session) still surface real names:
        #   1. ≥ $3 share price (avoid sub-penny chains)
        #   2. ≥ 1M shares traded so far today
        #   3. ≥ $5M dollar-volume (price × volume) so far today
        # Combined with the curated pool, this is enough to keep
        # micro-caps out while letting real movers in.
        if last_price < 3.0:
            continue
        if vol < 1_000_000:
            continue
        if last_price * vol < 5_000_000:
            continue
        # Score: pct × log(volume) — higher = more momentum + liquid
        score = pct * math.log(max(vol, 1.0))
        scored.append((sym, score, last_price, pct))
    scored.sort(key=lambda t: -t[1])
    # Take top N momentum candidates after the core anchors are in.
    momentum_budget = max(0, max_size - len(core))
    momentum = [s for s, _, _, _ in scored[:momentum_budget]]

    # ---- News-aware augmentation (free RSS feeds) -----------------------
    # Bias the momentum scorer toward tickers with active news flow in
    # the last ~30 min. A ticker that's both moving + getting written
    # about is more likely to keep moving than one moving on no news.
    # Pull from MarketWatch / CNBC / Yahoo headlines / Seeking Alpha /
    # WSB — all free, no rate limits. Falls through silently when
    # the news layer is unavailable so the watchlist still ships.
    news_enabled = bool(ProjectSettings.get(
        project_id, "news_aware_watchlist", default=True))
    news_added: list[str] = []
    if news_enabled:
        try:
            from news import get_news_mentions
            pool_set = set(pool)
            # Re-score: existing momentum-score + news bonus.
            for idx, (sym, score, last_price, pct) in enumerate(scored):
                mentions = get_news_mentions(sym, pool_set)
                if mentions and mentions.count > 0:
                    # Each mention adds 5% to the score; cap at 50%
                    # so a runaway-mention name doesn't dominate.
                    news_bonus = min(0.5, 0.05 * mentions.count)
                    scored[idx] = (
                        sym,
                        score * (1.0 + news_bonus),
                        last_price,
                        pct,
                    )
            # Re-sort after the news bonus + collect which got boosted.
            scored.sort(key=lambda t: -t[1])
            boosted = []
            for sym, _, _, _ in scored[:momentum_budget]:
                mentions = get_news_mentions(sym, pool_set)
                if mentions and mentions.count > 0:
                    boosted.append(sym)
            news_added = boosted
            # Refresh momentum based on the news-adjusted ordering.
            momentum = [s for s, _, _, _ in scored[:momentum_budget]]
        except Exception as e:
            logger.warning("news-aware scoring failed: %s", e)

    # ---- IV-rich layer (optional augmentation) --------------------------
    iv_rich: list[str] = []
    try:
        from analytics.iv_rank import get_iv_rank
        # Re-score top scored candidates by IV-rank, keep the IV-rich ones
        rerank: list[tuple[str, float]] = []
        for sym, _, _, _ in scored[:50]:
            try:
                iv = get_iv_rank(project_id, sym)
            except Exception:
                iv = None
            if iv is not None and iv >= iv_floor:
                rerank.append((sym, iv))
        rerank.sort(key=lambda t: -t[1])
        iv_rich = [s for s, _ in rerank[:5]]  # small augment
    except Exception:
        pass

    # ---- Combine + final filters ---------------------------------------
    combined: list[str] = []
    seen: set[str] = set()
    for s in core + momentum + iv_rich:
        u = s.upper().strip()
        if u and u not in seen:
            seen.add(u)
            combined.append(u)

    # NB: we intentionally do NOT filter the dynamic list by upcoming
    # earnings here. risk.earnings hits Yahoo Finance per ticker and a
    # 30-symbol refresh would burn ~30 API calls every market open,
    # which Yahoo rate-limits at HTTP 429 within ~10 names. The
    # Strategist already checks earnings per-trade in its main loop
    # (avoid_earnings_within_dte gate) so dropping a name here is
    # redundant. Keep the field on the response for forward-compat.
    dropped_earnings: list[str] = []

    # Final BP-fit pass (in case core has names that don't fit BP).
    dropped_bp: list[str] = []
    if options_bp > 0:
        final: list[str] = []
        if combined:
            try:
                final_snaps = client.snapshots(combined)
            except Exception:
                final_snaps = {}
            for s in combined:
                snap = final_snaps.get(s)
                if snap is None:
                    final.append(s)  # be conservative
                    continue
                try:
                    last_price = float(
                        getattr(snap, "last_price", 0) or 0)
                except Exception:
                    last_price = 0
                if last_price <= 0:
                    final.append(s)
                    continue
                if last_price * 1.05 > options_bp / 100:
                    dropped_bp.append(s)
                else:
                    final.append(s)
            combined = final
    combined = combined[:max_size]

    return {
        "tier":                tier,
        "cash":                cash,
        "options_bp":          options_bp,
        "core":                core,
        "momentum":            momentum,
        "iv_rich":             iv_rich,
        "news_boosted":        news_added,
        "final":               combined,
        "dropped_for_bp":      dropped_bp,
        "dropped_for_earnings": dropped_earnings,
    }


def refresh_watchlist(project_id: str, *,
                      force: bool = False) -> dict[str, Any]:
    """Build a fresh watchlist and persist it if changed. Returns a
    summary the scheduler logs. ``force=True`` bypasses the
    dynamic_watchlist_enabled gate (used by the Optimize-Now button)."""
    enabled = bool(ProjectSettings.get(
        project_id, "dynamic_watchlist_enabled", default=True))
    if not enabled and not force:
        return {"status": "disabled", "project_id": project_id}

    proposal = get_proposed_watchlist(project_id)
    if "error" in proposal:
        return {"status": "error", "err": proposal["error"]}

    final = proposal["final"]
    if not final:
        EventsRepo.log(project_id, "DynamicWatchlist", "REFRESH", {
            "status": "no_candidates",
            "tier": proposal["tier"],
            "options_bp": proposal["options_bp"],
            "narrative": [
                "Dynamic watchlist refresh produced 0 candidates after "
                "BP / earnings / momentum filters. Existing watchlist "
                "left in place.",
            ],
        })
        return {"status": "no_candidates", "project_id": project_id}

    new_watchlist = ",".join(final)
    old_watchlist = (ProjectSettings.get(
        project_id, "watchlist", default="") or "")

    if new_watchlist == old_watchlist:
        return {"status": "unchanged", "project_id": project_id,
                "count": len(final)}

    ProjectSettings.set(project_id, "watchlist", new_watchlist)
    EventsRepo.log(project_id, "Manual", "SETTING_CHANGE", {
        "key":    "watchlist",
        "old":    old_watchlist,
        "new":    new_watchlist,
        "source": "dynamic_watchlist_refresh",
    })

    diff_added = sorted(set(final) - set(
        s.strip().upper() for s in old_watchlist.split(",")
        if s.strip()))
    diff_removed = sorted(set(
        s.strip().upper() for s in old_watchlist.split(",")
        if s.strip()) - set(final))

    narrative = [
        f"Dynamic watchlist refresh on tier '{proposal['tier']}': "
        f"{len(final)} tickers. Core anchors: {len(proposal['core'])}, "
        f"momentum-added: {len(proposal['momentum'])}, "
        f"IV-rich-added: {len(proposal['iv_rich'])}. "
        f"Dropped {len(proposal['dropped_for_bp'])} for BP, "
        f"{len(proposal['dropped_for_earnings'])} for earnings."
    ]
    if diff_added:
        narrative.append(
            f"  Added: {', '.join(diff_added[:10])}"
            + (f" (+{len(diff_added)-10} more)"
               if len(diff_added) > 10 else "")
        )
    if diff_removed:
        narrative.append(
            f"  Removed: {', '.join(diff_removed[:10])}"
            + (f" (+{len(diff_removed)-10} more)"
               if len(diff_removed) > 10 else "")
        )
    EventsRepo.log(project_id, "DynamicWatchlist", "REFRESH", {
        "tier":             proposal["tier"],
        "options_bp":       proposal["options_bp"],
        "added":            diff_added,
        "removed":          diff_removed,
        "core":             proposal["core"],
        "momentum":         proposal["momentum"],
        "iv_rich":          proposal["iv_rich"],
        "dropped_for_bp":   proposal["dropped_for_bp"],
        "dropped_for_earnings": proposal["dropped_for_earnings"],
        "narrative":        narrative,
    })

    return {
        "status":  "refreshed",
        "project_id": project_id,
        "count":   len(final),
        "added":   diff_added,
        "removed": diff_removed,
    }
