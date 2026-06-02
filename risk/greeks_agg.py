"""Portfolio greeks aggregation.

Sums delta / gamma / theta / vega across all open option positions by
fetching current greeks per underlying. Equity positions contribute delta
equal to share count (delta=1 per share long, -1 short).
"""
from __future__ import annotations

import logging
from typing import Any

from db.repositories import ProjectsRepo
from execution import AlpacaClient

logger = logging.getLogger(__name__)

# Process-level cache: greeks rarely change minute-to-minute and the
# aggregation is the slowest call in the whole app (1 chain per underlying).
_GREEKS_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_GREEKS_TTL = 30.0


def aggregate_greeks(project_id: str) -> dict[str, Any]:
    import time as _time
    now = _time.monotonic()
    cached = _GREEKS_CACHE.get(project_id)
    if cached and (now - cached[0]) < _GREEKS_TTL:
        return cached[1]
    result = _aggregate_greeks_uncached(project_id)
    _GREEKS_CACHE[project_id] = (now, result)
    return result


def _aggregate_greeks_uncached(project_id: str) -> dict[str, Any]:
    project = ProjectsRepo.get(project_id)
    if project is None:
        return _empty_greeks()
    try:
        client = AlpacaClient(project)
        positions = client.list_positions()
    except Exception as e:
        logger.warning("greeks agg: alpaca fetch failed for %s: %s", project_id, e)
        return _empty_greeks(error=str(e))

    delta = gamma = theta = vega = 0.0
    contract_count = 0
    share_count = 0

    # Group options by underlying to amortize chain queries.
    options_by_underlying: dict[str, list[dict[str, Any]]] = {}
    for p in positions:
        cls = p.get("asset_class") or ""
        if cls == "us_equity":
            qty = float(p["qty"])
            delta += qty                       # equity contributes ±1 delta per share
            share_count += int(qty)
            continue
        # Options have symbols like NVDA250606P00210000 — first 1-5 chars are ticker.
        sym = p["symbol"]
        underlying = _extract_underlying(sym)
        options_by_underlying.setdefault(underlying, []).append(p)

    for underlying, opts in options_by_underlying.items():
        try:
            chain = client.option_chain_quotes(underlying)
        except Exception as e:
            logger.warning("chain fetch failed for %s: %s", underlying, e)
            continue
        for op in opts:
            sym = op["symbol"]
            qty = float(op["qty"])  # negative for short
            g = chain.get(sym) or {}
            if g.get("delta") is not None:
                delta += float(g["delta"]) * qty * 100
            if g.get("gamma") is not None:
                gamma += float(g["gamma"]) * qty * 100
            if g.get("theta") is not None:
                theta += float(g["theta"]) * qty * 100
            if g.get("vega") is not None:
                vega += float(g["vega"]) * qty * 100
            contract_count += int(abs(qty))

    return {
        "delta": round(delta, 2),
        "gamma": round(gamma, 4),
        "theta": round(theta, 2),
        "vega":  round(vega, 2),
        "contract_count": contract_count,
        "share_count": share_count,
    }


def _empty_greeks(error: str | None = None) -> dict[str, Any]:
    out = {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0,
           "contract_count": 0, "share_count": 0}
    if error:
        out["error"] = error
    return out


def _extract_underlying(symbol: str) -> str:
    """OCC option symbols start with the underlying ticker (1-6 chars) then
    a 6-digit YYMMDD, then C/P, then 8-digit strike (e.g. NVDA250606P00210000)."""
    # Find the first digit; everything before it is the underlying.
    for i, ch in enumerate(symbol):
        if ch.isdigit():
            return symbol[:i].upper()
    return symbol.upper()
