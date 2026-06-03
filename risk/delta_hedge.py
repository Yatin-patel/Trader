"""Delta Neutralization / Hedging.

Calculates and executes delta-neutral hedges to reduce directional exposure.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from db.repositories import EventsRepo, ProjectsRepo
from db.settings_store import ProjectSettings
from execution import AlpacaClient

logger = logging.getLogger(__name__)


def calculate_hedge_requirements(project_id: str) -> dict[str, Any]:
    """Calculate what's needed to delta-neutralize the portfolio.

    Args:
        project_id: Trading project ID

    Returns:
        Dict with hedge requirements and recommendations
    """
    from risk.greeks_agg import aggregate_greeks

    project = ProjectsRepo.get(project_id)
    if project is None:
        return {"error": "Project not found"}

    try:
        client = AlpacaClient(project)
        account = client.get_account()
        greeks = aggregate_greeks(project_id)
    except Exception as e:
        return {"error": f"Failed to get portfolio data: {e}"}

    equity = float(account.get("equity", 0))
    delta = greeks.get("delta", 0)
    dollar_delta = greeks.get("dollar_delta", 0)

    # Get SPY price for hedge calculation
    spy_price = 500.0  # Default
    try:
        spy_snap = client.snapshots(["SPY"]).get("SPY")
        if spy_snap:
            spy_price = spy_snap.last_price
    except Exception:
        pass

    # Delta exposure as percentage
    delta_pct = (abs(dollar_delta) / equity * 100) if equity > 0 else 0

    # Determine if hedge is needed
    delta_threshold = 500  # Shares equivalent
    needs_hedge = abs(delta) > delta_threshold

    # Calculate hedge using SPY shares
    # Each SPY share = $1 delta, so we need -delta shares of SPY
    spy_shares_needed = -int(delta)  # Negative to offset

    # Alternative: hedge with SPY options (more capital efficient)
    spy_delta_per_atm_call = 0.50  # ATM call delta ≈ 0.50
    spy_contracts_needed = -int(delta / (spy_delta_per_atm_call * 100))

    # Build recommendations
    recommendations = []

    if abs(spy_shares_needed) > 0:
        recommendations.append({
            "type": "EQUITY_HEDGE",
            "symbol": "SPY",
            "action": "BUY" if spy_shares_needed > 0 else "SELL",
            "quantity": abs(spy_shares_needed),
            "estimated_value": round(abs(spy_shares_needed) * spy_price, 2),
            "resulting_delta": round(delta + spy_shares_needed, 2),
            "description": f"{'Buy' if spy_shares_needed > 0 else 'Sell'} {abs(spy_shares_needed)} SPY shares to neutralize delta",
        })

    if abs(spy_contracts_needed) > 0:
        recommendations.append({
            "type": "OPTIONS_HEDGE",
            "symbol": "SPY",
            "action": "BUY" if spy_contracts_needed > 0 else "SELL",
            "option_type": "CALL" if spy_contracts_needed > 0 else "PUT",
            "contracts": abs(spy_contracts_needed),
            "suggested_delta": 0.50,
            "description": f"{'Buy' if spy_contracts_needed > 0 else 'Sell'} {abs(spy_contracts_needed)} ATM SPY {'calls' if spy_contracts_needed > 0 else 'puts'}",
        })

    # Per-underlying breakdown
    underlying_deltas = _get_delta_by_underlying(project_id)

    return {
        "project_id": project_id,
        "current_delta": round(delta, 2),
        "current_dollar_delta": round(dollar_delta, 2),
        "delta_pct_of_equity": round(delta_pct, 2),
        "equity": round(equity, 2),
        "is_delta_neutral": abs(delta) < 50,
        "needs_hedge": needs_hedge,
        "hedge_recommendations": recommendations,
        "underlying_breakdown": underlying_deltas,
        "spy_price": spy_price,
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
    }


def _get_delta_by_underlying(project_id: str) -> list[dict[str, Any]]:
    """Get delta exposure broken down by underlying symbol."""
    project = ProjectsRepo.get(project_id)
    if project is None:
        return []

    try:
        client = AlpacaClient(project)
        positions = client.list_positions()
    except Exception:
        return []

    delta_by_symbol: dict[str, dict[str, Any]] = {}

    # Equity positions
    for p in positions:
        if p.get("asset_class") == "us_equity":
            symbol = p["symbol"]
            qty = float(p["qty"])
            price = float(p.get("current_price", 0))
            delta_by_symbol[symbol] = {
                "symbol": symbol,
                "delta": qty,
                "dollar_delta": qty * price,
                "position_type": "EQUITY",
            }

    # Options positions
    options_by_underlying: dict[str, list[dict]] = {}
    for p in positions:
        cls = p.get("asset_class", "")
        if cls != "us_equity":
            sym = p["symbol"]
            # Extract underlying from OCC symbol
            for i, ch in enumerate(sym):
                if ch.isdigit():
                    underlying = sym[:i].upper()
                    break
            else:
                underlying = sym
            options_by_underlying.setdefault(underlying, []).append(p)

    for underlying, opts in options_by_underlying.items():
        try:
            chain = client.option_chain_quotes(underlying)
            snap = client.snapshots([underlying]).get(underlying)
            underlying_price = snap.last_price if snap else 100
        except Exception:
            continue

        total_delta = 0.0
        for op in opts:
            sym = op["symbol"]
            qty = float(op["qty"])
            g = chain.get(sym, {})
            if g.get("delta") is not None:
                total_delta += float(g["delta"]) * qty * 100

        if underlying in delta_by_symbol:
            delta_by_symbol[underlying]["delta"] += total_delta
            delta_by_symbol[underlying]["dollar_delta"] += total_delta * underlying_price
            delta_by_symbol[underlying]["has_options"] = True
        else:
            delta_by_symbol[underlying] = {
                "symbol": underlying,
                "delta": total_delta,
                "dollar_delta": total_delta * underlying_price,
                "position_type": "OPTIONS",
            }

    # Sort by absolute delta
    result = list(delta_by_symbol.values())
    result.sort(key=lambda x: abs(x["delta"]), reverse=True)

    return result


def execute_delta_hedge(
    project_id: str,
    hedge_type: str = "equity",
    target_delta: float = 0,
    dry_run: bool = False
) -> dict[str, Any]:
    """Execute a delta hedge trade.

    Args:
        project_id: Trading project ID
        hedge_type: "equity" for SPY shares, "options" for SPY options
        target_delta: Target portfolio delta after hedge (0 = neutral)
        dry_run: If True, don't execute, just preview

    Returns:
        Execution result
    """
    requirements = calculate_hedge_requirements(project_id)

    if "error" in requirements:
        return requirements

    current_delta = requirements["current_delta"]
    delta_to_hedge = current_delta - target_delta

    if abs(delta_to_hedge) < 10:
        return {"message": "Delta already within acceptable range", "current_delta": current_delta}

    project = ProjectsRepo.get(project_id)
    if not project:
        return {"error": "Project not found"}

    client = AlpacaClient(project)
    spy_price = requirements["spy_price"]

    if hedge_type == "equity":
        # Use SPY shares
        shares = -int(delta_to_hedge)  # Negative delta = buy shares

        if dry_run:
            return {
                "dry_run": True,
                "action": "BUY" if shares > 0 else "SELL",
                "symbol": "SPY",
                "quantity": abs(shares),
                "estimated_value": round(abs(shares) * spy_price, 2),
                "current_delta": current_delta,
                "resulting_delta": round(current_delta + shares, 2),
            }

        try:
            order = client.submit_market_equity(
                symbol="SPY",
                qty=abs(shares),
                side="buy" if shares > 0 else "sell",
                time_in_force="day"
            )

            EventsRepo.log(project_id, "DeltaHedge", "EXECUTE", {
                "hedge_type": "equity",
                "symbol": "SPY",
                "shares": shares,
                "price": spy_price,
                "order": order,
            })

            return {
                "success": True,
                "action": "BUY" if shares > 0 else "SELL",
                "symbol": "SPY",
                "quantity": abs(shares),
                "order_id": order.get("id"),
                "previous_delta": current_delta,
                "new_delta": round(current_delta + shares, 2),
            }

        except Exception as e:
            return {"error": f"Hedge execution failed: {e}"}

    elif hedge_type == "options":
        # Use ATM SPY options
        atm_strike = round(spy_price / 5) * 5  # Round to $5

        # Calculate contracts needed (assuming delta 0.50 per contract)
        contracts = -int(delta_to_hedge / 50)
        option_type = "call" if contracts > 0 else "put"

        # Get ATM option
        try:
            chain = client.list_option_contracts(
                "SPY", option_type,
                min_dte=21, max_dte=45,
                min_strike=atm_strike - 5,
                max_strike=atm_strike + 5,
                limit=10
            )

            if not chain:
                return {"error": "No suitable SPY options found"}

            quotes = client.option_chain_quotes("SPY")

            # Find best ATM option
            best_option = None
            best_diff = float("inf")

            for c in chain:
                diff = abs(c["strike"] - spy_price)
                q = quotes.get(c["symbol"], {})
                if diff < best_diff and (q.get("bid", 0) or 0) > 0:
                    best_diff = diff
                    best_option = {**c, **q}

            if not best_option:
                return {"error": "No liquid ATM options available"}

            mid = ((best_option.get("bid", 0) or 0) + (best_option.get("ask", 0) or 0)) / 2

            if dry_run:
                return {
                    "dry_run": True,
                    "action": "BUY",
                    "symbol": best_option["symbol"],
                    "option_type": option_type,
                    "strike": best_option["strike"],
                    "contracts": abs(contracts),
                    "estimated_cost": round(mid * abs(contracts) * 100, 2),
                    "current_delta": current_delta,
                    "resulting_delta": round(current_delta + contracts * 50, 2),
                }

            order = client.submit_limit_option(
                best_option["symbol"],
                abs(contracts),
                "buy",
                mid,
                time_in_force="day"
            )

            EventsRepo.log(project_id, "DeltaHedge", "EXECUTE", {
                "hedge_type": "options",
                "symbol": best_option["symbol"],
                "contracts": contracts,
                "price": mid,
                "order": order,
            })

            return {
                "success": True,
                "action": "BUY",
                "symbol": best_option["symbol"],
                "option_type": option_type,
                "contracts": abs(contracts),
                "order_id": order.get("id"),
                "previous_delta": current_delta,
                "new_delta": round(current_delta + contracts * 50, 2),
            }

        except Exception as e:
            return {"error": f"Options hedge failed: {e}"}

    else:
        return {"error": f"Unknown hedge type: {hedge_type}"}


def auto_hedge_check(project_id: str) -> dict[str, Any]:
    """Check if automatic delta hedging is needed.

    Args:
        project_id: Trading project ID

    Returns:
        Dict with hedge status and any actions taken
    """
    requirements = calculate_hedge_requirements(project_id)

    if "error" in requirements:
        return requirements

    delta = requirements["current_delta"]
    delta_pct = requirements["delta_pct_of_equity"]

    # Thresholds for auto-hedge
    DELTA_THRESHOLD = 1000  # Shares equivalent
    DELTA_PCT_THRESHOLD = 50  # Percentage of equity

    should_hedge = (
        abs(delta) > DELTA_THRESHOLD or
        abs(delta_pct) > DELTA_PCT_THRESHOLD
    )

    result = {
        "current_delta": delta,
        "delta_pct": delta_pct,
        "threshold_exceeded": should_hedge,
        "delta_threshold": DELTA_THRESHOLD,
        "delta_pct_threshold": DELTA_PCT_THRESHOLD,
        "checked_at": datetime.now(tz=timezone.utc).isoformat(),
    }

    if should_hedge:
        result["recommendation"] = requirements.get("hedge_recommendations", [])[:1]
        result["message"] = f"Delta exposure ({delta:.0f} / {delta_pct:.1f}% of equity) exceeds thresholds"

        EventsRepo.log(project_id, "DeltaHedge", "ALERT", {
            "delta": delta,
            "delta_pct": delta_pct,
            "message": result["message"],
        })
    else:
        result["message"] = "Delta exposure within acceptable limits"

    return result
