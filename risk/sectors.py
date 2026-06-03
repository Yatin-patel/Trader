"""Sector classification + per-sector collateral accounting.

Used by the Guardrail to enforce ``max_concentration_per_sector``. Sectors
follow GICS-ish buckets (collapsed where the wheel watchlist is sparse).

The map is intentionally static and small: covers the ~150 most liquid
US tickers we expect to see in a wheel watchlist. Unknown tickers return
``None`` and the Guardrail simply skips the sector check for them — that
fails open, which is the safer default.

If you need wider coverage, swap to a live data source (yfinance, FMP,
etc.) and cache results in the ``earnings_cache`` style.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import text

from db.connection import session_scope
from db.repositories import WheelRepo


# GICS-inspired bucketing. Kept manually because liquid US options trade
# on a fairly stable universe and the cost of a wrong sector is bounded
# (one missed rejection). Add tickers as your watchlist evolves.
_SECTORS: dict[str, str] = {
    # ---- Communication Services ----
    "GOOGL": "Communication", "GOOG": "Communication",
    "META": "Communication", "NFLX": "Communication",
    "DIS": "Communication", "T": "Communication", "VZ": "Communication",
    "TMUS": "Communication", "SNAP": "Communication",
    "PINS": "Communication", "RBLX": "Communication",
    "DKNG": "Communication", "EA": "Communication", "TTWO": "Communication",

    # ---- Consumer Discretionary ----
    "AMZN": "Cons Disc", "TSLA": "Cons Disc", "HD": "Cons Disc",
    "MCD": "Cons Disc", "NKE": "Cons Disc", "SBUX": "Cons Disc",
    "LOW": "Cons Disc", "TGT": "Cons Disc", "BKNG": "Cons Disc",
    "F": "Cons Disc", "GM": "Cons Disc", "RIVN": "Cons Disc",
    "NIO": "Cons Disc", "ABNB": "Cons Disc", "LULU": "Cons Disc",
    "ETSY": "Cons Disc", "EBAY": "Cons Disc", "CHWY": "Cons Disc",
    "U": "Cons Disc",

    # ---- Consumer Staples ----
    "WMT": "Cons Staples", "COST": "Cons Staples", "PG": "Cons Staples",
    "KO": "Cons Staples", "PEP": "Cons Staples", "CL": "Cons Staples",
    "MO": "Cons Staples", "PM": "Cons Staples", "MDLZ": "Cons Staples",

    # ---- Energy ----
    "XOM": "Energy", "CVX": "Energy", "COP": "Energy", "OXY": "Energy",
    "SLB": "Energy", "EOG": "Energy", "PSX": "Energy",
    "MRO": "Energy", "DVN": "Energy",

    # ---- Financials ----
    "JPM": "Financials", "BAC": "Financials", "WFC": "Financials",
    "GS": "Financials", "MS": "Financials", "C": "Financials",
    "BLK": "Financials", "SCHW": "Financials", "AXP": "Financials",
    "V": "Financials", "MA": "Financials", "BRK.B": "Financials",
    "PYPL": "Financials", "SQ": "Financials", "XYZ": "Financials",
    "SOFI": "Financials", "HOOD": "Financials", "COIN": "Financials",
    "NU": "Financials",

    # ---- Health Care ----
    "JNJ": "Health Care", "UNH": "Health Care", "PFE": "Health Care",
    "MRK": "Health Care", "LLY": "Health Care", "ABBV": "Health Care",
    "TMO": "Health Care", "ABT": "Health Care", "BMY": "Health Care",
    "AMGN": "Health Care", "GILD": "Health Care", "CVS": "Health Care",
    "MRNA": "Health Care",

    # ---- Industrials ----
    "BA": "Industrials", "CAT": "Industrials", "DE": "Industrials",
    "GE": "Industrials", "HON": "Industrials", "RTX": "Industrials",
    "LMT": "Industrials", "MMM": "Industrials", "UPS": "Industrials",
    "FDX": "Industrials", "AAL": "Industrials", "DAL": "Industrials",
    "UAL": "Industrials",

    # ---- Information Technology ----
    "AAPL": "Tech", "MSFT": "Tech", "NVDA": "Tech", "AMD": "Tech",
    "INTC": "Tech", "AVGO": "Tech", "ORCL": "Tech", "CRM": "Tech",
    "ADBE": "Tech", "CSCO": "Tech", "QCOM": "Tech", "TXN": "Tech",
    "IBM": "Tech", "PLTR": "Tech", "SHOP": "Tech", "DDOG": "Tech",
    "SNOW": "Tech", "MU": "Tech", "MRVL": "Tech", "ANET": "Tech",
    "PANW": "Tech", "CRWD": "Tech", "ZS": "Tech", "NET": "Tech",
    "MARA": "Tech", "RIOT": "Tech",  # crypto-miners-as-tech-proxies

    # ---- Materials ----
    "LIN": "Materials", "APD": "Materials", "SHW": "Materials",
    "FCX": "Materials", "NEM": "Materials",

    # ---- Real Estate ----
    "AMT": "Real Estate", "PLD": "Real Estate", "CCI": "Real Estate",
    "EQIX": "Real Estate", "PSA": "Real Estate", "O": "Real Estate",

    # ---- Utilities ----
    "NEE": "Utilities", "DUK": "Utilities", "SO": "Utilities",
    "AEP": "Utilities", "EXC": "Utilities",

    # ---- Index ETFs (treat as "Broad Market") ----
    "SPY": "Broad Mkt", "QQQ": "Broad Mkt", "IWM": "Broad Mkt",
    "DIA": "Broad Mkt", "VOO": "Broad Mkt", "VTI": "Broad Mkt",
}


def sector_of(ticker: str) -> str | None:
    """Return the sector for ``ticker`` or ``None`` if unmapped."""
    if not ticker:
        return None
    return _SECTORS.get(ticker.upper())


def sector_used_collateral(project_id: str) -> dict[str, float]:
    """Sum the collateral (strike * 100 * qty for CSPs) of currently-open
    contracts grouped by sector. Unmapped tickers are dropped (skips the
    sector check, fails open).

    The Guardrail uses this as the baseline before applying proposed
    trades.
    """
    out: dict[str, float] = {}
    for c in WheelRepo.list_open(project_id):
        if c.get("strategy_phase") != "CASH_SECURED_PUT":
            continue
        sec = sector_of(c.get("ticker", ""))
        if not sec:
            continue
        qty = int(c.get("quantity") or 1)
        strike = float(c.get("strike_price") or 0)
        out[sec] = out.get(sec, 0.0) + strike * 100.0 * qty
    return out
