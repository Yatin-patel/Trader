"""Repository for risk_limits and earnings_cache."""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import text

from .connection import session_scope


class RiskLimitsRepo:
    LIMIT_TYPES = (
        "daily_loss",
        "drawdown_pct",
        "consecutive_losses",
        "error_storm",
    )
    ACTIONS = ("HALT", "LIQUIDATE")

    @staticmethod
    def list(project_id: str, *, enabled_only: bool = False) -> list[dict[str, Any]]:
        where = ["project_id = :p"]
        if enabled_only:
            where.append("enabled = 1")
        sql = (
            "SELECT limit_id, limit_type, threshold, window_minutes, action,"
            " enabled, breach_count, last_breached_at, last_breach_value,"
            " created_at "
            "FROM dbo.risk_limits "
            f"WHERE {' AND '.join(where)} "
            "ORDER BY created_at DESC"
        )
        with session_scope() as s:
            rows = s.execute(text(sql), {"p": project_id}).fetchall()
        return [{
            "limit_id": int(r[0]), "limit_type": r[1],
            "threshold": float(r[2]),
            "window_minutes": int(r[3]) if r[3] is not None else None,
            "action": r[4], "enabled": bool(r[5]),
            "breach_count": int(r[6]),
            "last_breached_at": r[7].isoformat() if r[7] else None,
            "last_breach_value": float(r[8]) if r[8] is not None else None,
            "created_at": r[9].isoformat() if r[9] else None,
        } for r in rows]

    @staticmethod
    def upsert(*, project_id: str, limit_type: str, threshold: float,
               action: str = "HALT", window_minutes: int | None = None,
               enabled: bool = True, limit_id: int | None = None) -> int:
        if limit_type not in RiskLimitsRepo.LIMIT_TYPES:
            raise ValueError(f"unknown limit_type {limit_type}")
        if action not in RiskLimitsRepo.ACTIONS:
            raise ValueError(f"unknown action {action}")
        with session_scope() as s:
            if limit_id:
                s.execute(text("""
                    UPDATE dbo.risk_limits
                    SET limit_type = :lt, threshold = :th, window_minutes = :wm,
                        action = :a, enabled = :en
                    WHERE limit_id = :lid AND project_id = :p
                """), {"lt": limit_type, "th": threshold, "wm": window_minutes,
                       "a": action, "en": 1 if enabled else 0,
                       "lid": limit_id, "p": project_id})
                s.commit()
                return limit_id
            row = s.execute(text("""
                INSERT INTO dbo.risk_limits
                    (project_id, limit_type, threshold, window_minutes,
                     action, enabled)
                OUTPUT INSERTED.limit_id
                VALUES (:p, :lt, :th, :wm, :a, :en)
            """), {"p": project_id, "lt": limit_type, "th": threshold,
                   "wm": window_minutes, "a": action,
                   "en": 1 if enabled else 0}).fetchone()
            s.commit()
            return int(row[0])

    @staticmethod
    def delete(project_id: str, limit_id: int) -> None:
        with session_scope() as s:
            s.execute(text("""
                DELETE FROM dbo.risk_limits
                WHERE limit_id = :lid AND project_id = :p
            """), {"lid": limit_id, "p": project_id})
            s.commit()

    @staticmethod
    def record_breach(limit_id: int, breach_value: float) -> None:
        with session_scope() as s:
            s.execute(text("""
                UPDATE dbo.risk_limits
                SET breach_count = breach_count + 1,
                    last_breached_at = SYSUTCDATETIME(),
                    last_breach_value = :v
                WHERE limit_id = :lid
            """), {"lid": limit_id, "v": breach_value})
            s.commit()


class EarningsCacheRepo:
    @staticmethod
    def get(ticker: str) -> dict[str, Any] | None:
        with session_scope() as s:
            row = s.execute(text("""
                SELECT ticker, next_earnings_date, fetched_at, source
                FROM dbo.earnings_cache WHERE ticker = :t
            """), {"t": ticker.upper()}).fetchone()
        if not row:
            return None
        return {
            "ticker": row[0],
            "next_earnings_date": row[1],
            "fetched_at": row[2],
            "source": row[3],
        }

    @staticmethod
    def upsert(ticker: str, next_earnings_date: date | None,
               source: str = "yfinance") -> None:
        ticker = ticker.upper()
        with session_scope() as s:
            existing = s.execute(text(
                "SELECT 1 FROM dbo.earnings_cache WHERE ticker = :t"
            ), {"t": ticker}).fetchone()
            if existing:
                s.execute(text("""
                    UPDATE dbo.earnings_cache
                    SET next_earnings_date = :d, fetched_at = SYSUTCDATETIME(),
                        source = :src
                    WHERE ticker = :t
                """), {"t": ticker, "d": next_earnings_date, "src": source})
            else:
                s.execute(text("""
                    INSERT INTO dbo.earnings_cache
                        (ticker, next_earnings_date, source)
                    VALUES (:t, :d, :src)
                """), {"t": ticker, "d": next_earnings_date, "src": source})
            s.commit()
