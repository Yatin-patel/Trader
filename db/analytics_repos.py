"""Repositories for analytics: closed_contracts, closed_positions,
portfolio_snapshots. Kept separate from repositories.py for clarity."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text

from .connection import session_scope


class ClosedContractsRepo:
    @staticmethod
    def insert(*, project_id: str, contract_id: int | None, ticker: str,
               option_symbol: str | None, strategy_phase: str,
               opened_at: datetime, closed_at: datetime,
               strike_price: float, quantity: int,
               premium_collected: float, close_cost: float,
               closure_reason: str,
               delta_at_entry: float | None = None,
               dte_at_entry: int | None = None,
               underlying_at_entry: float | None = None,
               underlying_at_close: float | None = None,
               settings_snapshot: dict[str, Any] | None = None) -> int:
        # Normalize naive vs aware datetimes (SQL Server returns naive UTC)
        if opened_at.tzinfo is None:
            opened_at = opened_at.replace(tzinfo=timezone.utc)
        if closed_at.tzinfo is None:
            closed_at = closed_at.replace(tzinfo=timezone.utc)
        days_held = max(0, (closed_at - opened_at).days)
        realized_pnl = float(premium_collected) - float(close_cost)
        snapshot_text = json.dumps(settings_snapshot, default=str) if settings_snapshot else None
        with session_scope() as s:
            row = s.execute(text("""
                INSERT INTO dbo.closed_contracts
                    (contract_id, project_id, ticker, option_symbol, strategy_phase,
                     opened_at, closed_at, days_held, strike_price, quantity,
                     premium_collected, close_cost, realized_pnl, closure_reason,
                     delta_at_entry, dte_at_entry, underlying_at_entry,
                     underlying_at_close, settings_snapshot)
                OUTPUT INSERTED.closure_id
                VALUES (:cid, :p, :t, :os, :ph, :oa, :ca, :dh, :sk, :q,
                        :pc, :cc, :rp, :cr, :de, :dt, :ue, :uc, :ss)
            """), {
                "cid": contract_id, "p": project_id, "t": ticker, "os": option_symbol,
                "ph": strategy_phase, "oa": opened_at, "ca": closed_at,
                "dh": days_held, "sk": strike_price, "q": quantity,
                "pc": premium_collected, "cc": close_cost, "rp": realized_pnl,
                "cr": closure_reason, "de": delta_at_entry, "dt": dte_at_entry,
                "ue": underlying_at_entry, "uc": underlying_at_close,
                "ss": snapshot_text,
            }).fetchone()
            s.commit()
            return int(row[0])

    @staticmethod
    def list(project_id: str, *, since: datetime | None = None,
             ticker: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        where = ["project_id = :p"]
        params: dict[str, Any] = {"p": project_id, "lim": int(limit)}
        if since:
            where.append("closed_at >= :since")
            params["since"] = since
        if ticker:
            where.append("ticker = :tk")
            params["tk"] = ticker
        sql = (
            "SELECT TOP (:lim) closure_id, contract_id, ticker, option_symbol,"
            " strategy_phase, opened_at, closed_at, days_held, strike_price,"
            " quantity, premium_collected, close_cost, realized_pnl,"
            " closure_reason, delta_at_entry, dte_at_entry,"
            " underlying_at_entry, underlying_at_close, settings_snapshot "
            "FROM dbo.closed_contracts "
            f"WHERE {' AND '.join(where)} "
            "ORDER BY closed_at DESC"
        )
        with session_scope() as s:
            rows = s.execute(text(sql), params).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            try:
                snap = json.loads(r[18]) if r[18] else None
            except Exception:
                snap = None
            out.append({
                "closure_id": r[0], "contract_id": r[1], "ticker": r[2],
                "option_symbol": r[3], "strategy_phase": r[4],
                "opened_at": r[5], "closed_at": r[6], "days_held": r[7],
                "strike_price": float(r[8]), "quantity": int(r[9]),
                "premium_collected": float(r[10]), "close_cost": float(r[11]),
                "realized_pnl": float(r[12]), "closure_reason": r[13],
                "delta_at_entry": float(r[14]) if r[14] is not None else None,
                "dte_at_entry": int(r[15]) if r[15] is not None else None,
                "underlying_at_entry": float(r[16]) if r[16] is not None else None,
                "underlying_at_close": float(r[17]) if r[17] is not None else None,
                "settings_snapshot": snap,
            })
        return out

    @staticmethod
    def realized_pnl_since(project_id: str, since: datetime) -> float:
        with session_scope() as s:
            row = s.execute(text("""
                SELECT ISNULL(SUM(realized_pnl), 0) FROM dbo.closed_contracts
                WHERE project_id = :p AND closed_at >= :since
            """), {"p": project_id, "since": since}).fetchone()
        return float(row[0] or 0)

    @staticmethod
    def by_ticker(project_id: str, *, since: datetime | None = None,
                  min_trades: int = 1) -> list[dict[str, Any]]:
        where = ["project_id = :p"]
        params: dict[str, Any] = {"p": project_id, "mt": int(min_trades)}
        if since:
            where.append("closed_at >= :since")
            params["since"] = since
        sql = (
            "SELECT ticker, COUNT(*) AS trade_count,"
            " SUM(realized_pnl) AS total_pnl,"
            " SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) AS wins,"
            " AVG(realized_pnl) AS avg_pnl,"
            " AVG(CAST(days_held AS FLOAT)) AS avg_days_held,"
            " SUM(premium_collected) AS total_premium,"
            " MAX(realized_pnl) AS biggest_win,"
            " MIN(realized_pnl) AS biggest_loss "
            "FROM dbo.closed_contracts "
            f"WHERE {' AND '.join(where)} "
            "GROUP BY ticker "
            "HAVING COUNT(*) >= :mt "
            "ORDER BY total_pnl DESC"
        )
        with session_scope() as s:
            rows = s.execute(text(sql), params).fetchall()
        return [{
            "ticker": r[0], "trade_count": int(r[1]),
            "total_pnl": float(r[2] or 0), "wins": int(r[3] or 0),
            "avg_pnl": float(r[4] or 0), "avg_days_held": float(r[5] or 0),
            "total_premium": float(r[6] or 0),
            "biggest_win": float(r[7] or 0), "biggest_loss": float(r[8] or 0),
            "win_rate": (int(r[3] or 0) / int(r[1])) if r[1] else 0.0,
        } for r in rows]


class ClosedPositionsRepo:
    @staticmethod
    def insert(*, project_id: str, position_id: int | None, ticker: str,
               quantity: int, entry_price: float, exit_price: float,
               opened_at: datetime, closed_at: datetime, closure_reason: str,
               associated_contract_id: int | None = None) -> int:
        if opened_at.tzinfo is None:
            opened_at = opened_at.replace(tzinfo=timezone.utc)
        if closed_at.tzinfo is None:
            closed_at = closed_at.replace(tzinfo=timezone.utc)
        days_held = max(0, (closed_at - opened_at).days)
        realized_pnl = (float(exit_price) - float(entry_price)) * quantity
        with session_scope() as s:
            row = s.execute(text("""
                INSERT INTO dbo.closed_positions
                    (position_id, project_id, ticker, quantity, entry_price,
                     exit_price, opened_at, closed_at, days_held, realized_pnl,
                     closure_reason, associated_contract_id)
                OUTPUT INSERTED.closure_id
                VALUES (:pid, :p, :t, :q, :ep, :xp, :oa, :ca, :dh, :rp, :cr, :ac)
            """), {
                "pid": position_id, "p": project_id, "t": ticker,
                "q": quantity, "ep": entry_price, "xp": exit_price,
                "oa": opened_at, "ca": closed_at, "dh": days_held,
                "rp": realized_pnl, "cr": closure_reason,
                "ac": associated_contract_id,
            }).fetchone()
            s.commit()
            return int(row[0])

    @staticmethod
    def realized_pnl_since(project_id: str, since: datetime) -> float:
        with session_scope() as s:
            row = s.execute(text("""
                SELECT ISNULL(SUM(realized_pnl), 0) FROM dbo.closed_positions
                WHERE project_id = :p AND closed_at >= :since
            """), {"p": project_id, "since": since}).fetchone()
        return float(row[0] or 0)


class PortfolioSnapshotsRepo:
    @staticmethod
    def insert(*, project_id: str, cash: float, buying_power: float,
               equity: float, long_market_value: float | None = None,
               short_market_value: float | None = None,
               realized_pnl_day: float | None = None,
               unrealized_pnl: float | None = None) -> int:
        with session_scope() as s:
            row = s.execute(text("""
                INSERT INTO dbo.portfolio_snapshots
                    (project_id, cash, buying_power, equity, long_market_value,
                     short_market_value, realized_pnl_day, unrealized_pnl)
                OUTPUT INSERTED.snapshot_id
                VALUES (:p, :c, :bp, :eq, :lmv, :smv, :rpd, :up)
            """), {
                "p": project_id, "c": cash, "bp": buying_power, "eq": equity,
                "lmv": long_market_value, "smv": short_market_value,
                "rpd": realized_pnl_day, "up": unrealized_pnl,
            }).fetchone()
            s.commit()
            return int(row[0])

    @staticmethod
    def curve(project_id: str, *, since: datetime,
              max_points: int = 500) -> list[dict[str, Any]]:
        with session_scope() as s:
            rows = s.execute(text("""
                SELECT snapshot_at, cash, buying_power, equity,
                       realized_pnl_day, unrealized_pnl
                FROM dbo.portfolio_snapshots
                WHERE project_id = :p AND snapshot_at >= :since
                ORDER BY snapshot_at ASC
            """), {"p": project_id, "since": since}).fetchall()
        # Downsample if too many points
        if len(rows) > max_points and rows:
            step = max(1, len(rows) // max_points)
            rows = rows[::step]
        return [{
            "t": r[0].isoformat() if r[0] else None,
            "cash": float(r[1]),
            "buying_power": float(r[2]),
            "equity": float(r[3]),
            "realized_pnl_day": float(r[4] or 0),
            "unrealized_pnl": float(r[5] or 0),
        } for r in rows]

    @staticmethod
    def latest(project_id: str) -> dict[str, Any] | None:
        with session_scope() as s:
            row = s.execute(text("""
                SELECT TOP 1 snapshot_at, cash, buying_power, equity,
                       realized_pnl_day, unrealized_pnl
                FROM dbo.portfolio_snapshots
                WHERE project_id = :p
                ORDER BY snapshot_at DESC
            """), {"p": project_id}).fetchone()
        if not row:
            return None
        return {
            "t": row[0].isoformat() if row[0] else None,
            "cash": float(row[1]),
            "buying_power": float(row[2]),
            "equity": float(row[3]),
            "realized_pnl_day": float(row[4] or 0),
            "unrealized_pnl": float(row[5] or 0),
        }

    @staticmethod
    def earliest(project_id: str) -> dict[str, Any] | None:
        """First-ever snapshot — used as the project's starting balance."""
        with session_scope() as s:
            row = s.execute(text("""
                SELECT TOP 1 snapshot_at, cash, buying_power, equity
                FROM dbo.portfolio_snapshots
                WHERE project_id = :p
                ORDER BY snapshot_at ASC
            """), {"p": project_id}).fetchone()
        if not row:
            return None
        return {
            "t": row[0].isoformat() if row[0] else None,
            "cash": float(row[1]),
            "buying_power": float(row[2]),
            "equity": float(row[3]),
        }

    @staticmethod
    def at_or_after(project_id: str, when: datetime) -> dict[str, Any] | None:
        """First snapshot taken at-or-after the given timestamp.

        Used to compute returns over a fixed period (e.g. 7 days ago).
        """
        with session_scope() as s:
            row = s.execute(text("""
                SELECT TOP 1 snapshot_at, equity
                FROM dbo.portfolio_snapshots
                WHERE project_id = :p AND snapshot_at >= :w
                ORDER BY snapshot_at ASC
            """), {"p": project_id, "w": when}).fetchone()
        if not row:
            return None
        return {
            "t": row[0].isoformat() if row[0] else None,
            "equity": float(row[1]),
        }
