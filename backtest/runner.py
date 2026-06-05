"""Backtest MVP — replays a date range against the current strategy params.

This is a coarse simulator. For every trading day in the window it:
  1. Pulls actual daily bars per ticker (universe).
  2. Filters with the Scanner rules (volume, %-change, price band).
  3. For each candidate, computes a synthetic CSP at the configured delta
     midpoint with a fixed DTE = `csp_min_dte` + 7 days.
  4. Assumes the put is held to expiry. P&L = full premium if underlying
     closed ≥ strike at expiry, else (premium - (strike - close)).

Coarse on purpose — no Greeks, no LLM, no fills. The point is rapid
parameter exploration, not millisecond accuracy.
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text

from db.connection import insert_returning_id, session_scope
from db.repositories import ProjectsRepo
from db.settings_store import ProjectSettings
from execution import get_broker

logger = logging.getLogger(__name__)


def _bars_for(client: AlpacaClient, ticker: str, days: int) -> list[dict[str, Any]]:
    try:
        return client.daily_bars(ticker, lookback_days=days)
    except Exception:
        return []


def _synth_put_premium(strike: float, underlying: float, dte: int,
                       vol_proxy: float) -> float:
    """Very rough premium estimate using Black-Scholes-ish shortcut:
    premium ≈ vol_proxy * sqrt(dte/365) * underlying * 0.4 for ATM,
    scaled by moneyness. Good enough for sweep comparisons."""
    import math
    if underlying <= 0:
        return 0.0
    atm_premium = vol_proxy * math.sqrt(max(1, dte) / 365.0) * underlying * 0.4
    moneyness = max(0.01, strike / underlying)
    # OTM puts (strike < underlying) get less premium
    return atm_premium * max(0.05, min(2.0, moneyness * 0.95))


def _realized_vol_proxy(closes: list[float]) -> float:
    import math
    if len(closes) < 5:
        return 0.30
    rets = []
    for i in range(1, len(closes)):
        if closes[i - 1] > 0:
            rets.append(math.log(closes[i] / closes[i - 1]))
    if not rets:
        return 0.30
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / max(1, len(rets) - 1)
    return (var ** 0.5) * (252 ** 0.5)


def run_backtest(project_id: str, *, from_date: date, to_date: date,
                 universe: list[str] | None = None,
                 name: str = "ad-hoc") -> dict[str, Any]:
    """Run a synchronous backtest. Returns the summary dict."""
    project = ProjectsRepo.get(project_id)
    if project is None:
        return {"error": "project not found"}
    if (getattr(project, "broker_type", "alpaca") or "alpaca") != "alpaca":
        return {"error": ("Backtest currently relies on Alpaca historical "
                          "data. Phase 2 will add ETrade + yfinance "
                          "fallbacks. Switch to an Alpaca project to backtest.")}
    client = get_broker(project)

    delta_lo = float(ProjectSettings.get(project_id, "csp_delta_min") or 0.15)
    delta_hi = float(ProjectSettings.get(project_id, "csp_delta_max") or 0.30)
    min_pct = float(ProjectSettings.get(project_id, "scanner_min_pct_change") or 1.5)
    vol_thr = int(ProjectSettings.get(project_id, "volume_threshold") or 2_000_000)
    min_p = float(ProjectSettings.get(project_id, "scanner_min_price") or 5)
    max_p = float(ProjectSettings.get(project_id, "scanner_max_price") or 500)
    dte = int(ProjectSettings.get(project_id, "csp_min_dte", default=7) or 7) + 7

    if universe is None:
        ulist = ProjectSettings.get(project_id, "watchlist") or ""
        if isinstance(ulist, str):
            universe = [t.strip().upper() for t in ulist.split(",") if t.strip()]
        else:
            universe = list(ulist)
    universe = universe or ["AAPL", "MSFT", "NVDA", "AMD", "META", "GOOGL"]

    days_window = (to_date - from_date).days + 60
    # Insert RUNNING row
    params_snapshot = {
        "from_date": str(from_date), "to_date": str(to_date),
        "universe": universe, "csp_delta_min": delta_lo,
        "csp_delta_max": delta_hi, "dte": dte,
    }
    with session_scope() as s:
        run_id = insert_returning_id(s, """
            INSERT INTO backtest_runs
                (project_id, name, from_date, to_date, status, params)
            VALUES (:p, :n, :fd, :td, 'RUNNING', :pa)
        """, {"p": project_id, "n": name, "fd": from_date,
              "td": to_date, "pa": json.dumps(params_snapshot)})
        s.commit()

    # Fetch bars for every ticker in one pass
    bars_by_ticker: dict[str, list[dict[str, Any]]] = {}
    for t in universe:
        b = _bars_for(client, t, days_window)
        if b:
            bars_by_ticker[t] = b

    # Index bars by date for quick lookup
    indexed: dict[str, dict[date, dict[str, Any]]] = {}
    for t, bars in bars_by_ticker.items():
        idx = {}
        for b in bars:
            ts = b.get("t")
            d = ts.date() if hasattr(ts, "date") else None
            if d:
                idx[d] = b
        indexed[t] = idx

    # Walk dates and simulate
    trades: list[dict[str, Any]] = []
    cur = from_date
    while cur <= to_date:
        for t, idx in indexed.items():
            today_bar = idx.get(cur)
            if today_bar is None:
                continue
            # Need previous-day close
            prev_close = None
            search = cur - timedelta(days=1)
            for _ in range(7):
                pb = idx.get(search)
                if pb:
                    prev_close = pb["c"]
                    break
                search -= timedelta(days=1)
            if prev_close is None or prev_close <= 0:
                continue
            close = float(today_bar["c"])
            if not (min_p <= close <= max_p):
                continue
            if int(today_bar["v"]) < vol_thr:
                continue
            pct = (close - prev_close) / prev_close * 100
            if abs(pct) < min_pct:
                continue

            # Build synthetic CSP: target delta midpoint via strike below price
            target_delta = (delta_lo + delta_hi) / 2
            strike = close * (1 - target_delta * 0.5)   # rough mapping
            # Vol proxy from prior 21 days
            close_series = []
            scan_d = cur - timedelta(days=1)
            attempts = 0
            while len(close_series) < 21 and attempts < 60:
                b = idx.get(scan_d)
                if b:
                    close_series.append(float(b["c"]))
                scan_d -= timedelta(days=1)
                attempts += 1
            close_series.reverse()
            vol = _realized_vol_proxy(close_series)

            premium = _synth_put_premium(strike, close, dte, vol)

            # P&L at expiry: find bar at cur + dte trading days (approx)
            expiry = cur + timedelta(days=int(dte * 1.4))
            # Walk forward to find a real bar near expiry
            seek = expiry
            expiry_bar = None
            attempts = 0
            while attempts < 10:
                eb = idx.get(seek)
                if eb:
                    expiry_bar = eb
                    break
                seek -= timedelta(days=1)
                attempts += 1
            if expiry_bar is None:
                continue
            exp_close = float(expiry_bar["c"])
            if exp_close >= strike:
                pnl = premium * 100   # full premium kept
                outcome = "EXPIRED"
            else:
                pnl = (premium + (exp_close - strike)) * 100   # negative
                outcome = "ASSIGNED"
            trades.append({
                "ticker": t, "date": str(cur), "strike": round(strike, 2),
                "premium": round(premium, 3), "expiry": str(expiry),
                "exp_close": round(exp_close, 2), "pnl": round(pnl, 2),
                "outcome": outcome,
            })
        cur += timedelta(days=1)

    total_pnl = sum(t["pnl"] for t in trades)
    wins = sum(1 for t in trades if t["pnl"] > 0)
    summary = {
        "trade_count": len(trades),
        "wins": wins,
        "losses": len(trades) - wins,
        "win_rate": round(wins / len(trades), 4) if trades else 0,
        "total_pnl": round(total_pnl, 2),
        "avg_pnl": round(total_pnl / len(trades), 2) if trades else 0,
        "trades_sample": trades[:50],
    }

    with session_scope() as s:
        s.execute(text("""
            UPDATE backtest_runs
            SET status = 'COMPLETE', completed_at = UTC_TIMESTAMP(),
                result = :r
            WHERE run_id = :rid
        """), {"r": json.dumps(summary), "rid": run_id})
        s.commit()

    return {"run_id": run_id, "summary": summary, "params": params_snapshot}


def list_runs(project_id: str, limit: int = 25) -> list[dict[str, Any]]:
    with session_scope() as s:
        rows = s.execute(text("""
            SELECT run_id, name, from_date, to_date, started_at,
                   completed_at, status
            FROM backtest_runs
            WHERE project_id = :p
            ORDER BY run_id DESC
            LIMIT :lim
        """), {"p": project_id, "lim": int(limit)}).fetchall()
    return [{
        "run_id": int(r[0]), "name": r[1],
        "from_date": str(r[2]), "to_date": str(r[3]),
        "started_at": r[4].isoformat() if r[4] else None,
        "completed_at": r[5].isoformat() if r[5] else None,
        "status": r[6],
    } for r in rows]


def get_run(project_id: str, run_id: int) -> dict[str, Any] | None:
    with session_scope() as s:
        row = s.execute(text("""
            SELECT name, from_date, to_date, started_at, completed_at,
                   status, params, result
            FROM backtest_runs
            WHERE run_id = :rid AND project_id = :p
        """), {"rid": int(run_id), "p": project_id}).fetchone()
    if not row:
        return None
    try:
        params = json.loads(row[6]) if row[6] else None
    except Exception:
        params = None
    try:
        result = json.loads(row[7]) if row[7] else None
    except Exception:
        result = None
    return {
        "run_id": run_id, "name": row[0],
        "from_date": str(row[1]), "to_date": str(row[2]),
        "started_at": row[3].isoformat() if row[3] else None,
        "completed_at": row[4].isoformat() if row[4] else None,
        "status": row[5], "params": params, "result": result,
    }