"""Slice closed-trade P&L by strategy/market dimensions for the attribution UI."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from db.analytics_repos import ClosedContractsRepo


def _bucket(value: float, edges: list[float]) -> str | None:
    if value is None:
        return None
    for i in range(len(edges) - 1):
        if edges[i] <= value < edges[i + 1]:
            return f"{edges[i]:.2f}-{edges[i + 1]:.2f}"
    if value >= edges[-1]:
        return f"≥ {edges[-1]:.2f}"
    if value < edges[0]:
        return f"< {edges[0]:.2f}"
    return None


def _int_bucket(value: int, edges: list[int]) -> str | None:
    if value is None:
        return None
    for i in range(len(edges) - 1):
        if edges[i] <= value < edges[i + 1]:
            return f"{edges[i]}-{edges[i + 1] - 1}"
    if value >= edges[-1]:
        return f"≥ {edges[-1]}"
    return None


# Dimension extractors: trade dict -> label string (or None to skip)

def _dim_delta(t: dict[str, Any]) -> str | None:
    d = t.get("delta_at_entry")
    if d is None:
        return None
    return _bucket(abs(float(d)), [0.0, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40, 0.45, 0.50, 1.01])


def _dim_dte(t: dict[str, Any]) -> str | None:
    d = t.get("dte_at_entry")
    if d is None:
        return None
    return _int_bucket(int(d), [0, 4, 8, 15, 31, 46, 200])


def _dim_phase(t: dict[str, Any]) -> str:
    return t.get("strategy_phase") or "UNKNOWN"


def _dim_ticker(t: dict[str, Any]) -> str:
    return t.get("ticker") or "UNKNOWN"


def _dim_dow(t: dict[str, Any]) -> str | None:
    opened_at = t.get("opened_at")
    if not opened_at:
        return None
    if isinstance(opened_at, str):
        try:
            opened_at = datetime.fromisoformat(opened_at)
        except Exception:
            return None
    return ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][opened_at.weekday()]


def _dim_underlying_price(t: dict[str, Any]) -> str | None:
    p = t.get("underlying_at_entry")
    if p is None:
        return None
    return _bucket(float(p), [0, 25, 50, 100, 250, 500, 100000])


_DIMENSIONS: dict[str, Callable[[dict[str, Any]], str | None]] = {
    "delta": _dim_delta,
    "dte": _dim_dte,
    "phase": _dim_phase,
    "ticker": _dim_ticker,
    "day_of_week": _dim_dow,
    "underlying_price": _dim_underlying_price,
}


def attribution_by_dimension(project_id: str, dimension: str,
                             *, since_days: int | None = None,
                             min_trades: int = 1) -> list[dict[str, Any]]:
    extractor = _DIMENSIONS.get(dimension)
    if extractor is None:
        return []

    since = (datetime.now(tz=timezone.utc) - timedelta(days=since_days)) if since_days else None
    trades = ClosedContractsRepo.list(project_id, since=since, limit=20000)

    buckets: dict[str, list[float]] = {}
    pnl_buckets: dict[str, list[float]] = {}
    for t in trades:
        label = extractor(t)
        if label is None:
            continue
        buckets.setdefault(label, []).append(t["realized_pnl"])
        pnl_buckets.setdefault(label, []).append(t["realized_pnl"])

    rows = []
    for label, values in buckets.items():
        if len(values) < min_trades:
            continue
        wins = [v for v in values if v > 0]
        total = sum(values)
        rows.append({
            "label": label,
            "trade_count": len(values),
            "wins": len(wins),
            "win_rate": round(len(wins) / len(values), 4),
            "avg_pnl": round(total / len(values), 2),
            "total_pnl": round(total, 2),
            "expected_value": round(total / len(values), 2),
            "confidence": "high" if len(values) >= 30 else "medium" if len(values) >= 10 else "low",
        })
    rows.sort(key=lambda r: r["total_pnl"], reverse=True)
    return rows
