"""US economic-event calendar — market-wide schedule risk awareness.

The agents avoid opening new positions in the days surrounding these
events because they create binary intraday vol that can break a tight
delta/IV thesis. Specifically:

  * FOMC meeting days (rate decisions / dot-plot) — typically 2pm ET
    statement release.
  * CPI release days (8:30am ET) — biggest single-day moves usually
    happen in the first 15 min after the release.
  * NFP / jobs report (8:30am ET, first Friday of the month).
  * PCE inflation (10am ET, late month).

Why a static table instead of fetching live: the BLS / Fed publish these
schedules a year ahead. An offline list is more reliable than scraping a
site that might break. Update annually when the next year's calendar
publishes (Fed releases in summer; BLS in autumn).

Public API:
  next_events(within_days, kinds=None) -> list[dict]
  is_event_within(within_days, kinds=None) -> bool
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Iterable


@dataclass(frozen=True)
class EconEvent:
    kind: str                # 'fomc' | 'cpi' | 'nfp' | 'pce'
    date_: date
    label: str               # human-friendly description


# ---------------------------------------------------------------------------
# 2026 schedule — Fed FOMC meetings (statement on day 2), BLS CPI/NFP/PCE.
# Source: federalreserve.gov + bls.gov release schedules.
# Update annually; the list is small enough to keep in code rather than DB.
# ---------------------------------------------------------------------------
_EVENTS_2026: tuple[EconEvent, ...] = (
    # ----- FOMC (rate decisions) -----
    EconEvent("fomc", date(2026, 1, 28),  "FOMC rate decision (Jan)"),
    EconEvent("fomc", date(2026, 3, 18),  "FOMC rate decision (Mar)"),
    EconEvent("fomc", date(2026, 4, 29),  "FOMC rate decision (Apr)"),
    EconEvent("fomc", date(2026, 6, 17),  "FOMC rate decision (Jun)"),
    EconEvent("fomc", date(2026, 7, 29),  "FOMC rate decision (Jul)"),
    EconEvent("fomc", date(2026, 9, 16),  "FOMC rate decision (Sep)"),
    EconEvent("fomc", date(2026, 10, 28), "FOMC rate decision (Oct)"),
    EconEvent("fomc", date(2026, 12, 9),  "FOMC rate decision (Dec)"),

    # ----- CPI (Consumer Price Index, 8:30am ET) -----
    EconEvent("cpi", date(2026, 1, 14),  "CPI report (Dec 2025 data)"),
    EconEvent("cpi", date(2026, 2, 11),  "CPI report (Jan)"),
    EconEvent("cpi", date(2026, 3, 11),  "CPI report (Feb)"),
    EconEvent("cpi", date(2026, 4, 14),  "CPI report (Mar)"),
    EconEvent("cpi", date(2026, 5, 13),  "CPI report (Apr)"),
    EconEvent("cpi", date(2026, 6, 11),  "CPI report (May)"),
    EconEvent("cpi", date(2026, 7, 15),  "CPI report (Jun)"),
    EconEvent("cpi", date(2026, 8, 12),  "CPI report (Jul)"),
    EconEvent("cpi", date(2026, 9, 11),  "CPI report (Aug)"),
    EconEvent("cpi", date(2026, 10, 14), "CPI report (Sep)"),
    EconEvent("cpi", date(2026, 11, 13), "CPI report (Oct)"),
    EconEvent("cpi", date(2026, 12, 10), "CPI report (Nov)"),

    # ----- NFP / Employment Situation (first Friday, 8:30am ET) -----
    EconEvent("nfp", date(2026, 1, 9),   "Jobs report (Dec 2025)"),
    EconEvent("nfp", date(2026, 2, 6),   "Jobs report (Jan)"),
    EconEvent("nfp", date(2026, 3, 6),   "Jobs report (Feb)"),
    EconEvent("nfp", date(2026, 4, 3),   "Jobs report (Mar)"),
    EconEvent("nfp", date(2026, 5, 1),   "Jobs report (Apr)"),
    EconEvent("nfp", date(2026, 6, 5),   "Jobs report (May)"),
    EconEvent("nfp", date(2026, 7, 2),   "Jobs report (Jun)"),
    EconEvent("nfp", date(2026, 8, 7),   "Jobs report (Jul)"),
    EconEvent("nfp", date(2026, 9, 4),   "Jobs report (Aug)"),
    EconEvent("nfp", date(2026, 10, 2),  "Jobs report (Sep)"),
    EconEvent("nfp", date(2026, 11, 6),  "Jobs report (Oct)"),
    EconEvent("nfp", date(2026, 12, 4),  "Jobs report (Nov)"),

    # ----- PCE (Personal Consumption Expenditures, Fed's preferred metric) -----
    EconEvent("pce", date(2026, 1, 30),  "PCE inflation (Dec 2025)"),
    EconEvent("pce", date(2026, 2, 27),  "PCE inflation (Jan)"),
    EconEvent("pce", date(2026, 3, 27),  "PCE inflation (Feb)"),
    EconEvent("pce", date(2026, 4, 30),  "PCE inflation (Mar)"),
    EconEvent("pce", date(2026, 5, 29),  "PCE inflation (Apr)"),
    EconEvent("pce", date(2026, 6, 26),  "PCE inflation (May)"),
    EconEvent("pce", date(2026, 7, 31),  "PCE inflation (Jun)"),
    EconEvent("pce", date(2026, 8, 28),  "PCE inflation (Jul)"),
    EconEvent("pce", date(2026, 9, 25),  "PCE inflation (Aug)"),
    EconEvent("pce", date(2026, 10, 30), "PCE inflation (Sep)"),
    EconEvent("pce", date(2026, 11, 25), "PCE inflation (Oct)"),
    EconEvent("pce", date(2026, 12, 23), "PCE inflation (Nov)"),
)


# Default major-events set used by the wheel filter. PCE is excluded here
# because its market impact is much smaller than CPI/FOMC/NFP. Override
# per-project by setting `skip_on_pce_days` to True.
_MAJOR_KINDS: frozenset[str] = frozenset({"fomc", "cpi", "nfp"})


def next_events(within_days: int,
                kinds: Iterable[str] | None = None) -> list[EconEvent]:
    """Return events occurring in the next ``within_days`` calendar days
    (including today). Filtered by ``kinds`` if provided."""
    today = datetime.now(tz=timezone.utc).date()
    cutoff = today + timedelta(days=int(within_days))
    wanted = set(kinds) if kinds else _MAJOR_KINDS
    return [e for e in _EVENTS_2026
            if today <= e.date_ <= cutoff and e.kind in wanted]


def is_event_within(within_days: int,
                    kinds: Iterable[str] | None = None) -> tuple[bool, str]:
    """Return (True, label) if any matching event falls inside the window,
    else (False, '')."""
    upcoming = next_events(within_days, kinds)
    if not upcoming:
        return (False, "")
    e = upcoming[0]
    return (True, f"{e.label} on {e.date_.isoformat()}")
