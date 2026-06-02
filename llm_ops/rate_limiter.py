"""Sliding-window rate limiter for LLM calls (Cat 8.2).

Keeps per-model request timestamps in memory. Returns False if a request
would exceed the configured limit; the caller can then back off / skip.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from typing import Deque

from db.settings_store import AppSettings

# Free-tier defaults — override in settings if needed.
_DEFAULT_RPM: dict[str, int] = {
    "gemini-2.5-flash":      10,
    "gemini-2.5-flash-lite": 30,
    "gemini-2.5-pro":         5,
    "gemini-2.0-flash":      15,
}
_DEFAULT_RPD: dict[str, int] = {
    "gemini-2.5-flash":      250,
    "gemini-2.5-flash-lite": 1000,
    "gemini-2.5-pro":        50,
    "gemini-2.0-flash":      1500,
}

_lock = threading.Lock()
_minute_windows: dict[str, Deque[float]] = {}
_day_windows: dict[str, Deque[float]] = {}


def _setting(key: str, fallback: int) -> int:
    try:
        v = AppSettings.get(key)
        if v is not None:
            return int(v)
    except Exception:
        pass
    return fallback


def allow(model: str) -> tuple[bool, str]:
    """Return (allowed, reason). Records the call time when allowed."""
    rpm = _setting(f"rpm_{model}", _DEFAULT_RPM.get(model, 60))
    rpd = _setting(f"rpd_{model}", _DEFAULT_RPD.get(model, 10000))
    now = time.time()
    with _lock:
        m_dq = _minute_windows.setdefault(model, deque())
        d_dq = _day_windows.setdefault(model, deque())
        cutoff_60 = now - 60
        cutoff_day = now - 86400
        while m_dq and m_dq[0] < cutoff_60:
            m_dq.popleft()
        while d_dq and d_dq[0] < cutoff_day:
            d_dq.popleft()
        if len(m_dq) >= rpm:
            return (False, f"rate-limit {len(m_dq)}/{rpm} per minute")
        if len(d_dq) >= rpd:
            return (False, f"rate-limit {len(d_dq)}/{rpd} per day")
        m_dq.append(now)
        d_dq.append(now)
    return (True, "")


def reset_window(model: str | None = None) -> None:
    with _lock:
        if model is None:
            _minute_windows.clear()
            _day_windows.clear()
        else:
            _minute_windows.pop(model, None)
            _day_windows.pop(model, None)
