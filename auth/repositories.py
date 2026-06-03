"""DB access for the users table."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import text

from db.connection import session_scope


@dataclass
class AuthUser:
    user_id: str
    email: str
    is_admin: bool
    is_active: bool
    totp_enabled: bool
    email_verified: bool
    created_at: datetime
    last_login_at: datetime | None


def _row_to_user(row: Any) -> AuthUser:
    return AuthUser(
        user_id=str(row[0]),
        email=row[1],
        is_admin=bool(row[2]),
        is_active=bool(row[3]),
        totp_enabled=bool(row[4]),
        email_verified=bool(row[5]),
        created_at=row[6],
        last_login_at=row[7],
    )


class UsersRepo:
    _SELECT_FIELDS = (
        "user_id, email, is_admin, is_active, totp_enabled, "
        "email_verified, created_at, last_login_at"
    )

    @classmethod
    def get_by_email(cls, email: str) -> AuthUser | None:
        with session_scope() as s:
            row = s.execute(text(
                f"SELECT {cls._SELECT_FIELDS} FROM users "
                f"WHERE LOWER(email) = LOWER(:e)"
            ), {"e": email}).fetchone()
        return _row_to_user(row) if row else None

    @classmethod
    def get_by_id(cls, user_id: str) -> AuthUser | None:
        with session_scope() as s:
            row = s.execute(text(
                f"SELECT {cls._SELECT_FIELDS} FROM users WHERE user_id = :u"
            ), {"u": user_id}).fetchone()
        return _row_to_user(row) if row else None

    @classmethod
    def get_password_hash(cls, user_id: str) -> str | None:
        with session_scope() as s:
            row = s.execute(text(
                "SELECT password_hash FROM users WHERE user_id = :u"
            ), {"u": user_id}).fetchone()
        return row[0] if row else None

    @classmethod
    def create(cls, *, email: str, password_hash: str,
               is_admin: bool = False) -> AuthUser:
        import uuid
        user_id = str(uuid.uuid4())
        with session_scope() as s:
            s.execute(text("""
                INSERT INTO users (user_id, email, password_hash, is_admin)
                VALUES (:uid, :e, :p, :a)
            """), {"uid": user_id, "e": email.lower().strip(),
                   "p": password_hash, "a": 1 if is_admin else 0})
            s.commit()
            row = s.execute(text(
                f"SELECT {cls._SELECT_FIELDS} FROM users WHERE user_id = :u"
            ), {"u": user_id}).fetchone()
        return _row_to_user(row)

    @classmethod
    def update_password(cls, user_id: str, password_hash: str) -> None:
        with session_scope() as s:
            s.execute(text(
                "UPDATE users SET password_hash = :p WHERE user_id = :u"
            ), {"u": user_id, "p": password_hash})
            s.commit()

    @classmethod
    def touch_last_login(cls, user_id: str) -> None:
        with session_scope() as s:
            s.execute(text(
                "UPDATE users SET last_login_at = UTC_TIMESTAMP() "
                "WHERE user_id = :u"
            ), {"u": user_id})
            s.commit()

    @classmethod
    def get_totp_secret(cls, user_id: str) -> str | None:
        with session_scope() as s:
            row = s.execute(text(
                "SELECT totp_secret FROM users WHERE user_id = :u"
            ), {"u": user_id}).fetchone()
        return row[0] if row and row[0] else None

    @classmethod
    def set_totp(cls, user_id: str, *, secret: str, enabled: bool) -> None:
        with session_scope() as s:
            s.execute(text(
                "UPDATE users SET totp_secret = :sec, totp_enabled = :en "
                "WHERE user_id = :u"
            ), {"u": user_id, "sec": secret, "en": 1 if enabled else 0})
            s.commit()

    @classmethod
    def disable_totp(cls, user_id: str) -> None:
        with session_scope() as s:
            s.execute(text(
                "UPDATE users SET totp_secret = NULL, totp_enabled = 0 "
                "WHERE user_id = :u"
            ), {"u": user_id})
            s.commit()

    @classmethod
    def count(cls) -> int:
        with session_scope() as s:
            row = s.execute(text(
                "SELECT COUNT(*) FROM users"
            )).fetchone()
        return int(row[0] or 0)

    @classmethod
    def list_all(cls) -> list[AuthUser]:
        with session_scope() as s:
            rows = s.execute(text(
                f"SELECT {cls._SELECT_FIELDS} FROM users "
                f"ORDER BY created_at ASC"
            )).fetchall()
        return [_row_to_user(r) for r in rows]
