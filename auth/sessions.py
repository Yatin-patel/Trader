"""Server-side session store.

Sessions are random GUIDs stored in user_sessions with a 14-day TTL.
The token is set in an HttpOnly cookie. Logout deletes the row.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text

from db.connection import session_scope

SESSION_COOKIE = "trader_session"
SESSION_TTL_HOURS = 14 * 24   # 14 days


def create_session(user_id: str, *, ip: str | None = None,
                   user_agent: str | None = None) -> tuple[str, datetime]:
    """Create a new session for the given user.

    Returns (session_token, expires_at). The token is the cookie value.
    """
    token = str(uuid.uuid4())
    expires = datetime.now(tz=timezone.utc) + timedelta(hours=SESSION_TTL_HOURS)
    with session_scope() as s:
        s.execute(text("""
            INSERT INTO user_sessions
                (session_token, user_id, expires_at, ip_address, user_agent)
            VALUES (:t, :u, :e, :ip, :ua)
        """), {"t": token, "u": user_id, "e": expires,
               "ip": (ip or "")[:45], "ua": (user_agent or "")[:500]})
        s.commit()
    return token, expires


def get_session(token: str) -> dict[str, Any] | None:
    """Return {user_id, expires_at} for a live session, else None.

    Expired sessions are deleted lazily on read.
    """
    if not token:
        return None
    with session_scope() as s:
        row = s.execute(text("""
            SELECT user_id, expires_at FROM user_sessions
            WHERE session_token = :t
        """), {"t": token}).fetchone()
        if not row:
            return None
        expires_at = row[1]
        if expires_at and expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at and expires_at < datetime.now(tz=timezone.utc):
            # Expired — clean up.
            s.execute(text("""
                DELETE FROM user_sessions WHERE session_token = :t
            """), {"t": token})
            s.commit()
            return None
        return {"user_id": str(row[0]), "expires_at": expires_at}


def revoke_session(token: str) -> None:
    if not token:
        return
    with session_scope() as s:
        s.execute(text(
            "DELETE FROM user_sessions WHERE session_token = :t"
        ), {"t": token})
        s.commit()


def revoke_all_for_user(user_id: str) -> int:
    """Log the user out of every session. Returns number of sessions killed."""
    with session_scope() as s:
        result = s.execute(text(
            "DELETE FROM user_sessions WHERE user_id = :u"
        ), {"u": user_id})
        s.commit()
        return result.rowcount or 0


def revoke_others_for_user(user_id: str, keep_token: str) -> int:
    """Log out every session EXCEPT the one whose token == keep_token."""
    if not keep_token:
        return 0
    with session_scope() as s:
        result = s.execute(text(
            "DELETE FROM user_sessions "
            "WHERE user_id = :u AND session_token <> :t"
        ), {"u": user_id, "t": keep_token})
        s.commit()
        return result.rowcount or 0


def list_for_user(user_id: str) -> list[dict[str, Any]]:
    """All live sessions for the user, oldest first."""
    with session_scope() as s:
        rows = s.execute(text("""
            SELECT session_token, created_at, expires_at, ip_address, user_agent
            FROM user_sessions
            WHERE user_id = :u AND expires_at > UTC_TIMESTAMP()
            ORDER BY created_at DESC
        """), {"u": user_id}).fetchall()
    out = []
    for r in rows:
        out.append({
            "token": str(r[0]),
            "created_at": r[1],
            "expires_at": r[2],
            "ip_address": r[3],
            "user_agent": r[4],
        })
    return out
