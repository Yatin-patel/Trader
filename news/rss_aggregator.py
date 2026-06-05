"""RSS news aggregator — free sources, no rate limits, ticker-tagged.

Pulls headlines from a curated list of FREE general-market RSS feeds
(NOT per-ticker, which Yahoo rate-limits) and extracts cashtag /
ticker mentions from each headline. Results live in an in-memory
cache refreshed by a scheduler tick (every 30 min) so each lookup is
O(1) for the watchlist refresher.

Sources picked for:
  * Free (no API key, no paywall)
  * No documented rate limit on RSS endpoints
  * High signal density on actively-traded tickers

If a feed goes down or rate-limits us we silently fall back — the
news layer is a BIAS on watchlist scoring, never a hard filter, so
network flakiness doesn't break the wheel.
"""
from __future__ import annotations

import logging
import re
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any

import requests

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Free RSS sources. Mix of broad-market wires + retail-sentiment.
# Each one is keyed by a label so debug output shows what produced
# what. The fetcher tolerates failure per-feed so a single dead URL
# doesn't break the aggregation.
# ---------------------------------------------------------------------------
RSS_SOURCES: list[tuple[str, str]] = [
    # MarketWatch top stories — Dow Jones, broad market headlines.
    ("marketwatch",
     "https://feeds.content.dowjones.io/public/rss/mw_topstories"),
    # CNBC top news + markets.
    ("cnbc_markets",
     "https://search.cnbc.com/rs/search/combinedcms/view.xml"
     "?partnerId=wrss01&id=10000664"),
    ("cnbc_top",
     "https://search.cnbc.com/rs/search/combinedcms/view.xml"
     "?partnerId=wrss01&id=100003114"),
    # Yahoo Finance general (NOT per-ticker — that gets 429'd).
    ("yahoo_finance",
     "https://feeds.finance.yahoo.com/rss/2.0/headline"),
    # Seeking Alpha market currents — heavy on cashtags ($AAPL etc).
    ("seeking_alpha",
     "https://seekingalpha.com/market_currents.xml"),
    # Investing.com stock-market news.
    ("investing_com",
     "https://www.investing.com/rss/news_25.rss"),
    # Reddit r/wallstreetbets — retail sentiment + meme momentum.
    ("wsb",
     "https://www.reddit.com/r/wallstreetbets/.rss"),
    # Reddit r/stocks — broader retail discussion.
    ("reddit_stocks",
     "https://www.reddit.com/r/stocks/.rss"),
]


# Tickers that are also common English words — exclude unless they
# appear with a cashtag ($) prefix. Without this filter the regex
# matches "THE", "USA", "FOR", "ALL", "GO" as tickers.
_COMMON_WORD_FALSE_POSITIVES = {
    "THE", "AND", "FOR", "ARE", "BUT", "NOT", "YOU", "ALL", "CAN",
    "HER", "WAS", "ONE", "OUR", "OUT", "DAY", "GET", "HAS", "HIM",
    "HIS", "HOW", "MAN", "NEW", "NOW", "OLD", "SEE", "TWO", "WAY",
    "WHO", "BOY", "DID", "ITS", "LET", "PUT", "SAY", "SHE", "TOO",
    "USE", "DAD", "MOM", "FED", "USA", "CEO", "CFO", "GDP", "ETF",
    "API", "URL", "USD", "EUR", "GBP", "FAQ", "AI", "IT", "IS",
    "BE", "OR", "TO", "AT", "ON", "IN", "OF", "BY", "GO", "DO",
    "ME", "NO", "UP", "WE", "AS", "AN", "AM",
}


@dataclass
class NewsMentions:
    """Per-ticker news mention count + sample headlines from the last
    refresh window. ``count`` is total mentions across all feeds (a
    headline that mentions $AAPL twice still counts once)."""
    ticker: str
    count: int = 0
    sources: set[str] = field(default_factory=set)
    headlines: list[str] = field(default_factory=list)


# Cache: ticker -> NewsMentions. Refreshed by refresh_news_cache().
_CACHE: dict[str, NewsMentions] = {}
_CACHE_TIMESTAMP: float = 0.0
_CACHE_LOCK = threading.Lock()
_CACHE_TTL_SECONDS = 30 * 60  # 30 minutes


def _ticker_regex(valid_tickers: set[str] | None = None) -> re.Pattern:
    """Compile a regex that matches BOTH cashtag-prefixed tickers
    ($AAPL) and bare uppercase 2-5 letter words. Bare words get
    filtered later against valid_tickers + the common-word stoplist."""
    return re.compile(r"\$?([A-Z]{1,5})\b")


def _extract_tickers(text: str,
                     valid_tickers: set[str] | None = None) -> set[str]:
    """Pull every plausible ticker out of a headline string.

    A 2-5 letter all-caps word is treated as a candidate. To be
    counted it must either:
      * appear with a cashtag prefix ($AAPL), OR
      * appear in valid_tickers (caller supplies the universe)
    Common English words are always rejected.
    """
    if not text:
        return set()
    found: set[str] = set()
    cashtag_matches = re.findall(r"\$([A-Z]{1,5})\b", text)
    for sym in cashtag_matches:
        if sym in _COMMON_WORD_FALSE_POSITIVES:
            continue
        found.add(sym)
    bare_matches = re.findall(r"\b([A-Z]{2,5})\b", text)
    for sym in bare_matches:
        if sym in _COMMON_WORD_FALSE_POSITIVES:
            continue
        if valid_tickers and sym not in valid_tickers:
            continue
        found.add(sym)
    return found


def _parse_feed(xml_bytes: bytes) -> list[str]:
    """Pull (title + description) text out of an RSS XML payload.

    Handles RSS 2.0, Atom, and Reddit's RSS-flavored Atom by trying
    each known element-path. Robust to namespaces — we strip them
    rather than try to track every variant."""
    try:
        root = ET.fromstring(xml_bytes)
    except Exception as e:
        logger.warning("RSS parse failed: %s", e)
        return []

    items: list[str] = []
    # Strip namespaces from tags so .findall is simpler.
    for elem in root.iter():
        if "}" in elem.tag:
            elem.tag = elem.tag.split("}", 1)[1]

    # RSS 2.0 → <channel><item><title>
    for item in root.iter("item"):
        title = item.findtext("title") or ""
        desc = item.findtext("description") or ""
        items.append(f"{title} {desc}")
    # Atom → <entry><title>
    for entry in root.iter("entry"):
        title = entry.findtext("title") or ""
        summary = entry.findtext("summary") or entry.findtext(
            "content") or ""
        items.append(f"{title} {summary}")

    return items


def _fetch_feed(label: str, url: str,
                timeout: float = 15.0) -> list[str]:
    """Fetch one feed. Returns list of headline-strings or [] on
    error. Logs at INFO level for ops visibility."""
    try:
        # User-Agent is REQUIRED for several feeds (Reddit + some
        # CDN-fronted RSS) — a generic UA string is fine since RSS
        # is explicitly public.
        r = requests.get(
            url,
            headers={"User-Agent": "Mozilla/5.0 (Trader news aggregator)"},
            timeout=timeout,
        )
    except Exception as e:
        logger.info("RSS fetch failed %s (%s): %s", label, url, e)
        return []
    if r.status_code != 200:
        logger.info("RSS %s HTTP %s", label, r.status_code)
        return []
    return _parse_feed(r.content)


def refresh_news_cache(
    valid_tickers: set[str] | None = None,
    *,
    force: bool = False,
) -> dict[str, Any]:
    """Refresh the news mention cache from all configured RSS feeds.
    Returns a summary dict the scheduler can log.

    ``valid_tickers``: only count bare-word matches that appear in this
    set. The watchlist refresher passes its curated liquid-optionable
    pool so we don't surface false-positive tickers like "GDP".

    ``force``: bypass the TTL check and refresh now.
    """
    global _CACHE_TIMESTAMP
    now = time.monotonic()
    with _CACHE_LOCK:
        if not force and (now - _CACHE_TIMESTAMP) < _CACHE_TTL_SECONDS:
            return {
                "status": "cached",
                "age_seconds": round(now - _CACHE_TIMESTAMP, 1),
                "tickers": len(_CACHE),
            }

    new_cache: dict[str, NewsMentions] = {}
    per_source_count: dict[str, int] = {}

    for label, url in RSS_SOURCES:
        headlines = _fetch_feed(label, url)
        if not headlines:
            per_source_count[label] = 0
            continue
        per_source_count[label] = len(headlines)
        for text in headlines:
            tickers = _extract_tickers(text, valid_tickers)
            for sym in tickers:
                entry = new_cache.get(sym)
                if entry is None:
                    entry = NewsMentions(ticker=sym)
                    new_cache[sym] = entry
                entry.count += 1
                entry.sources.add(label)
                if len(entry.headlines) < 5:
                    short = text.strip().replace("\n", " ")[:160]
                    if short and short not in entry.headlines:
                        entry.headlines.append(short)

    with _CACHE_LOCK:
        _CACHE.clear()
        _CACHE.update(new_cache)
        _CACHE_TIMESTAMP = now

    return {
        "status": "refreshed",
        "tickers_found": len(new_cache),
        "per_source": per_source_count,
        "top_5": sorted(
            new_cache.values(), key=lambda m: -m.count)[:5],
    }


def get_news_mentions(
    ticker: str,
    valid_tickers: set[str] | None = None,
) -> NewsMentions | None:
    """Look up news mentions for a ticker. Auto-refreshes the cache
    if it's older than the TTL. Returns None if the ticker has no
    mentions in the current window."""
    with _CACHE_LOCK:
        age = time.monotonic() - _CACHE_TIMESTAMP
    if age >= _CACHE_TTL_SECONDS:
        refresh_news_cache(valid_tickers)
    with _CACHE_LOCK:
        return _CACHE.get(ticker.upper())


def get_top_news_tickers(
    n: int = 20,
    valid_tickers: set[str] | None = None,
) -> list[NewsMentions]:
    """Top N tickers by news mention count in the current window.
    Triggers a refresh if the cache is stale."""
    with _CACHE_LOCK:
        age = time.monotonic() - _CACHE_TIMESTAMP
    if age >= _CACHE_TTL_SECONDS:
        refresh_news_cache(valid_tickers)
    with _CACHE_LOCK:
        return sorted(_CACHE.values(), key=lambda m: -m.count)[:n]
