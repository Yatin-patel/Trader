"""Data access helpers for trading_projects, positions, contracts, events."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy import text

from .connection import insert_returning_id, session_scope
from .settings_store import _decrypt, _encrypt


# UUID validation pattern (accepts standard UUID format)
_UUID_RE = re.compile(
    r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'
)


def _is_valid_uuid(val: str | None) -> bool:
    """Return True if val is a valid UUID string."""
    if not val:
        return False
    return bool(_UUID_RE.match(str(val)))


@dataclass
class TradingProject:
    project_id: str
    project_name: str
    alpaca_api_key: str
    alpaca_secret_key: str
    alpaca_base_url: str
    alpaca_data_feed: str
    max_equity_allocation: float
    is_active: bool
    user_id: str | None = None
    broker_type: str = "alpaca"          # "alpaca" | "etrade"
    # ETrade-only fields. All decrypted; empty strings when missing.
    etrade_consumer_key: str = ""
    etrade_consumer_secret: str = ""
    etrade_access_token: str = ""
    etrade_access_token_secret: str = ""
    etrade_account_id_key: str = ""
    etrade_environment: str = "sandbox"  # "sandbox" | "production"


_PROJECT_FIELDS = (
    "project_id, project_name, alpaca_api_key, alpaca_secret_key, "
    "alpaca_base_url, alpaca_data_feed, max_equity_allocation, is_active, "
    "user_id, broker_type, "
    "etrade_consumer_key, etrade_consumer_secret, "
    "etrade_access_token, etrade_access_token_secret, "
    "etrade_account_id_key, etrade_environment"
)


def _row_to_project(r: Any) -> TradingProject:
    return TradingProject(
        project_id=r[0], project_name=r[1],
        alpaca_api_key=_decrypt(r[2]) or "",
        alpaca_secret_key=_decrypt(r[3]) or "",
        alpaca_base_url=r[4], alpaca_data_feed=r[5],
        max_equity_allocation=float(r[6]),
        is_active=bool(r[7]),
        user_id=str(r[8]) if r[8] is not None else None,
        broker_type=(r[9] or "alpaca"),
        etrade_consumer_key=_decrypt(r[10]) or "",
        etrade_consumer_secret=_decrypt(r[11]) or "",
        etrade_access_token=_decrypt(r[12]) or "",
        etrade_access_token_secret=_decrypt(r[13]) or "",
        etrade_account_id_key=r[14] or "",
        etrade_environment=(r[15] or "sandbox"),
    )


class ProjectsRepo:
    """All read methods accept optional user_id to scope results to one user.

    When user_id is None the query is NOT scoped (used by the multi-tenant
    runner, which manages every active project regardless of owner, and by
    migrations / admin tooling).
    """

    @staticmethod
    def list_active(user_id: str | None = None) -> list[TradingProject]:
        sql = (f"SELECT {_PROJECT_FIELDS} FROM trading_projects "
               f"WHERE is_active = 1")
        params: dict[str, Any] = {}
        if user_id is not None:
            # Validate UUID format to prevent SQL conversion errors
            if not _is_valid_uuid(user_id):
                return []
            sql += " AND user_id = :u"
            params["u"] = user_id
        with session_scope() as s:
            rows = s.execute(text(sql), params).fetchall()
        return [_row_to_project(r) for r in rows]

    @staticmethod
    def list_all(user_id: str | None = None) -> list[TradingProject]:
        sql = f"SELECT {_PROJECT_FIELDS} FROM trading_projects"
        params: dict[str, Any] = {}
        if user_id is not None:
            # Validate UUID format to prevent SQL conversion errors
            if not _is_valid_uuid(user_id):
                return []
            sql += " WHERE user_id = :u"
            params["u"] = user_id
        with session_scope() as s:
            rows = s.execute(text(sql), params).fetchall()
        return [_row_to_project(r) for r in rows]

    @staticmethod
    def get(project_id: str,
            user_id: str | None = None) -> TradingProject | None:
        """Returns None if project doesn't exist OR (when user_id is given)
        the project belongs to another user. Caller treats both as 404."""
        sql = (f"SELECT {_PROJECT_FIELDS} FROM trading_projects "
               f"WHERE project_id = :p")
        params: dict[str, Any] = {"p": project_id}
        if user_id is not None:
            # Validate UUID format to prevent SQL conversion errors
            if not _is_valid_uuid(user_id):
                return None
            sql += " AND user_id = :u"
            params["u"] = user_id
        with session_scope() as s:
            row = s.execute(text(sql), params).fetchone()
        return _row_to_project(row) if row else None

    @staticmethod
    def upsert(project: TradingProject) -> None:
        with session_scope() as s:
            existing = s.execute(
                text("SELECT 1 FROM trading_projects WHERE project_id = :p"),
                {"p": project.project_id},
            ).fetchone()
            payload = {
                "p": project.project_id,
                "n": project.project_name,
                "k": _encrypt(project.alpaca_api_key),
                "s": _encrypt(project.alpaca_secret_key),
                "u": project.alpaca_base_url,
                "f": project.alpaca_data_feed,
                "m": project.max_equity_allocation,
                "a": 1 if project.is_active else 0,
                "uid": project.user_id,
                "bt": project.broker_type or "alpaca",
                "eck": _encrypt(project.etrade_consumer_key or ""),
                "ecs": _encrypt(project.etrade_consumer_secret or ""),
                "eat": _encrypt(project.etrade_access_token or ""),
                "eas": _encrypt(project.etrade_access_token_secret or ""),
                "eak": project.etrade_account_id_key or "",
                "een": project.etrade_environment or "sandbox",
            }
            if existing:
                s.execute(text("""
                    UPDATE trading_projects SET
                        project_name = :n,
                        alpaca_api_key = :k,
                        alpaca_secret_key = :s,
                        alpaca_base_url = :u,
                        alpaca_data_feed = :f,
                        max_equity_allocation = :m,
                        is_active = :a,
                        user_id = COALESCE(:uid, user_id),
                        broker_type = :bt,
                        etrade_consumer_key = :eck,
                        etrade_consumer_secret = :ecs,
                        etrade_access_token = :eat,
                        etrade_access_token_secret = :eas,
                        etrade_account_id_key = :eak,
                        etrade_environment = :een,
                        updated_at = UTC_TIMESTAMP()
                    WHERE project_id = :p
                """), payload)
            else:
                s.execute(text("""
                    INSERT INTO trading_projects
                        (project_id, project_name, alpaca_api_key, alpaca_secret_key,
                         alpaca_base_url, alpaca_data_feed, max_equity_allocation,
                         is_active, user_id, broker_type,
                         etrade_consumer_key, etrade_consumer_secret,
                         etrade_access_token, etrade_access_token_secret,
                         etrade_account_id_key, etrade_environment)
                    VALUES (:p, :n, :k, :s, :u, :f, :m, :a, :uid, :bt,
                            :eck, :ecs, :eat, :eas, :eak, :een)
                """), payload)
            s.commit()

    @staticmethod
    def update_etrade_tokens(project_id: str, *,
                             access_token: str,
                             access_token_secret: str,
                             account_id_key: str | None = None) -> None:
        """Persist tokens after a successful OAuth dance, encrypted at rest."""
        with session_scope() as s:
            params = {
                "p": project_id,
                "eat": _encrypt(access_token),
                "eas": _encrypt(access_token_secret),
                "eak": account_id_key,
            }
            s.execute(text("""
                UPDATE trading_projects SET
                    etrade_access_token = :eat,
                    etrade_access_token_secret = :eas,
                    etrade_account_id_key = COALESCE(:eak, etrade_account_id_key),
                    etrade_token_renewed_at = UTC_TIMESTAMP(),
                    updated_at = UTC_TIMESTAMP()
                WHERE project_id = :p
            """), params)
            s.commit()

    @staticmethod
    def delete(project_id: str) -> None:
        with session_scope() as s:
            s.execute(text("DELETE FROM trading_projects WHERE project_id = :p"), {"p": project_id})
            s.commit()

    @staticmethod
    def assign_owner(project_id: str, user_id: str) -> None:
        """Used by migrations + admin tooling to claim a project."""
        with session_scope() as s:
            s.execute(text(
                "UPDATE trading_projects SET user_id = :u "
                "WHERE project_id = :p"
            ), {"p": project_id, "u": user_id})
            s.commit()


class PositionsRepo:
    @staticmethod
    def open_position(project_id: str, ticker: str, entry_price: float, quantity: int,
                      stop_loss_dollars: float) -> int:
        with session_scope() as s:
            position_id = insert_returning_id(s, """
                INSERT INTO stock_positions
                    (project_id, ticker, entry_price, current_price, max_loss_threshold, quantity, status)
                VALUES (:p, :t, :e, :e, :mlt, :q, 'OPEN')
            """, {"p": project_id, "t": ticker, "e": entry_price,
                   "mlt": entry_price - stop_loss_dollars, "q": quantity})
            s.commit()
            return position_id

    @staticmethod
    def list_open(project_id: str) -> list[dict[str, Any]]:
        with session_scope() as s:
            rows = s.execute(text("""
                SELECT position_id, ticker, entry_price, current_price, max_loss_threshold,
                       quantity, status, opened_at
                FROM stock_positions
                WHERE project_id = :p AND status = 'OPEN'
            """), {"p": project_id}).fetchall()
        return [dict(zip(
            ("position_id", "ticker", "entry_price", "current_price",
             "max_loss_threshold", "quantity", "status", "opened_at"),
            r,
        )) for r in rows]

    @staticmethod
    def close(position_id: int, final_status: str = "CLOSED") -> None:
        with session_scope() as s:
            s.execute(text("""
                UPDATE stock_positions
                SET status = :st, closed_at = UTC_TIMESTAMP()
                WHERE position_id = :pid
            """), {"st": final_status, "pid": position_id})
            s.commit()

    @staticmethod
    def update_price(position_id: int, current_price: float) -> None:
        with session_scope() as s:
            s.execute(text("""
                UPDATE stock_positions
                SET current_price = :cp
                WHERE position_id = :pid
            """), {"cp": current_price, "pid": position_id})
            s.commit()


class WheelRepo:
    @staticmethod
    def open_contract(project_id: str, ticker: str, phase: str, option_symbol: str,
                      strike: float, premium: float, expiration: date,
                      delta: float | None,
                      quantity: int = 1,
                      underlying_at_entry: float | None = None,
                      settings_snapshot: dict[str, Any] | None = None) -> int:
        import json as _json
        snap_text = _json.dumps(settings_snapshot, default=str) if settings_snapshot else None
        with session_scope() as s:
            contract_id = insert_returning_id(s, """
                INSERT INTO wheel_contracts
                    (project_id, ticker, strategy_phase, option_symbol, strike_price,
                     premium_collected, expiration_date, delta_at_entry, quantity,
                     underlying_at_entry, settings_snapshot)
                VALUES (:p, :t, :ph, :os, :sk, :pr, :ex, :d, :q, :ue, :ss)
            """, {"p": project_id, "t": ticker, "ph": phase, "os": option_symbol,
                   "sk": strike, "pr": premium, "ex": expiration, "d": delta,
                   "q": quantity, "ue": underlying_at_entry, "ss": snap_text})
            s.commit()
            return position_id

    @staticmethod
    def list_open(project_id: str) -> list[dict[str, Any]]:
        with session_scope() as s:
            rows = s.execute(text("""
                SELECT contract_id, ticker, strategy_phase, option_symbol, strike_price,
                       premium_collected, expiration_date, delta_at_entry, is_assigned,
                       opened_at, quantity, underlying_at_entry, settings_snapshot
                FROM wheel_contracts
                WHERE project_id = :p AND is_closed = 0
            """), {"p": project_id}).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            try:
                snap = json.loads(r[12]) if r[12] else None
            except Exception:
                snap = None
            out.append({
                "contract_id": r[0], "ticker": r[1], "strategy_phase": r[2],
                "option_symbol": r[3], "strike_price": float(r[4]),
                "premium_collected": float(r[5]), "expiration_date": r[6],
                "delta_at_entry": float(r[7]) if r[7] is not None else None,
                "is_assigned": bool(r[8]), "opened_at": r[9],
                "quantity": int(r[10] or 1),
                "underlying_at_entry": float(r[11]) if r[11] is not None else None,
                "settings_snapshot": snap,
            })
        return out

    @staticmethod
    def mark_assigned(contract_id: int) -> None:
        with session_scope() as s:
            s.execute(text("""
                UPDATE wheel_contracts
                SET is_assigned = 1, strategy_phase = 'STOCK_ASSIGNED', updated_at = UTC_TIMESTAMP()
                WHERE contract_id = :c
            """), {"c": contract_id})
            s.commit()

    @staticmethod
    def close(contract_id: int) -> None:
        with session_scope() as s:
            s.execute(text("""
                UPDATE wheel_contracts
                SET is_closed = 1, updated_at = UTC_TIMESTAMP()
                WHERE contract_id = :c
            """), {"c": contract_id})
            s.commit()


class EventsRepo:
    @staticmethod
    def log(project_id: str, node_name: str, event_type: str, payload: Any) -> None:
        try:
            payload_text = json.dumps(payload, default=str)
        except Exception:
            payload_text = str(payload)
        with session_scope() as s:
            s.execute(text("""
                INSERT INTO agent_events (project_id, node_name, event_type, payload)
                VALUES (:p, :n, :e, :pl)
            """), {"p": project_id, "n": node_name, "e": event_type, "pl": payload_text})
            s.commit()

    @staticmethod
    def query(project_id: str, *, node: str | None = None,
              event_type: str | None = None, search: str | None = None,
              limit: int = 100, before_id: int | None = None) -> list[dict[str, Any]]:
        """Filterable event query. `search` does a LIKE over the payload JSON."""
        where = ["project_id = :p"]
        params: dict[str, Any] = {"p": project_id, "lim": int(limit)}
        if node:
            where.append("node_name = :n")
            params["n"] = node
        if event_type:
            where.append("event_type = :e")
            params["e"] = event_type
        if search:
            where.append("payload LIKE :s")
            params["s"] = f"%{search}%"
        if before_id:
            where.append("event_id < :bid")
            params["bid"] = int(before_id)
        sql = (
            "SELECT event_id, node_name, event_type, payload, created_at "
            "FROM agent_events "
            f"WHERE {' AND '.join(where)} "
            "ORDER BY event_id DESC "
            "LIMIT :lim"
        )
        with session_scope() as s:
            rows = s.execute(text(sql), params).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            try:
                payload = json.loads(r[3]) if r[3] else None
            except Exception:
                payload = r[3]
            out.append({
                "event_id": r[0], "node_name": r[1], "event_type": r[2],
                "payload": payload, "created_at": r[4],
            })
        return out

    @staticmethod
    def recent(project_id: str, limit: int = 50) -> list[dict[str, Any]]:
        with session_scope() as s:
            rows = s.execute(text("""
                SELECT event_id, node_name, event_type, payload, created_at
                FROM agent_events
                WHERE project_id = :p
                ORDER BY created_at DESC
                LIMIT :lim
            """), {"lim": limit, "p": project_id}).fetchall()
        out = []
        for r in rows:
            try:
                payload = json.loads(r[3]) if r[3] else None
            except Exception:
                payload = r[3]
            out.append({
                "event_id": r[0], "node_name": r[1], "event_type": r[2],
                "payload": payload, "created_at": r[4],
            })
        return out