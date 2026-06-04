"""Self-service password reset via emailed token.

Flow:
  1. User clicks 'Forgot password?' on /login, enters their email.
  2. /forgot route looks up the user and, if found, issues a time-limited
     URL-safe token bound to (user_id, password_hash_fingerprint). The
     hash fingerprint means the token is invalidated automatically the
     moment the password is changed (you can't replay an old token after
     a successful reset).
  3. Token is embedded in a reset URL and emailed via the EXISTING
     notifications/adapters.send_email machinery. We don't add new
     SMTP infrastructure — we reuse what the notifications system uses.
  4. User clicks the link → /reset/<token> page → enters a new password.
  5. On submit, token is validated, the new hash is written, ALL
     existing sessions for that user are revoked (forced re-login on
     every device), and an audit event is logged.

Privacy:
  * /forgot does NOT reveal whether the email is registered. The page
    always says "if an account exists, a reset link has been sent".
    Otherwise the form becomes an account-enumeration oracle.

Token security:
  * Bound to the CURRENT password hash so it can't be reused after a
    successful reset.
  * 1-hour expiry (configurable via AppSettings).
  * Signed with SECRET_ENCRYPTION_KEY so a database leak doesn't let an
    attacker forge tokens.
"""
from __future__ import annotations

import hashlib
import logging
import os
from typing import Any

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import text

from auth.passwords import hash_password
from auth.repositories import UsersRepo
from auth.sessions import revoke_all_for_user
from db.connection import session_scope
from db.settings_store import AppSettings

logger = logging.getLogger(__name__)

# Token namespace — prevents a token issued by one feature from being
# accepted by another (e.g. the 2FA-pending tokens).
_SALT = "trader.password-reset.v1"
# Default 1 hour; override with AppSettings 'password_reset_ttl_seconds'.
_DEFAULT_TTL = 3600


def _serializer() -> URLSafeTimedSerializer:
    key = os.getenv("SECRET_ENCRYPTION_KEY") or "DEV_INSECURE_KEY"
    return URLSafeTimedSerializer(key, salt=_SALT)


def _fingerprint(password_hash: str | None) -> str:
    """Short stable digest of the current password hash, used to bind a
    reset token to a SPECIFIC version of the password. Once the user
    successfully resets, the fingerprint changes and any outstanding
    reset link becomes invalid (prevents replay)."""
    return hashlib.sha256(
        (password_hash or "").encode("utf-8")
    ).hexdigest()[:16]


def make_reset_token(user_id: str) -> str:
    """Mint a signed reset token for the given user."""
    h = UsersRepo.get_password_hash(user_id) or ""
    payload = {"u": user_id, "f": _fingerprint(h)}
    return _serializer().dumps(payload)


def consume_reset_token(token: str) -> dict[str, Any] | None:
    """Validate a token. Returns {user_id} on success, None on any failure.

    Failure modes (all return None silently to avoid leaking info):
      * malformed / wrong-key signature
      * expired
      * password has been changed since the token was issued
      * user no longer exists or is inactive
    """
    try:
        ttl = int(AppSettings.get("password_reset_ttl_seconds",
                                  default=_DEFAULT_TTL) or _DEFAULT_TTL)
    except Exception:
        ttl = _DEFAULT_TTL
    try:
        payload = _serializer().loads(token, max_age=ttl)
    except SignatureExpired:
        logger.info("password reset token expired")
        return None
    except BadSignature:
        logger.info("password reset token signature invalid")
        return None
    except Exception:
        logger.exception("unexpected token parse error")
        return None

    user_id = str(payload.get("u") or "")
    expected_fp = str(payload.get("f") or "")
    if not user_id or not expected_fp:
        return None
    user = UsersRepo.get_by_id(user_id)
    if user is None or not user.is_active:
        return None
    current_fp = _fingerprint(UsersRepo.get_password_hash(user_id))
    if current_fp != expected_fp:
        # Token was issued against a different hash version — i.e.
        # the password has already been changed since. Don't allow replay.
        return None
    return {"user_id": user_id, "email": user.email}


def apply_new_password(user_id: str, new_password: str) -> tuple[bool, str]:
    """Hash + persist + revoke all sessions. Returns (ok, message)."""
    if not new_password or len(new_password) < 10:
        return (False, "password must be at least 10 characters")
    try:
        new_hash = hash_password(new_password)
        UsersRepo.update_password(user_id, new_hash)
        revoked = revoke_all_for_user(user_id)
        # Audit event — stored in the agent_events table without a
        # project_id (NULL means global).
        try:
            with session_scope() as s:
                s.execute(text("""
                    INSERT INTO agent_events
                        (project_id, node_name, event_type, payload)
                    VALUES (NULL, 'Auth', 'PASSWORD_RESET',
                            JSON_OBJECT('user_id', :u, 'sessions_revoked', :r))
                """), {"u": user_id, "r": int(revoked)})
                s.commit()
        except Exception:
            logger.exception("audit insert failed (non-fatal)")
        return (True, "password updated")
    except Exception as e:
        logger.exception("apply_new_password failed")
        return (False, f"could not save password: {e}")


# ---------------------------------------------------------------------------
# Email delivery — reuse the SMTP machinery from notifications/adapters.
# ---------------------------------------------------------------------------
def _smtp_channel_from_app_settings() -> dict[str, Any] | None:
    """Construct a one-off 'channel' dict (the shape notifications.adapters
    expects) from global AppSettings. Returns None if SMTP isn't
    configured at the app level."""
    host = AppSettings.get("smtp_host")
    if not host:
        return None
    return {
        "config": {
            "smtp_host": host,
            "smtp_port": AppSettings.get("smtp_port", default=587),
            "smtp_user": AppSettings.get("smtp_user"),
            "smtp_password": AppSettings.get("smtp_password"),
            "from": AppSettings.get("smtp_from") or AppSettings.get("smtp_user"),
            "use_tls": True,
        },
    }


def send_reset_email(to_email: str, reset_url: str) -> tuple[bool, str]:
    """Send the reset email. Returns (ok, message)."""
    channel = _smtp_channel_from_app_settings()
    if channel is None:
        return (False, "SMTP not configured at app level — set "
                       "smtp_host/smtp_user/smtp_password/smtp_from in "
                       "Global Settings.")
    channel["target"] = to_email
    body = (
        "We received a request to reset the password on your trader "
        "account.\n\n"
        f"Reset your password using the link below (expires in 1 hour):\n\n"
        f"  {reset_url}\n\n"
        "If you didn't request this, ignore this email — your password "
        "won't change.\n"
    )
    from notifications.adapters import send_email
    return send_email(channel, "Password reset", body, "info")
