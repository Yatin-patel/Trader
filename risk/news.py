"""News sentiment fetch + LLM scoring.

Uses yfinance to pull recent headlines, then the configured LLM (via
llm_factory) to assign a sentiment score in [-1, +1]. Cached for 4 hours
per ticker.
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
_MAX_HEADLINES = 8


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


def _fetch_headlines(ticker: str) -> list[str]:
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
            # yfinance >= 0.2.50 nests under "content"
            content = item.get("content")
            if isinstance(content, dict):
                title = content.get("title") or ""
        title = title.strip()
        if title:
            out.append(title)
    return out


def _score_with_llm(ticker: str, headlines: list[str]) -> tuple[float | None, str]:
    if not headlines:
        return (0.0, "no headlines available")
    # Lazy import to avoid circular dependency (agents → strategist → risk.news)
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
        # Best-effort JSON parse
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


def get_news_sentiment(ticker: str) -> dict[str, Any]:
    cached = _cache_get(ticker)
    if cached and not _is_stale(cached["fetched_at"]):
        return cached
    headlines = _fetch_headlines(ticker)
    score, rationale = _score_with_llm(ticker, headlines)
    _cache_set(ticker, score, headlines, rationale)
    return {"score": score, "headlines": headlines, "rationale": rationale}


def passes_news_filter(ticker: str, min_score: float, enabled: bool) -> tuple[bool, str]:
    """Return (allow, reason_if_blocked)."""
    if not enabled:
        return (True, "")
    s = get_news_sentiment(ticker)
    score = s.get("score")
    if score is None:
        return (True, "")   # unknown → allow
    if score >= min_score:
        return (True, "")
    return (False, f"news sentiment {score:+.2f} below threshold {min_score:+.2f}: "
                   + (s.get("rationale", "") or ""))
