"""Three transactional emails for the user-approval flow.

Reuses the SMTP machinery wired up for password reset (Gmail SMTP via
AppSettings). Every email is best-effort — if SMTP isn't configured or
delivery fails, we log and return False. The signup flow does NOT
block on email send because that would let an SMTP outage cause user
registration to fail.
"""
from __future__ import annotations

import logging
from typing import Any

from db.settings_store import AppSettings

logger = logging.getLogger(__name__)


def _smtp_channel(to_addr: str) -> dict[str, Any] | None:
    host = AppSettings.get("smtp_host")
    if not host:
        return None
    return {
        "target": to_addr,
        "config": {
            "smtp_host": host,
            "smtp_port": AppSettings.get("smtp_port", default=587),
            "smtp_user": AppSettings.get("smtp_user"),
            "smtp_password": AppSettings.get("smtp_password"),
            "from": (AppSettings.get("smtp_from")
                     or AppSettings.get("smtp_user")),
            "use_tls": True,
        },
    }


def _send(to_addr: str, title: str, body: str,
          severity: str = "info") -> tuple[bool, str | None]:
    channel = _smtp_channel(to_addr)
    if channel is None:
        return (False, "SMTP not configured")
    from notifications.adapters import send_email
    try:
        return send_email(channel, title, body, severity)
    except Exception as e:
        logger.exception("approval email send failed for %s: %s",
                         to_addr, e)
        return (False, str(e))


def notify_admin_pending(admin_email: str, new_email: str,
                          approval_url: str) -> tuple[bool, str | None]:
    """Email sent to every active admin when a new user signs up."""
    title = f"New signup pending review: {new_email}"
    body = (
        f"A new user has signed up and is waiting for review.\n\n"
        f"Email: {new_email}\n\n"
        f"Approve or reject from the admin panel:\n\n"
        f"  {approval_url}\n\n"
        f"Until you approve, the user cannot log in. If you reject, the "
        f"email is permanently blocked from re-registering.\n"
    )
    return _send(admin_email, title, body, "info")


def notify_user_pending(new_email: str) -> tuple[bool, str | None]:
    """Email sent to the new user immediately after signup."""
    title = "Your Trader account is under review"
    body = (
        f"Thanks for signing up.\n\n"
        f"Your account ({new_email}) is currently being reviewed by an "
        f"admin. You'll receive another email as soon as a decision is "
        f"made.\n\n"
        f"Until then, you won't be able to log in.\n"
    )
    return _send(new_email, title, body, "info")


def notify_user_approved(user_email: str, login_url: str
                          ) -> tuple[bool, str | None]:
    """Email sent to the user when an admin approves their account."""
    title = "Your Trader account has been approved"
    body = (
        f"Good news — your account ({user_email}) has been approved.\n\n"
        f"You can sign in here:\n\n"
        f"  {login_url}\n\n"
        f"Welcome aboard.\n"
    )
    return _send(user_email, title, body, "info")


def notify_user_rejected(user_email: str) -> tuple[bool, str | None]:
    """Email sent to the user when an admin rejects their account."""
    title = "Your Trader account application was not approved"
    body = (
        f"Thanks for your interest in Trader.\n\n"
        f"We're unable to approve your account ({user_email}) at this "
        f"time. This decision is final; the email cannot be used to "
        f"sign up again.\n\n"
        f"If you believe this is an error, contact the administrator.\n"
    )
    return _send(user_email, title, body, "warn")
