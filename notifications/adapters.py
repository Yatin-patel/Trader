"""Channel adapters — each takes (channel, title, body, severity) → (ok, error)."""
from __future__ import annotations

import json
import logging
import smtplib
import ssl
from email.message import EmailMessage
from typing import Any

import httpx

logger = logging.getLogger(__name__)


def send_in_app(channel: dict[str, Any], title: str, body: str,
                severity: str) -> tuple[bool, str | None]:
    # In-app is just a DB record; the notifications row is the message.
    return (True, None)


def send_discord(channel: dict[str, Any], title: str, body: str,
                 severity: str) -> tuple[bool, str | None]:
    url = (channel.get("target") or "").strip()
    if not url:
        return (False, "no webhook URL configured")
    color = {"info": 0x4f8cff, "warn": 0xffc800,
             "error": 0xff5c7a, "critical": 0xff0000}.get(severity, 0x4f8cff)
    embed = {
        "title": title[:256],
        "description": (body or "")[:4000],
        "color": color,
    }
    payload = {"username": "Trader", "embeds": [embed]}
    try:
        with httpx.Client(timeout=10) as c:
            r = c.post(url, json=payload)
            if r.status_code >= 400:
                return (False, f"HTTP {r.status_code}: {r.text[:200]}")
            return (True, None)
    except Exception as e:
        return (False, f"discord error: {e}")


def send_slack(channel: dict[str, Any], title: str, body: str,
               severity: str) -> tuple[bool, str | None]:
    url = (channel.get("target") or "").strip()
    if not url:
        return (False, "no webhook URL configured")
    icon = {"info": ":information_source:", "warn": ":warning:",
            "error": ":no_entry:", "critical": ":rotating_light:"}.get(severity, "")
    text_body = f"{icon} *{title}*\n{body or ''}"
    try:
        with httpx.Client(timeout=10) as c:
            r = c.post(url, json={"text": text_body})
            if r.status_code >= 400:
                return (False, f"HTTP {r.status_code}: {r.text[:200]}")
            return (True, None)
    except Exception as e:
        return (False, f"slack error: {e}")


def send_email(channel: dict[str, Any], title: str, body: str,
               severity: str) -> tuple[bool, str | None]:
    cfg = channel.get("config") or {}
    host = cfg.get("smtp_host")
    port = int(cfg.get("smtp_port") or 587)
    user = cfg.get("smtp_user")
    password = cfg.get("smtp_password")
    from_addr = cfg.get("from") or user
    use_tls = bool(cfg.get("use_tls", True))
    to_addr = (channel.get("target") or "").strip()
    if not host or not from_addr or not to_addr:
        return (False, "email config incomplete (need smtp_host, from, and target)")

    msg = EmailMessage()
    msg["Subject"] = f"[Trader · {severity.upper()}] {title}"
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg.set_content(body or title)

    try:
        if use_tls:
            ctx = ssl.create_default_context()
            with smtplib.SMTP(host, port, timeout=15) as s:
                s.starttls(context=ctx)
                if user and password:
                    s.login(user, password)
                s.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=15) as s:
                if user and password:
                    s.login(user, password)
                s.send_message(msg)
        return (True, None)
    except Exception as e:
        return (False, f"email error: {e}")


ADAPTERS = {
    "in_app":  send_in_app,
    "discord": send_discord,
    "slack":   send_slack,
    "email":   send_email,
}
