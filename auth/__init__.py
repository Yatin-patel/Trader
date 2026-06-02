"""Authentication module — email/password login + session-cookie auth.

Public API:
    hash_password / verify_password   — argon2 wrappers
    create_session / get_session / revoke_session
    UsersRepo                         — DB access for users
    current_user_optional / require_user — FastAPI dependencies
"""
from .passwords import hash_password, verify_password
from .sessions import (
    create_session, get_session, revoke_session, revoke_all_for_user,
    revoke_others_for_user, list_for_user,
    SESSION_COOKIE, SESSION_TTL_HOURS,
)
from .repositories import UsersRepo, AuthUser
from .dependencies import current_user_optional, require_user
from . import totp

__all__ = [
    "hash_password", "verify_password",
    "create_session", "get_session", "revoke_session", "revoke_all_for_user",
    "SESSION_COOKIE", "SESSION_TTL_HOURS",
    "UsersRepo", "AuthUser",
    "current_user_optional", "require_user",
    "totp",
]
