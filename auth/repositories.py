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
    # Manual admin-approval state. 'pending' is the default for new
    # signups; only 'active' can log in; 'rejected' is permanent (the
    # signup endpoint refuses re-registration on the same email).
    account_status: str = "active"


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
        account_status=(row[8] if len(row) > 8 and row[8] else "active"),
    )


class UsersRepo:
    _SELECT_FIELDS = (
        "user_id, email, is_admin, is_active, totp_enabled, "
        "email_verified, created_at, last_login_at, account_status"
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
               is_admin: bool = False,
               account_status: str = "pending",
               is_active: bool = False) -> AuthUser:
        """New users default to account_status='pending' + is_active=0.
        Admins (bootstrap path: very first user) override to
        account_status='active' + is_active=1 so the first-run flow
        still works without anyone to approve."""
        import uuid
        user_id = str(uuid.uuid4())
        with session_scope() as s:
            s.execute(text("""
                INSERT INTO users
                    (user_id, email, password_hash, is_admin,
                     is_active, account_status)
                VALUES (:uid, :e, :p, :a, :ia, :st)
            """), {"uid": user_id, "e": email.lower().strip(),
                   "p": password_hash, "a": 1 if is_admin else 0,
                   "ia": 1 if is_active else 0,
                   "st": account_status})
            s.commit()
            row = s.execute(text(
                f"SELECT {cls._SELECT_FIELDS} FROM users WHERE user_id = :u"
            ), {"u": user_id}).fetchone()
        return _row_to_user(row)

    @classmethod
    def list_pending(cls) -> list[AuthUser]:
        """Users awaiting admin review."""
        with session_scope() as s:
            rows = s.execute(text(
                f"SELECT {cls._SELECT_FIELDS} FROM users "
                f"WHERE account_status = 'pending' "
                f"ORDER BY created_at ASC"
            )).fetchall()
        return [_row_to_user(r) for r in rows]

    @classmethod
    def list_admins(cls) -> list[AuthUser]:
        """Used to fan out admin-notification emails."""
        with session_scope() as s:
            rows = s.execute(text(
                f"SELECT {cls._SELECT_FIELDS} FROM users "
                f"WHERE is_admin = 1 AND account_status = 'active'"
            )).fetchall()
        return [_row_to_user(r) for r in rows]

    @classmethod
    def set_status(cls, user_id: str, status: str) -> None:
        """Flip account_status + is_active in a single transaction.
        is_active mirrors the status for fast filtering — 'active' -> 1,
        anything else -> 0."""
        if status not in ("pending", "active", "rejected"):
            raise ValueError(f"unknown account_status {status!r}")
        with session_scope() as s:
            s.execute(text("""
                UPDATE users
                SET account_status = :st,
                    is_active = :ia
                WHERE user_id = :u
            """), {"u": user_id, "st": status,
                   "ia": 1 if status == "active" else 0})
            s.commit()

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
