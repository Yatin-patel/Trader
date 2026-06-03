"""Market Outlook: top performers ranking + 30/60/90-day forecasts.

Two halves:
  * Top performers — composite ranking by realized P&L, win rate, recent
    momentum, and current IV rank, across (traded ∪ scanner watchlist ∪
    S&P watchlist).
  * Outlook per ticker per horizon — hybrid quant (lognormal projection
    over trailing 1y daily returns) + LLM narrative. Cached 12h in
    market_outlook_cache.
"""
from __future__ import annotations

import json
import logging
import math
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text

from analytics.iv_rank import get_iv_rank
from db.connection import session_scope
from db.repositories import ProjectsRepo
from db.settings_store import ProjectSettings
from execution import AlpacaClient, BrokerNotConfigured, get_broker

logger = logging.getLogger(__name__)

_CACHE_TTL_HOURS = 12
_HORIZONS = [30, 60, 90]

# Representative S&P large caps as a market-wide top performers universe.
_SP_TOP = [
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "BRK.B", "AVGO", "LLY",
    "JPM", "UNH", "V", "WMT", "XOM", "MA", "JNJ", "PG", "HD", "ORCL",
    "COST", "ABBV", "BAC", "MRK", "CVX", "KO", "ADBE", "PEP", "TMO", "AMD",
    "CRM", "MCD", "ACN", "NFLX", "LIN", "ABT", "CSCO", "AMGN", "WFC", "DIS",
    "INTC", "PFE", "QCOM", "TXN", "PM", "IBM", "GE", "RTX", "CMCSA", "GS",
]


# --------------------------- Universe -----------------------------------------

def build_universe(project_id: str, *, cap: int = 120) -> list[str]:
    """Union of (user watchlist, traded tickers, S&P fallback), capped.

    Watchlist comes first so user-curated symbols always make it into the
    universe even when the cap is binding.
    """
    out: dict[str, None] = {}

    custom = ProjectSettings.get(project_id, "watchlist", default=None)
    if custom:
        if isinstance(custom, str):
            for s in custom.split(","):
                s = s.strip().upper()
                if s:
                    out[s] = None
        elif isinstance(custom, list):
            for s in custom:
                key = str(s).strip().upper()
                if key:
                    out[key] = None

    with session_scope() as s:
        rows = s.execute(text("""
            SELECT DISTINCT ticker FROM closed_contracts
            WHERE project_id = :p
        """), {"p": project_id}).fetchall()
    for r in rows:
        if r[0]:
            out[str(r[0]).upper()] = None

    for t in _SP_TOP:
        out[t.upper()] = None

    return list(out.keys())[:cap]


# --------------------------- History helpers ----------------------------------

def _fetch_history(client: AlpacaClient, ticker: str, lookback_days: int = 260) -> list[dict]:
    """Wrap AlpacaClient.daily_bars with exception isolation."""
    try:
        return client.daily_bars(ticker, lookback_days=lookback_days)
    except Exception as e:
        logger.debug("daily_bars failed for %s: %s", ticker, e)
        return []


def _trailing_return(bars: list[dict], n: int) -> float | None:
    if len(bars) < n + 1:
        return None
    last = float(bars[-1]["c"])
    past = float(bars[-1 - n]["c"])
    if past <= 0:
        return None
    return (last / past) - 1.0


def _log_returns(bars: list[dict]) -> list[float]:
    closes = [float(b["c"]) for b in bars if float(b["c"]) > 0]
    out = []
    for i in range(1, len(closes)):
        out.append(math.log(closes[i] / closes[i - 1]))
    return out


# --------------------------- Per-ticker metrics -------------------------------

def _wheel_metrics(project_id: str, ticker: str) -> dict[str, Any]:
    """Return realized_pnl, win_rate, n_cycles for a ticker (project-scoped)."""
    with session_scope() as s:
        row = s.execute(text("""
            SELECT
                COUNT(*) AS n,
                COALESCE(SUM(realized_pnl), 0) AS total,
                SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) AS wins
            FROM closed_contracts
            WHERE project_id = :p AND ticker = :t
        """), {"p": project_id, "t": ticker}).fetchone()
    n = int(row[0] or 0)
    total = float(row[1] or 0.0)
    wins = int(row[2] or 0)
    return {
        "n_cycles": n,
        "realized_pnl": round(total, 2),
        "win_rate": round(wins / n, 4) if n else None,
    }


def _momentum(bars: list[dict]) -> dict[str, Any]:
    return {
        "last_price": round(float(bars[-1]["c"]), 2) if bars else None,
        "mom_1m": _trailing_return(bars, 21),
        "mom_3m": _trailing_return(bars, 63),
        "mom_6m": _trailing_return(bars, 126),
    }


# --------------------------- Top Performers ranking ---------------------------

def _normalize(values: list[float | None]) -> list[float]:
    """Min-max normalize to [0,1]; None → 0."""
    nums = [v for v in values if v is not None]
    if not nums:
        return [0.0] * len(values)
    lo, hi = min(nums), max(nums)
    if hi - lo < 1e-9:
        return [0.5 if v is not None else 0.0 for v in values]
    return [((v - lo) / (hi - lo)) if v is not None else 0.0 for v in values]


def top_performers(project_id: str, *, limit: int = 25) -> list[dict[str, Any]]:
    """Compose ranking by realized P&L + win rate + momentum + IV rank.

    Bars and IV rank are fetched in parallel so a 100-ticker universe
    completes in ~5s instead of ~50s.
    """
    project = ProjectsRepo.get(project_id)
    if project is None:
        return []
    # Outlook needs daily bars + IV-rank, which depend on Alpaca's market
    # data API. For ETrade projects we fall back gracefully — return empty
    # rankings with a marker so the UI shows a friendly message.
    if (getattr(project, "broker_type", "alpaca") or "alpaca") != "alpaca":
        return [{
            "_broker_unsupported": True,
            "broker_type": project.broker_type,
            "message": ("Market Outlook uses Alpaca's daily-bars + options API "
                        "for ranking. Phase 2 will add ETrade + yfinance "
                        "fallbacks. For now, switch to an Alpaca project "
                        "to see rankings."),
        }]
    client = AlpacaClient(project)

    universe = build_universe(project_id)

    def _enrich(t: str) -> dict[str, Any] | None:
        bars = _fetch_history(client, t, lookback_days=160)
        wm = _wheel_metrics(project_id, t)
        if not bars:
            if wm["n_cycles"] == 0:
                return None
            return {
                "ticker": t, "last_price": None,
                "mom_1m": None, "mom_3m": None, "mom_6m": None,
                "iv_rank": None,
                **wm,
            }
        mm = _momentum(bars)
        try:
            iv = get_iv_rank(project_id, t)
        except Exception:
            iv = None
        return {"ticker": t, "iv_rank": iv, **mm, **wm}

    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(_enrich, universe))
    rows = [r for r in results if r is not None]

    if not rows:
        return []

    # Composite score: equal-weight normalized signals.
    s_pnl = _normalize([r["realized_pnl"] for r in rows])
    s_win = _normalize([r["win_rate"] for r in rows])
    s_m1 = _normalize([r["mom_1m"] for r in rows])
    s_m3 = _normalize([r["mom_3m"] for r in rows])
    s_m6 = _normalize([r["mom_6m"] for r in rows])
    s_iv = _normalize([r["iv_rank"] for r in rows])

    for i, r in enumerate(rows):
        r["score"] = round(
            0.20 * s_pnl[i] + 0.15 * s_win[i] +
            0.10 * s_m1[i] + 0.15 * s_m3[i] + 0.15 * s_m6[i] +
            0.25 * s_iv[i],
            4,
        )

    rows.sort(key=lambda r: r["score"], reverse=True)
    out = rows[:limit]
    # Stamp universe metadata on the first row so the UI can show it.
    if out:
        out[0]["_universe_size"] = len(universe)
        out[0]["_ranked_size"] = len(rows)
    return out


# --------------------------- Quant outlook (lognormal projection) -------------

def _quant_outlook(bars: list[dict], horizon_days: int) -> dict[str, Any] | None:
    """Project price bands using lognormal returns over trailing year."""
    rets = _log_returns(bars)
    if len(rets) < 60:
        return None
    mu = sum(rets) / len(rets)              # daily drift
    var = sum((r - mu) ** 2 for r in rets) / max(1, len(rets) - 1)
    sigma = math.sqrt(var)
    t = horizon_days
    spot = float(bars[-1]["c"])
    drift = (mu - 0.5 * sigma * sigma) * t
    vol = sigma * math.sqrt(t)

    def at(z: float) -> float:
        return round(spot * math.exp(drift + vol * z), 2)

    low = at(-1.28)     # ~10th percentile
    mid = at(0.0)
    high = at(1.28)     # ~90th percentile
    prob_up = round(_norm_cdf(drift / vol if vol > 0 else 0.0), 4)

    return {
        "spot": round(spot, 2),
        "horizon_days": horizon_days,
        "low": low, "mid": mid, "high": high,
        "expected_return_pct": round(((mid - spot) / spot) * 100, 2),
        "prob_up": prob_up,
        "annualized_vol": round(sigma * math.sqrt(252), 4),
        "annualized_drift": round(mu * 252, 4),
    }


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


# --------------------------- Cache + LLM outlook ------------------------------

def _cache_get(ticker: str, horizon: int) -> dict[str, Any] | None:
    with session_scope() as s:
        row = s.execute(text("""
            SELECT quant_json, llm_text, confidence, direction, generated_at
            FROM market_outlook_cache
            WHERE ticker = :t AND horizon_days = :h
        """), {"t": ticker.upper(), "h": int(horizon)}).fetchone()
    if not row:
        return None
    gen = row[4]
    if gen is None:
        return None
    if gen.tzinfo is None:
        gen = gen.replace(tzinfo=timezone.utc)
    if (datetime.now(tz=timezone.utc) - gen) > timedelta(hours=_CACHE_TTL_HOURS):
        return None
    try:
        quant = json.loads(row[0]) if row[0] else None
    except Exception:
        quant = None
    return {
        "quant": quant,
        "narrative": row[1],
        "confidence": row[2],
        "direction": row[3],
        "generated_at": gen.isoformat(),
    }


def _cache_set(ticker: str, horizon: int, *, quant: dict[str, Any] | None,
               narrative: str | None, confidence: str | None,
               direction: str | None) -> None:
    with session_scope() as s:
        exists = s.execute(text("""
            SELECT 1 FROM market_outlook_cache
            WHERE ticker = :t AND horizon_days = :h
        """), {"t": ticker.upper(), "h": int(horizon)}).fetchone()
        params = {
            "t": ticker.upper(), "h": int(horizon),
            "q": json.dumps(quant, default=str) if quant else None,
            "n": narrative, "c": confidence, "d": direction,
        }
        if exists:
            s.execute(text("""
                UPDATE market_outlook_cache
                SET quant_json = :q, llm_text = :n,
                    confidence = :c, direction = :d,
                    generated_at = UTC_TIMESTAMP()
                WHERE ticker = :t AND horizon_days = :h
            """), params)
        else:
            s.execute(text("""
                INSERT INTO market_outlook_cache
                    (ticker, horizon_days, quant_json, llm_text,
                     confidence, direction)
                VALUES (:t, :h, :q, :n, :c, :d)
            """), params)
        s.commit()


def _llm_narrative(ticker: str, quant_by_h: dict[int, dict[str, Any]],
                   context: dict[str, Any]) -> dict[str, Any]:
    """Single LLM call covering all three horizons. Returns per-horizon dict."""
    from agents.llm_factory import build_llm
    from langchain_core.messages import HumanMessage, SystemMessage
    llm = build_llm(purpose="chat", max_tokens=3500)
    if llm is None:
        return {}

    system = SystemMessage(content=(
        "You are an equity outlook writer for a wheel-strategy trader. "
        "You will be given quantitative projections (lognormal bands) plus "
        "context about the ticker. Write ONE concise paragraph (2-3 sentences) "
        "per horizon (30, 60, 90 days). Do NOT invent prices; reference the "
        "bands you're given. Be honest about uncertainty."
        "\nReturn ONLY raw JSON with no markdown fences. Schema:"
        ' {"horizons": {"30": {"text": "...", "direction": "bullish|neutral|bearish",'
        ' "confidence": "low|medium|high"}, "60": {...}, "90": {...}}}'
    ))
    payload = {
        "ticker": ticker,
        "context": context,
        "projections": quant_by_h,
    }
    user = HumanMessage(content=json.dumps(payload, default=str)[:6000])
    try:
        resp = llm.invoke([system, user])
        content = resp.content if isinstance(resp.content, str) else "".join(
            getattr(c, "text", "") for c in resp.content
        )
        start = content.find("{")
        end = content.rfind("}")
        if start == -1 or end == -1:
            return {}
        parsed = json.loads(content[start: end + 1])
        return parsed.get("horizons", {}) or {}
    except Exception as e:
        logger.exception("market outlook LLM failed for %s: %s", ticker, e)
        return {}


def predict(project_id: str, ticker: str, *, force: bool = False) -> dict[str, Any]:
    """Return hybrid outlook for 30/60/90 days for one ticker. Cached 12h."""
    ticker = ticker.upper()

    if not force:
        cached = {str(h): _cache_get(ticker, h) for h in _HORIZONS}
        if all(cached.values()):
            # Cached path: derive spot from the most recent quant block.
            any_q = next((c["quant"] for c in cached.values()
                          if c and c.get("quant")), None)
            ctx = {"spot_price": any_q["spot"] if any_q else None,
                   "iv_rank": None, "wheel_history": None, "momentum": None}
            return {"ticker": ticker, "context": ctx,
                    "horizons": cached, "source": "cache"}

    project = ProjectsRepo.get(project_id)
    if project is None:
        return {"error": "project not found"}
    if (getattr(project, "broker_type", "alpaca") or "alpaca") != "alpaca":
        return {"error": ("Outlook predictions need Alpaca daily-bars. "
                          "Phase 2 adds ETrade + yfinance fallbacks.")}
    client = AlpacaClient(project)

    bars = _fetch_history(client, ticker, lookback_days=260)
    if not bars:
        return {"error": f"no price history for {ticker}"}

    quant_by_h: dict[int, dict[str, Any]] = {}
    for h in _HORIZONS:
        q = _quant_outlook(bars, h)
        if q:
            quant_by_h[h] = q

    # Context for LLM narrative
    try:
        iv = get_iv_rank(project_id, ticker)
    except Exception:
        iv = None
    wm = _wheel_metrics(project_id, ticker)
    mm = _momentum(bars)
    context = {
        "iv_rank": iv,
        "wheel_history": wm,
        "momentum": mm,
        "spot_price": round(float(bars[-1]["c"]), 2),
    }

    narratives = _llm_narrative(ticker, quant_by_h, context)

    horizons_out: dict[str, dict[str, Any]] = {}
    for h in _HORIZONS:
        narr = narratives.get(str(h), {}) if narratives else {}
        text_blob = narr.get("text") if isinstance(narr, dict) else None
        direction = narr.get("direction") if isinstance(narr, dict) else None
        confidence = narr.get("confidence") if isinstance(narr, dict) else None
        _cache_set(ticker, h,
                   quant=quant_by_h.get(h),
                   narrative=text_blob,
                   confidence=confidence,
                   direction=direction)
        horizons_out[str(h)] = {
            "quant": quant_by_h.get(h),
            "narrative": text_blob,
            "direction": direction,
            "confidence": confidence,
            "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        }

    return {
        "ticker": ticker,
        "context": context,
        "horizons": horizons_out,
        "source": "fresh",
    }
