"""VIX-Based Hedging and Alert System.

Monitors VIX levels and generates hedging recommendations based on
market volatility conditions.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from db.repositories import EventsRepo, ProjectsRepo
from db.settings_store import ProjectSettings

logger = logging.getLogger(__name__)

# VIX thresholds
VIX_LOW = 15.0      # Low volatility - favor selling premium
VIX_MODERATE = 20.0 # Normal conditions
VIX_ELEVATED = 25.0 # Elevated - consider hedging
VIX_HIGH = 30.0     # High - aggressive hedging
VIX_EXTREME = 40.0  # Extreme - defensive mode


def get_vix_level() -> dict[str, Any]:
    """Get current VIX level from market data.

    Returns:
        Dict with VIX level, classification, and percentile
    """
    try:
        import yfinance as yf

        vix = yf.Ticker("^VIX")
        hist = vix.history(period="1y")

        if hist.empty:
            return {"error": "VIX data unavailable"}

        current = float(hist["Close"].iloc[-1])
        high_52w = float(hist["Close"].max())
        low_52w = float(hist["Close"].min())

        # Calculate percentile
        sorted_values = hist["Close"].sort_values()
        percentile = (sorted_values < current).sum() / len(sorted_values) * 100

        # Classify
        if current < VIX_LOW:
            classification = "LOW"
            description = "Low volatility - favorable for premium selling"
        elif current < VIX_MODERATE:
            classification = "MODERATE"
            description = "Normal volatility conditions"
        elif current < VIX_ELEVATED:
            classification = "ELEVATED"
            description = "Elevated volatility - consider wider strikes"
        elif current < VIX_HIGH:
            classification = "HIGH"
            description = "High volatility - reduce position sizes"
        elif current < VIX_EXTREME:
            classification = "VERY_HIGH"
            description = "Very high volatility - defensive positioning"
        else:
            classification = "EXTREME"
            description = "Extreme volatility - consider protective hedges"

        # Calculate recent change
        if len(hist) >= 5:
            prev_5d = float(hist["Close"].iloc[-5])
            change_5d = ((current - prev_5d) / prev_5d) * 100
        else:
            change_5d = 0

        return {
            "vix": round(current, 2),
            "classification": classification,
            "description": description,
            "percentile": round(percentile, 1),
            "52w_high": round(high_52w, 2),
            "52w_low": round(low_52w, 2),
            "change_5d_pct": round(change_5d, 2),
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
        }

    except Exception as e:
        logger.warning("Failed to fetch VIX: %s", e)
        return {"error": str(e)}


def evaluate_vix_alerts(project_id: str) -> list[dict[str, Any]]:
    """Evaluate VIX-based alerts and recommendations for a project.

    Args:
        project_id: Trading project ID

    Returns:
        List of alerts and recommendations
    """
    vix_data = get_vix_level()

    if "error" in vix_data:
        return [{"type": "ERROR", "message": f"VIX data unavailable: {vix_data['error']}"}]

    vix = vix_data["vix"]
    classification = vix_data["classification"]
    alerts = []

    # Get current portfolio greeks
    try:
        from risk.greeks_agg import aggregate_greeks
        greeks = aggregate_greeks(project_id)
    except Exception:
        greeks = {"delta": 0, "gamma": 0, "vega": 0}

    # Generate alerts based on VIX level
    if classification == "EXTREME":
        alerts.append({
            "severity": "critical",
            "type": "VIX_EXTREME",
            "vix": vix,
            "message": f"VIX at {vix:.1f} (EXTREME) - Consider hedging with protective puts or reducing positions",
            "recommendations": [
                "Buy SPY puts for downside protection",
                "Reduce overall position sizes by 50%",
                "Close high-delta short positions",
                "Avoid new short premium positions",
            ],
        })
    elif classification == "VERY_HIGH":
        alerts.append({
            "severity": "error",
            "type": "VIX_VERY_HIGH",
            "vix": vix,
            "message": f"VIX at {vix:.1f} (VERY HIGH) - Defensive positioning recommended",
            "recommendations": [
                "Consider protective hedges",
                "Widen strike selection on new positions",
                "Reduce max position sizes by 25%",
            ],
        })
    elif classification == "HIGH":
        alerts.append({
            "severity": "warning",
            "type": "VIX_HIGH",
            "vix": vix,
            "message": f"VIX at {vix:.1f} (HIGH) - Increased caution advised",
            "recommendations": [
                "Monitor positions more frequently",
                "Consider smaller position sizes",
                "Be prepared for larger moves",
            ],
        })
    elif classification == "ELEVATED":
        alerts.append({
            "severity": "info",
            "type": "VIX_ELEVATED",
            "vix": vix,
            "message": f"VIX at {vix:.1f} (ELEVATED) - Slightly increased volatility",
            "recommendations": [
                "Good premium selling environment",
                "Consider wider delta range",
            ],
        })
    elif classification == "LOW":
        alerts.append({
            "severity": "info",
            "type": "VIX_LOW",
            "vix": vix,
            "message": f"VIX at {vix:.1f} (LOW) - Premium may be cheaper",
            "recommendations": [
                "Premiums may be lower than usual",
                "Consider longer DTE for better premium",
                "Watch for sudden vol spikes",
            ],
        })

    # Vega exposure warning
    vega = greeks.get("vega", 0)
    if abs(vega) > 1000 and classification in ("ELEVATED", "HIGH", "VERY_HIGH", "EXTREME"):
        alerts.append({
            "severity": "warning",
            "type": "VEGA_EXPOSURE",
            "vega": vega,
            "vix": vix,
            "message": f"High vega exposure ({vega:.0f}) during elevated VIX ({vix:.1f})",
            "recommendations": [
                "Consider reducing vega exposure",
                "Close or roll ITM short options",
            ],
        })

    # VIX spike detection
    change_5d = vix_data.get("change_5d_pct", 0)
    if change_5d > 30:
        alerts.append({
            "severity": "warning",
            "type": "VIX_SPIKE",
            "vix": vix,
            "change_5d_pct": change_5d,
            "message": f"VIX spiked {change_5d:.0f}% in 5 days - market stress detected",
            "recommendations": [
                "Review all open positions",
                "Tighten stop-losses",
                "Consider defensive trades",
            ],
        })

    # Log significant alerts
    for alert in alerts:
        if alert.get("severity") in ("error", "critical"):
            EventsRepo.log(project_id, "VIXMonitor", "ALERT", {
                "type": alert["type"],
                "vix": vix,
                "message": alert["message"],
            })

    return alerts


def get_hedge_recommendations(project_id: str) -> dict[str, Any]:
    """Get specific hedging trade recommendations based on VIX and portfolio.

    Args:
        project_id: Trading project ID

    Returns:
        Dict with hedge recommendations
    """
    vix_data = get_vix_level()

    if "error" in vix_data:
        return {"error": vix_data["error"]}

    vix = vix_data["vix"]

    # Get portfolio value
    project = ProjectsRepo.get(project_id)
    if not project:
        return {"error": "Project not found"}

    try:
        from execution import AlpacaClient
        client = AlpacaClient(project)
        account = client.get_account()
        equity = float(account.get("equity", 0))
    except Exception as e:
        return {"error": f"Account fetch failed: {e}"}

    recommendations = []

    # Calculate hedge sizing
    # Rule of thumb: hedge 10-30% of portfolio value depending on VIX
    if vix >= VIX_EXTREME:
        hedge_pct = 0.30
        urgency = "immediate"
    elif vix >= VIX_HIGH:
        hedge_pct = 0.20
        urgency = "high"
    elif vix >= VIX_ELEVATED:
        hedge_pct = 0.10
        urgency = "moderate"
    else:
        hedge_pct = 0.05
        urgency = "low"

    hedge_value = equity * hedge_pct

    # SPY put recommendation
    spy_price = 500.0  # Approximate - would fetch real price
    try:
        spy_snap = client.snapshots(["SPY"]).get("SPY")
        if spy_snap:
            spy_price = spy_snap.last_price
    except Exception:
        pass

    # OTM put strike (5% below current)
    put_strike = round(spy_price * 0.95 / 5) * 5  # Round to $5

    # Calculate contracts needed
    notional_per_contract = put_strike * 100
    contracts = max(1, int(hedge_value / notional_per_contract))

    recommendations.append({
        "type": "PROTECTIVE_PUT",
        "symbol": "SPY",
        "action": "BUY",
        "strike": put_strike,
        "option_type": "PUT",
        "suggested_dte": 30,
        "contracts": contracts,
        "estimated_cost": round(hedge_value * 0.02, 2),  # ~2% rough estimate
        "rationale": f"Protect {hedge_pct*100:.0f}% of portfolio (${hedge_value:,.0f}) against downside",
    })

    # VIX call recommendation for vol hedge
    if vix < VIX_ELEVATED:
        recommendations.append({
            "type": "VIX_CALL",
            "symbol": "VIX",
            "action": "BUY",
            "strike": round(vix * 1.5),  # 50% OTM
            "option_type": "CALL",
            "suggested_dte": 30,
            "contracts": max(1, int(equity / 100000)),
            "rationale": "Profit from vol spike if market sells off",
        })

    return {
        "project_id": project_id,
        "vix": vix,
        "vix_classification": vix_data["classification"],
        "portfolio_equity": round(equity, 2),
        "hedge_pct_recommended": hedge_pct,
        "hedge_value": round(hedge_value, 2),
        "urgency": urgency,
        "recommendations": recommendations,
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
    }
