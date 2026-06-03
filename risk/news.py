"""News headlines + sentiment scoring for the trading agents.

Source priority (best → worst):
  1. Alpaca News API — real-time, free with your Alpaca account, aggregates
     Benzinga + others. The right primary source for an Alpaca-backed app.
  2. yfinance — fallback. Free but rate-limited / brittle.

Scoring priority (fast → expensive):
  1. VADER — pure-Python lexicon-based. ~100 µs per headline, no API call.
     Captures most cases (positive/negative tone). The MIN of all headline
     compound scores is used (worst headline drives the decision — paranoia
     is the right bias for blocking trades).
  2. LLM — only invoked when VADER is unavailable or as a backup. Slower
     (~1 s) and costs money per call; the legacy LLM scorer is kept for
     compatibility but is no longer the primary path.

The 24-hour window: only headlines published within the last
``news_skip_window_hours`` (default 24) count toward the blocking decision.
Older news has had time to bleed into the price already.

Cache: per-ticker, 4-hour TTL. Re-fetching during a single cycle is wasteful;
re-fetching every 4 hours keeps news fresh enough to catch new headlines
between the wheel's daily cadence.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text

from db.connection import session_scope

logger = logging.getLogger(__name__)

_TTL_HOURS = 4
_MAX_HEADLINES = 15
# How recently must a headline be published to be considered.
_DEFAULT_WINDOW_HOURS = 24


# ---------------------------------------------------------------------------
# VADER sentiment (lazy import — module is small but adds startup time)
# ---------------------------------------------------------------------------
_vader = None


def _get_vader():
    global _vader
    if _vader is None:
        try:
            from vaderSentiment.vaderSentiment import (
                SentimentIntensityAnalyzer,
            )
            _vader = SentimentIntensityAnalyzer()
        except Exception:
            logger.warning("vaderSentiment not available; falling back to LLM scoring")
            _vader = False  # sentinel: tried and failed
    return _vader or None


def _score_vader(headlines: list[str]) -> tuple[float | None, str]:
    """Return (worst compound score, rationale). Worst-headline-wins logic
    because for a blocking decision we want to be conservative."""
    v = _get_vader()
    if v is None or not headlines:
        return (None, "")
    scores: list[tuple[float, str]] = []
    for h in headlines:
        try:
            s = v.polarity_scores(h)
            scores.append((float(s.get("compound", 0.0)), h))
        except Exception:
            continue
    if not scores:
        return (None, "")
    scores.sort(key=lambda x: x[0])    # ascending: worst first
    worst_score, worst_headline = scores[0]
    return (worst_score,
            f"worst headline ({worst_score:+.2f}): {worst_headline[:140]}")


# ---------------------------------------------------------------------------
# Cache layer (same shape as before)
# ---------------------------------------------------------------------------
def _is_stale(fetched_at) -> bool:
    if fetched_at is None:
        return True
    if fetched_at.tzinfo is None:
        fetched_at = fetched_at.replace(tzinfo=timezone.utc)
    return (datetime.now(tz=timezone.utc) - fetched_at) > timedelta(hours=_TTL_HOURS)


def _cache_get(ticker: str) -> dict[str, Any] | None:
    with session_scope() as s:
        row = s.execute(text("""
            SELECT sentiment_score, headlines, rationale, fetched_at
            FROM news_sentiment_cache WHERE ticker = :t
        """), {"t": ticker.upper()}).fetchone()
    if not row:
        return None
    return {
        "score": float(row[0]) if row[0] is not None else None,
        "headlines": json.loads(row[1]) if row[1] else [],
        "rationale": row[2],
        "fetched_at": row[3],
    }


def _cache_set(ticker: str, score: float | None,
               headlines: list[str], rationale: str | None) -> None:
    with session_scope() as s:
        exists = s.execute(text(
            "SELECT 1 FROM news_sentiment_cache WHERE ticker = :t"
        ), {"t": ticker.upper()}).fetchone()
        payload = json.dumps(headlines[:_MAX_HEADLINES])
        params = {"t": ticker.upper(), "s": score, "h": payload,
                  "r": (rationale or "")[:1000]}
        if exists:
            s.execute(text("""
                UPDATE news_sentiment_cache
                SET sentiment_score = :s, headlines = :h, rationale = :r,
                    fetched_at = UTC_TIMESTAMP()
                WHERE ticker = :t
            """), params)
        else:
            s.execute(text("""
                INSERT INTO news_sentiment_cache
                    (ticker, sentiment_score, headlines, rationale)
                VALUES (:t, :s, :h, :r)
            """), params)
        s.commit()


# ---------------------------------------------------------------------------
# Headline sources
# ---------------------------------------------------------------------------
def _fetch_headlines_alpaca(ticker: str,
                            window_hours: int = _DEFAULT_WINDOW_HOURS
                            ) -> list[str]:
    """Use Alpaca's NewsClient. Requires APCA-API-KEY-ID + APCA-API-SECRET-KEY
    in the env, or AppSettings — but Alpaca's data API gates news off the
    keys-id-and-secret you already use for trading."""
    try:
        from alpaca.data.historical.news import NewsClient
        from alpaca.data.requests import NewsRequest
    except ImportError:
        return []
    # Use any active Alpaca project's keys — we don't need a specific tenant
    # for news, the keys just authenticate against Alpaca's data API.
    try:
        from db.repositories import ProjectsRepo
        for p in ProjectsRepo.list_all():
            bt = (getattr(p, "broker_type", "alpaca") or "alpaca")
            if bt != "alpaca" or not p.alpaca_api_key:
                continue
            client = NewsClient(api_key=p.alpaca_api_key,
                                secret_key=p.alpaca_secret_key)
            req = NewsRequest(
                symbols=[ticker.upper()],
                start=datetime.now(tz=timezone.utc) - timedelta(hours=window_hours),
                limit=_MAX_HEADLINES,
            )
            news = client.get_news(req).news or []
            headlines: list[str] = []
            for n in news[:_MAX_HEADLINES]:
                title = getattr(n, "headline", None) or ""
                if title:
                    headlines.append(title.strip())
            return headlines
    except Exception as e:
        logger.debug("alpaca news fetch failed for %s: %s", ticker, e)
    return []


def _fetch_headlines_yfinance(ticker: str) -> list[str]:
    """Legacy fallback. yfinance.news has been flaky in recent releases."""
    try:
        import yfinance as yf
    except ImportError:
        return []
    try:
        news = yf.Ticker(ticker).news or []
    except Exception:
        return []
    out: list[str] = []
    for item in news[:_MAX_HEADLINES]:
        title = (item.get("title") if isinstance(item, dict) else None) or ""
        if not title and isinstance(item, dict):
            content = item.get("content")
            if isinstance(content, dict):
                title = content.get("title") or ""
        title = title.strip()
        if title:
            out.append(title)
    return out


def _fetch_headlines(ticker: str,
                     window_hours: int = _DEFAULT_WINDOW_HOURS) -> list[str]:
    h = _fetch_headlines_alpaca(ticker, window_hours)
    if h:
        return h
    return _fetch_headlines_yfinance(ticker)


# ---------------------------------------------------------------------------
# LLM scoring (legacy backup — only used when VADER is unavailable)
# ---------------------------------------------------------------------------
def _score_with_llm(ticker: str,
                    headlines: list[str]) -> tuple[float | None, str]:
    if not headlines:
        return (0.0, "no headlines available")
    from agents.llm_factory import build_llm
    from langchain_core.messages import HumanMessage, SystemMessage
    llm = build_llm(purpose="chat", max_tokens=512)
    if llm is None:
        return (None, "no LLM configured")
    system = SystemMessage(content=(
        "You assess news sentiment for an options-trading bot. Read the "
        "headlines for the given ticker. Return a single JSON object: "
        '{"score": float in -1..+1, "rationale": "short reason"}.\n'
        "Score guidance:\n"
        "  -1.0 catastrophic (bankruptcy, fraud, criminal charges)\n"
        "  -0.5 strongly negative (guidance cut, lawsuit, major loss)\n"
        "   0.0 neutral / mixed / generic news\n"
        "  +0.5 strongly positive (beat earnings, major contract)\n"
        "  +1.0 transformative positive (acquisition premium, breakthrough)\n"
    ))
    user = HumanMessage(content=f"Ticker: {ticker}\nHeadlines:\n" + "\n".join(
        f"- {h}" for h in headlines))
    try:
        resp = llm.invoke([system, user])
        content = resp.content if isinstance(resp.content, str) else "".join(
            getattr(c, "text", "") for c in resp.content
        )
        start = content.find("{")
        end = content.rfind("}")
        if start == -1 or end == -1:
            return (0.0, "unparseable: " + content[:120])
        parsed = json.loads(content[start: end + 1])
        score = float(parsed.get("score", 0))
        score = max(-1.0, min(1.0, score))
        return (score, str(parsed.get("rationale", ""))[:300])
    except Exception as e:
        logger.debug("news LLM call failed for %s: %s", ticker, e)
        return (None, f"llm error: {e}")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def get_news_sentiment(ticker: str,
                       window_hours: int = _DEFAULT_WINDOW_HOURS
                       ) -> dict[str, Any]:
    cached = _cache_get(ticker)
    if cached and not _is_stale(cached["fetched_at"]):
        return cached

    headlines = _fetch_headlines(ticker, window_hours)
    # Prefer VADER (fast, free, deterministic) over LLM.
    score, rationale = _score_vader(headlines)
    if score is None:
        score, rationale = _score_with_llm(ticker, headlines)
    _cache_set(ticker, score, headlines, rationale)
    return {"score": score, "headlines": headlines, "rationale": rationale}


def passes_news_filter(ticker: str, min_score: float,
                       enabled: bool) -> tuple[bool, str]:
    """Return (allow, reason_if_blocked).

    With VADER, ``min_score`` is interpreted as the worst-headline compound
    score that's still allowed (e.g. -0.50 = block when any headline is
    moderately-or-worse negative).
    """
    if not enabled:
        return (True, "")
    s = get_news_sentiment(ticker)
    score = s.get("score")
    if score is None:
        return (True, "")   # unknown → allow (fail open)
    if score >= min_score:
        return (True, "")
    return (False,
            f"news sentiment {score:+.2f} below threshold {min_score:+.2f}: "
            + (s.get("rationale", "") or ""))
