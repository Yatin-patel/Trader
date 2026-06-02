"""Notification dispatcher.

Two entry points:
  * `dispatch(project_id, title, body, severity, event_type, payload)` — fans
    a single notification out to every enabled channel whose `events_filter`
    matches (or is None / empty = match-all).
  * `notify_event(project_id, event_type, event_payload)` — convenience that
    builds a friendly title/body from a known event_type and calls dispatch.
"""
from __future__ import annotations

import logging
from typing import Any

from db.notifications_repo import ChannelsRepo, NotificationsRepo

from .adapters import ADAPTERS

logger = logging.getLogger(__name__)


_DEFAULT_SUBSCRIBE = {"KILL_SWITCH", "ERROR", "EXECUTE", "BUY_TO_CLOSE",
                      "CLOSE_FOR_ROLL", "RESET_PAPER", "DIGEST"}


def _channel_subscribes(channel: dict[str, Any], event_type: str | None) -> bool:
    if not event_type:
        return True
    f = channel.get("events_filter")
    if not f:
        return event_type in _DEFAULT_SUBSCRIBE
    if isinstance(f, list):
        return event_type in f or "*" in f
    return True


def dispatch(project_id: str, title: str, body: str | None = None,
             severity: str = "info", event_type: str | None = None,
             payload: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Fan-out to every enabled channel subscribed to this event_type.
    Always writes an in-app notification row."""
    results: list[dict[str, Any]] = []

    # Always record one in-app row (so the bell shows it).
    in_app_id = NotificationsRepo.create(
        project_id=project_id, title=title, body=body,
        severity=severity, event_type=event_type, payload=payload,
        status="in_app",
    )
    results.append({"channel": "in_app", "notification_id": in_app_id,
                    "ok": True})

    channels = ChannelsRepo.list(project_id, enabled_only=True)
    for ch in channels:
        if ch["channel_type"] == "in_app":
            continue   # in-app handled above
        if not _channel_subscribes(ch, event_type):
            continue
        nid = NotificationsRepo.create(
            project_id=project_id, title=title, body=body,
            severity=severity, event_type=event_type, payload=payload,
            channel_id=ch["channel_id"], status="queued",
        )
        adapter = ADAPTERS.get(ch["channel_type"])
        if adapter is None:
            NotificationsRepo.mark_sent(nid, ok=False,
                                        error=f"no adapter for {ch['channel_type']}")
            results.append({"channel": ch["channel_type"], "ok": False,
                            "error": "no adapter", "notification_id": nid})
            continue
        try:
            ok, err = adapter(ch, title, body or "", severity)
        except Exception as e:
            ok, err = False, str(e)
        NotificationsRepo.mark_sent(nid, ok=ok, error=err)
        ChannelsRepo.record_send(ch["channel_id"], ok, error=err)
        results.append({"channel": ch["channel_type"], "ok": ok,
                        "error": err, "notification_id": nid})
    return results


# Map event_type to (title, severity, body-builder)

def _format_kill_switch(payload):
    return ("🛑 Kill switch breached: " + str(payload.get("limit_type", "?")),
            "critical",
            f"Observed {payload.get('observed_value')} vs threshold "
            f"{payload.get('threshold')}. Action: {payload.get('action')}.")


def _format_error(payload):
    err = payload.get("err") or payload.get("error") or "unknown"
    return ("Trader error", "error", str(err)[:1000])


def _format_execute(payload):
    results = payload.get("results") or []
    statuses: dict[str, int] = {}
    for r in results:
        s = r.get("status", "?")
        statuses[s] = statuses.get(s, 0) + 1
    tickers = list({(r.get("trade") or {}).get("ticker", "?") for r in results})
    title = f"Orders submitted: {', '.join(tickers[:5])}"
    body = "\n".join(f"  {k}: {v}" for k, v in statuses.items())
    return (title, "info", body)


def _format_buy_to_close(payload):
    ticker = payload.get("ticker", "?")
    title = f"Bought to close: {ticker}"
    body = (f"Sold at ${payload.get('premium_open', 0)} → "
            f"buying at ${payload.get('current_mid', 0)}. "
            f"Captured {(payload.get('profit_pct_so_far', 0) or 0) * 100:.0f}% "
            f"of max profit.")
    return (title, "info", body)


def _format_close_for_roll(payload):
    return (f"Auto-roll: {payload.get('ticker', '?')}",
            "info",
            f"Closed at ${payload.get('close_price', 0)} "
            f"({payload.get('dte', '?')} DTE). Strategist will reopen next cycle.")


def _format_reset_paper(payload):
    return ("Paper account reset", "info",
            f"Cash set to ${payload.get('cash', 0):,.0f}.")


_FORMATTERS = {
    "KILL_SWITCH":     _format_kill_switch,
    "ERROR":           _format_error,
    "EXECUTE":         _format_execute,
    "BUY_TO_CLOSE":    _format_buy_to_close,
    "CLOSE_FOR_ROLL":  _format_close_for_roll,
    "RESET_PAPER":     _format_reset_paper,
}


def notify_event(project_id: str, event_type: str,
                 event_payload: dict[str, Any]) -> list[dict[str, Any]]:
    fmt = _FORMATTERS.get(event_type)
    if fmt is None:
        title = f"{event_type}"
        severity = "info"
        body = None
    else:
        title, severity, body = fmt(event_payload or {})
    return dispatch(project_id, title, body, severity=severity,
                    event_type=event_type, payload=event_payload)
