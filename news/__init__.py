"""News-aware market signal layer.

Aggregates free-tier RSS feeds (MarketWatch, CNBC, Yahoo Finance,
Seeking Alpha market currents, Reddit WSB) and extracts ticker
mentions so the dynamic watchlist can bias toward names with active
news flow.

Distinct from ``risk.news`` which is per-ticker LLM-sentiment scoring
used by the Strategist to filter individual trades. This module
operates at the universe-selection layer.
"""
from .rss_aggregator import (
    NewsMentions, refresh_news_cache, get_news_mentions,
)

__all__ = ["NewsMentions", "refresh_news_cache", "get_news_mentions"]
