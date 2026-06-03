"""Intraday technical signals for day trading (RSI, MACD).

Provides real-time technical indicators for 0DTE and intraday trading decisions.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)


def calculate_rsi(prices: list[float], period: int = 14) -> float | None:
    """Calculate Relative Strength Index.

    Args:
        prices: List of closing prices (oldest to newest)
        period: RSI lookback period (default 14)

    Returns:
        RSI value (0-100) or None if insufficient data
    """
    if len(prices) < period + 1:
        return None

    prices_arr = np.array(prices, dtype=float)
    deltas = np.diff(prices_arr)

    gains = np.where(deltas > 0, deltas, 0)
    losses = np.where(deltas < 0, -deltas, 0)

    # Use EMA for smoothing (Wilder's smoothing)
    alpha = 1.0 / period

    avg_gain = gains[:period].mean()
    avg_loss = losses[:period].mean()

    for i in range(period, len(gains)):
        avg_gain = alpha * gains[i] + (1 - alpha) * avg_gain
        avg_loss = alpha * losses[i] + (1 - alpha) * avg_loss

    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss
    rsi = 100.0 - (100.0 / (1.0 + rs))

    return round(rsi, 2)


def calculate_macd(
    prices: list[float],
    fast: int = 12,
    slow: int = 26,
    signal: int = 9
) -> dict[str, float | None]:
    """Calculate MACD (Moving Average Convergence Divergence).

    Args:
        prices: List of closing prices (oldest to newest)
        fast: Fast EMA period (default 12)
        slow: Slow EMA period (default 26)
        signal: Signal line EMA period (default 9)

    Returns:
        Dict with macd_line, signal_line, histogram values
    """
    if len(prices) < slow + signal:
        return {"macd_line": None, "signal_line": None, "histogram": None}

    prices_arr = np.array(prices, dtype=float)

    # Calculate EMAs
    def ema(data: np.ndarray, period: int) -> np.ndarray:
        alpha = 2.0 / (period + 1)
        result = np.zeros_like(data)
        result[0] = data[0]
        for i in range(1, len(data)):
            result[i] = alpha * data[i] + (1 - alpha) * result[i - 1]
        return result

    fast_ema = ema(prices_arr, fast)
    slow_ema = ema(prices_arr, slow)

    macd_line = fast_ema - slow_ema
    signal_line = ema(macd_line, signal)
    histogram = macd_line - signal_line

    return {
        "macd_line": round(float(macd_line[-1]), 4),
        "signal_line": round(float(signal_line[-1]), 4),
        "histogram": round(float(histogram[-1]), 4),
    }


def calculate_vwap(prices: list[float], volumes: list[int]) -> float | None:
    """Calculate Volume-Weighted Average Price.

    Args:
        prices: List of prices
        volumes: List of volumes corresponding to prices

    Returns:
        VWAP value or None if insufficient data
    """
    if len(prices) < 1 or len(prices) != len(volumes):
        return None

    total_volume = sum(volumes)
    if total_volume == 0:
        return None

    vwap = sum(p * v for p, v in zip(prices, volumes)) / total_volume
    return round(vwap, 4)


def generate_intraday_signal(
    ticker: str,
    prices: list[float],
    volumes: list[int] | None = None,
    rsi_oversold: int = 30,
    rsi_overbought: int = 70
) -> dict[str, Any]:
    """Generate intraday trading signal based on technical indicators.

    Args:
        ticker: Stock symbol
        prices: List of intraday prices (oldest to newest, 5-min bars recommended)
        volumes: Optional volume data for VWAP
        rsi_oversold: RSI level for buy signals
        rsi_overbought: RSI level for sell signals

    Returns:
        Signal dict with action, strength, indicators, and rationale
    """
    result = {
        "ticker": ticker.upper(),
        "timestamp": datetime.now(tz=timezone.utc).isoformat(),
        "signal": "NEUTRAL",
        "strength": 0.0,
        "indicators": {},
        "rationale": [],
    }

    # Calculate RSI
    rsi = calculate_rsi(prices)
    result["indicators"]["rsi"] = rsi

    # Calculate MACD
    macd = calculate_macd(prices)
    result["indicators"]["macd"] = macd

    # Calculate VWAP if volume data available
    if volumes:
        vwap = calculate_vwap(prices, volumes)
        result["indicators"]["vwap"] = vwap
    else:
        vwap = None

    # Generate signal based on indicators
    score = 0.0
    rationale = []

    if rsi is not None:
        if rsi < rsi_oversold:
            score += 1.0
            rationale.append(f"RSI {rsi:.1f} is oversold (< {rsi_oversold})")
        elif rsi > rsi_overbought:
            score -= 1.0
            rationale.append(f"RSI {rsi:.1f} is overbought (> {rsi_overbought})")
        else:
            rationale.append(f"RSI {rsi:.1f} is neutral")

    if macd["histogram"] is not None:
        if macd["histogram"] > 0 and macd["macd_line"] > macd["signal_line"]:
            score += 0.5
            rationale.append("MACD histogram positive, bullish crossover")
        elif macd["histogram"] < 0 and macd["macd_line"] < macd["signal_line"]:
            score -= 0.5
            rationale.append("MACD histogram negative, bearish crossover")

    # VWAP comparison
    if vwap is not None and len(prices) > 0:
        current_price = prices[-1]
        if current_price > vwap * 1.01:
            score += 0.3
            rationale.append(f"Price ${current_price:.2f} above VWAP ${vwap:.2f}")
        elif current_price < vwap * 0.99:
            score -= 0.3
            rationale.append(f"Price ${current_price:.2f} below VWAP ${vwap:.2f}")

    # Determine signal
    if score >= 1.0:
        result["signal"] = "BUY"
        result["strength"] = min(score / 2.0, 1.0)
    elif score <= -1.0:
        result["signal"] = "SELL"
        result["strength"] = min(abs(score) / 2.0, 1.0)
    else:
        result["signal"] = "NEUTRAL"
        result["strength"] = 0.0

    result["rationale"] = rationale
    result["score"] = round(score, 2)

    return result


def scan_intraday_opportunities(
    project_id: str,
    tickers: list[str] | None = None
) -> list[dict[str, Any]]:
    """Scan multiple tickers for intraday trading opportunities.

    Args:
        project_id: Trading project ID
        tickers: List of tickers to scan (uses watchlist if None)

    Returns:
        List of signals sorted by strength
    """
    from db.repositories import ProjectsRepo
    from db.settings_store import ProjectSettings
    from execution import AlpacaClient

    project = ProjectsRepo.get(project_id)
    if project is None:
        return []

    # Get tickers from watchlist if not provided
    if tickers is None:
        watchlist = ProjectSettings.get(project_id, "watchlist", "")
        if watchlist:
            tickers = [t.strip().upper() for t in watchlist.split(",") if t.strip()]
        else:
            tickers = ["SPY", "QQQ", "AAPL", "MSFT", "NVDA", "AMD", "TSLA"]

    client = AlpacaClient(project)
    rsi_oversold = int(ProjectSettings.get(project_id, "intraday_rsi_oversold", 30))
    rsi_overbought = int(ProjectSettings.get(project_id, "intraday_rsi_overbought", 70))

    signals = []
    for ticker in tickers[:20]:  # Limit to 20 tickers per scan
        try:
            # Get recent intraday bars (last 2 days of 5-min bars)
            bars = client.daily_bars(ticker, lookback_days=2)
            if len(bars) < 14:
                continue

            prices = [b["c"] for b in bars]
            volumes = [b["v"] for b in bars]

            signal = generate_intraday_signal(
                ticker, prices, volumes,
                rsi_oversold=rsi_oversold,
                rsi_overbought=rsi_overbought
            )

            if signal["signal"] != "NEUTRAL":
                signals.append(signal)

        except Exception as e:
            logger.warning("Failed to scan %s: %s", ticker, e)
            continue

    # Sort by signal strength (strongest first)
    signals.sort(key=lambda s: s["strength"], reverse=True)

    return signals
