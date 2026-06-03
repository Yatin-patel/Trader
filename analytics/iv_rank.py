"""Realized-volatility rank as an IV-rank proxy.

Alpaca doesn't expose historical option IV directly, so we use 30-day
realized volatility from daily bars as a stable, free proxy. We rank
the current 30-day window against 1 year of rolling 30-day windows.

Cache lives in iv_rank_cache with 12h TTL.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text

from db.connection import session_scope
from db.repositories import ProjectsRepo
from execution import AlpacaClient

logger = logging.getLogger(__name__)

_TTL_HOURS = 12
_WINDOW = 30   # trading days
_LOOKBACK = 252  # ~1 yr trading days


def _is_stale(fetched_at) -> bool:
    if fetched_at is None:
        return True
    if fetched_at.tzinfo is None:
        fetched_at = fetched_at.replace(tzinfo=timezone.utc)
    return (datetime.now(tz=timezone.utc) - fetched_at) > timedelta(hours=_TTL_HOURS)


def _cache_get(ticker: str) -> dict[str, Any] | None:
    with session_scope() as s:
        row = s.execute(text("""
            SELECT iv_rank, realized_vol, sample_days, fetched_at
            FROM iv_rank_cache WHERE ticker = :t
        """), {"t": ticker.upper()}).fetchone()
    if not row:
        return None
    return {"iv_rank": float(row[0]) if row[0] is not None else None,
            "realized_vol": float(row[1]) if row[1] is not None else None,
            "sample_days": int(row[2]) if row[2] is not None else None,
            "fetched_at": row[3]}


def _cache_set(ticker: str, iv_rank: float | None,
               realized_vol: float | None, sample_days: int | None) -> None:
    with session_scope() as s:
        exists = s.execute(text(
            "SELECT 1 FROM iv_rank_cache WHERE ticker = :t"
        ), {"t": ticker.upper()}).fetchone()
        if exists:
            s.execute(text("""
                UPDATE iv_rank_cache
                SET iv_rank = :r, realized_vol = :v,
                    sample_days = :n, fetched_at = UTC_TIMESTAMP()
                WHERE ticker = :t
            """), {"t": ticker.upper(), "r": iv_rank,
                   "v": realized_vol, "n": sample_days})
        else:
            s.execute(text("""
                INSERT INTO iv_rank_cache
                    (ticker, iv_rank, realized_vol, sample_days)
                VALUES (:t, :r, :v, :n)
            """), {"t": ticker.upper(), "r": iv_rank,
                   "v": realized_vol, "n": sample_days})
        s.commit()


def _stdev(values: list[float]) -> float:
    n = len(values)
    if n < 2:
        return 0.0
    mean = sum(values) / n
    var = sum((v - mean) ** 2 for v in values) / (n - 1)
    return var ** 0.5


def _rolling_realized_vol(closes: list[float], window: int) -> list[float]:
    if len(closes) < window + 1:
        return []
    import math
    log_returns: list[float] = []
    for i in range(1, len(closes)):
        if closes[i - 1] <= 0 or closes[i] <= 0:
            log_returns.append(0.0)
            continue
        log_returns.append(math.log(closes[i] / closes[i - 1]))
    vols: list[float] = []
    for i in range(window, len(log_returns) + 1):
        sample = log_returns[i - window: i]
        vols.append(_stdev(sample) * (252 ** 0.5))   # annualized
    return vols


def get_iv_rank(project_id: str, ticker: str) -> float | None:
    """Return iv_rank ∈ [0,1] or None if insufficient data."""
    cached = _cache_get(ticker)
    if cached and not _is_stale(cached["fetched_at"]):
        return cached.get("iv_rank")

    project = ProjectsRepo.get(project_id)
    if project is None:
        return None
    try:
        client = AlpacaClient(project)
        bars = client.daily_bars(ticker.upper(), lookback_days=_LOOKBACK + _WINDOW)
    except Exception as e:
        logger.debug("iv_rank bars fetch failed %s: %s", ticker, e)
        _cache_set(ticker, None, None, 0)
        return None
    if not bars or len(bars) < _WINDOW + 1:
        _cache_set(ticker, None, None, len(bars) if bars else 0)
        return None
    closes = [float(b["c"]) for b in bars]
    vols = _rolling_realized_vol(closes, _WINDOW)
    if not vols:
        _cache_set(ticker, None, None, 0)
        return None
    current_vol = vols[-1]
    sorted_vols = sorted(vols)
    # iv_rank = position of current_vol in 1-yr distribution.
    below = sum(1 for v in sorted_vols if v < current_vol)
    iv_rank = below / len(sorted_vols)
    _cache_set(ticker, iv_rank, current_vol, len(vols))
    return iv_rank


def passes_iv_filter(project_id: str, ticker: str, min_rank: float) -> bool:
    if min_rank <= 0:
        return True   # filter disabled
    rank = get_iv_rank(project_id, ticker)
    if rank is None:
        # Unknown → allow rather than block (don't punish missing data).
        return True
    return rank >= min_rank
