"""Earnings calendar checker.

Uses `yfinance` (no API key) to fetch the next earnings date per ticker,
cached in `dbo.earnings_cache`. Cache TTL is 24 hours.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

from db.risk_repos import EarningsCacheRepo

logger = logging.getLogger(__name__)

_TTL_HOURS = 24


def _is_stale(fetched_at) -> bool:
    if fetched_at is None:
        return True
    if fetched_at.tzinfo is None:
        fetched_at = fetched_at.replace(tzinfo=timezone.utc)
    return (datetime.now(tz=timezone.utc) - fetched_at) > timedelta(hours=_TTL_HOURS)


def get_next_earnings(ticker: str) -> date | None:
    """Return the next earnings date for `ticker`, or None if unknown."""
    cached = EarningsCacheRepo.get(ticker)
    if cached and not _is_stale(cached["fetched_at"]):
        return cached["next_earnings_date"]

    # Fetch from yfinance (lazy import — optional dependency)
    try:
        import yfinance as yf
    except ImportError:
        logger.warning("yfinance not installed; earnings checks disabled")
        EarningsCacheRepo.upsert(ticker, None)
        return None

    try:
        ticker_obj = yf.Ticker(ticker)
        cal = ticker_obj.calendar
    except Exception as e:
        logger.debug("yfinance fetch failed for %s: %s", ticker, e)
        EarningsCacheRepo.upsert(ticker, None)
        return None

    next_date: date | None = None
    try:
        # yfinance >= 0.2 returns a dict; older returns a DataFrame.
        if isinstance(cal, dict):
            ed = cal.get("Earnings Date")
            if ed:
                if isinstance(ed, list) and ed:
                    val = ed[0]
                else:
                    val = ed
                if isinstance(val, datetime):
                    next_date = val.date()
                elif isinstance(val, date):
                    next_date = val
        elif cal is not None and hasattr(cal, "iloc"):
            # DataFrame shape
            try:
                val = cal.iloc[0, 0]
                if hasattr(val, "date"):
                    next_date = val.date()
            except Exception:
                pass
    except Exception as e:
        logger.debug("earnings parse failed for %s: %s", ticker, e)

    EarningsCacheRepo.upsert(ticker, next_date)
    return next_date


def upcoming_earnings_within(ticker: str, dte: int) -> bool:
    """Return True iff `ticker` has an earnings event within `dte` days."""
    if dte <= 0:
        return False
    e_date = get_next_earnings(ticker)
    if not e_date:
        return False
    today = date.today()
    if e_date < today:
        return False
    return (e_date - today).days <= int(dte)
