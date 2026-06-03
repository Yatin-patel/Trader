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
        account = client.get_account()
    except Exception as e:
        logger.warning("greeks agg: alpaca fetch failed for %s: %s", project_id, e)
        return _empty_greeks(error=str(e))

    delta = gamma = theta = vega = 0.0
    dollar_delta = 0.0  # Delta exposure in dollars
    daily_theta_dollars = 0.0  # Daily theta decay in dollars
    gamma_exposure = 0.0  # Dollar gamma (1% move impact)
    contract_count = 0
    share_count = 0
    equity_value = float(account.get("equity", 0))

    # Track underlying prices for dollar calculations
    underlying_prices: dict[str, float] = {}

    # Group options by underlying to amortize chain queries.
    options_by_underlying: dict[str, list[dict[str, Any]]] = {}
    equity_positions: list[dict[str, Any]] = []

    for p in positions:
        cls = p.get("asset_class") or ""
        if cls == "us_equity":
            qty = float(p["qty"])
            price = float(p.get("current_price") or p.get("avg_entry_price") or 0)
            delta += qty                       # equity contributes ±1 delta per share
            dollar_delta += qty * price        # Dollar value of equity positions
            share_count += int(qty)
            underlying_prices[p["symbol"]] = price
            equity_positions.append(p)
            continue
        # Options have symbols like NVDA250606P00210000 — first 1-5 chars are ticker.
        sym = p["symbol"]
        underlying = _extract_underlying(sym)
        options_by_underlying.setdefault(underlying, []).append(p)

    for underlying, opts in options_by_underlying.items():
        try:
            chain = client.option_chain_quotes(underlying)
            # Get underlying price
            if underlying not in underlying_prices:
                snap = client.snapshots([underlying]).get(underlying)
                if snap:
                    underlying_prices[underlying] = snap.last_price
        except Exception as e:
            logger.warning("chain fetch failed for %s: %s", underlying, e)
            continue

        underlying_price = underlying_prices.get(underlying, 0)

        for op in opts:
            sym = op["symbol"]
            qty = float(op["qty"])  # negative for short
            g = chain.get(sym) or {}

            # Delta
            if g.get("delta") is not None:
                opt_delta = float(g["delta"]) * qty * 100
                delta += opt_delta
                # Dollar delta = delta * underlying price
                dollar_delta += opt_delta * underlying_price

            # Gamma
            if g.get("gamma") is not None:
                opt_gamma = float(g["gamma"]) * qty * 100
                gamma += opt_gamma
                # Gamma exposure: impact of 1% move in underlying
                # Formula: 0.5 * gamma * (underlying_price * 0.01)^2 * 100
                if underlying_price > 0:
                    move_1pct = underlying_price * 0.01
                    gamma_exposure += 0.5 * opt_gamma * (move_1pct ** 2)

            # Theta
            if g.get("theta") is not None:
                opt_theta = float(g["theta"]) * qty * 100
                theta += opt_theta
                # Daily theta in dollars (theta is already per-contract per day)
                daily_theta_dollars += opt_theta

            # Vega
            if g.get("vega") is not None:
                vega += float(g["vega"]) * qty * 100

            contract_count += int(abs(qty))

    # Calculate beta-weighted delta (SPY equivalent)
    # Simplified: assume portfolio beta ≈ 1.0
    beta_weighted_delta = delta  # Would need individual betas for precision

    # Portfolio delta as percentage of equity
    delta_pct = (dollar_delta / equity_value * 100) if equity_value > 0 else 0

    return {
        # Standard Greeks
        "delta": round(delta, 2),
        "gamma": round(gamma, 4),
        "theta": round(theta, 2),
        "vega": round(vega, 2),
        # Enhanced metrics
        "dollar_delta": round(dollar_delta, 2),
        "delta_pct_of_equity": round(delta_pct, 2),
        "daily_theta_dollars": round(daily_theta_dollars, 2),
        "gamma_exposure": round(gamma_exposure, 2),
        "beta_weighted_delta": round(beta_weighted_delta, 2),
        # Position counts
        "contract_count": contract_count,
        "share_count": share_count,
        "equity_value": round(equity_value, 2),
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
