"""Pull real broker fees and stamp them onto closed_contracts rows.

Broker-derived approach (vs the simpler rate-based approximation):
- Alpaca surfaces per-order regulatory + commission fees via
  ``/v2/account/activities`` (activity_type=FILL/FEE). The trading
  account is otherwise commission-free for retail options, so the
  fee per trade typically comes out to the small ORF/OCC reg charges.
- ETrade surfaces commission + fee per transaction on
  ``/v1/accounts/{accountIdKey}/transactions``. Real-money options
  trades show $0.65/contract per side (less at higher trade volumes).

Matching strategy:
1. Read each project's closed_contracts rows with brokerage_fee IS NULL
   and closed_at older than ~5 min (gives the broker time to settle).
2. Fetch broker activity / transactions covering the same window.
3. Match each fee row to a closure by (option_symbol, fill timestamp
   within ±30 min of closed_at). Use OCC↔ETrade symbol conversion
   for ETrade.
4. UPDATE closed_contracts.brokerage_fee + fee_synced_at.

Each call is bounded (~500 closures, ~250 broker rows) so a single
sync invocation can't run away. The scheduler calls us every 15 min.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from db.analytics_repos import ClosedContractsRepo
from db.repositories import EventsRepo, ProjectsRepo

logger = logging.getLogger(__name__)


# Window we accept between a closed_contracts.closed_at and a broker
# fee timestamp. Brokers can lag the trade fill by a few minutes during
# busy periods.
_MATCH_WINDOW = timedelta(minutes=30)


def sync_fees_for_project(project_id: str) -> dict[str, Any]:
    """Fetch broker fees for all closed_contracts rows in the project
    that still have brokerage_fee = NULL. Returns a summary dict the
    scheduler can log."""
    project = ProjectsRepo.get(project_id)
    if project is None:
        return {"status": "no_project", "matched": 0}
    pending = ClosedContractsRepo.list_pending_sync(project_id)
    if not pending:
        return {"status": "nothing_pending", "matched": 0}

    broker_type = (getattr(project, "broker_type", "alpaca") or "alpaca").lower()
    try:
        if broker_type == "alpaca":
            matched = _sync_alpaca(project, pending)
        elif broker_type == "etrade":
            matched = _sync_etrade(project, pending)
        else:
            logger.info("fees sync: unknown broker_type %r", broker_type)
            return {"status": "unknown_broker", "matched": 0}
    except Exception as e:
        logger.exception("fees sync failed for %s: %s", project_id, e)
        EventsRepo.log(project_id, "FeesSync", "ERROR", {"err": str(e)[:300]})
        return {"status": "error", "matched": 0, "err": str(e)[:300]}

    if matched:
        EventsRepo.log(project_id, "FeesSync", "OK", {
            "broker": broker_type,
            "pending_before": len(pending),
            "matched": matched,
        })
    return {"status": "ok", "matched": matched,
            "pending_before": len(pending)}


# ---------------------------------------------------------------------------
# Alpaca
# ---------------------------------------------------------------------------

def _sync_alpaca(project: Any, pending: list[dict[str, Any]]) -> int:
    """Match Alpaca activities to pending closures and stamp the fee."""
    # Window: from the oldest pending closure to now.
    oldest = min(_as_utc(c["closed_at"]) for c in pending if c.get("closed_at"))
    after = (oldest - _MATCH_WINDOW).date().isoformat()

    base = (project.alpaca_base_url
            or "https://paper-api.alpaca.markets").rstrip("/")
    url = f"{base}/v2/account/activities"
    headers = {
        "APCA-API-KEY-ID":     project.alpaca_api_key,
        "APCA-API-SECRET-KEY": project.alpaca_secret_key,
        "Accept":              "application/json",
    }
    import requests as _requests
    try:
        # Paginate up to MAX_PAGES (= 5 × 100-row pages = 500
        # activities). Alpaca returns activities newest-first; we stop
        # once we've covered the requested window OR hit MAX_PAGES.
        MAX_PAGES = 5
        rows: list[dict[str, Any]] = []
        page_token: str | None = None
        for _page in range(MAX_PAGES):
            params: dict[str, Any] = {
                # Alpaca's valid enum doesn't include SEC/ORF — reg +
                # exchange fees come bundled under FEE. FILL gives us
                # the symbol + transaction time used to bucket fees.
                "activity_types": "FILL,JNLC,FEE",
                "after":          after,
                # Alpaca caps page_size at 100 — /v2/account/activities
                # rejects >100 with HTTP 422.
                "page_size":      100,
                "direction":      "desc",
            }
            if page_token:
                params["page_token"] = page_token
            r = _requests.get(
                url, params=params, headers=headers, timeout=20,
            )
            if r.status_code != 200:
                logger.warning("alpaca activities HTTP %s: %s",
                               r.status_code, r.text[:200])
                break
            try:
                page = r.json() or []
            except Exception:
                page = []
            if not page:
                break
            rows.extend(page)
            if len(page) < 100:
                break  # final page
            # Alpaca's continuation token = the id of the last row.
            last_id = page[-1].get("id")
            if not last_id:
                break
            page_token = str(last_id)
    except Exception as e:
        logger.warning("alpaca activities fetch failed: %s", e)
        return 0

    # Bucket fees AND seen-fills by (symbol, day). FILL rows tell us
    # the broker has acknowledged the trade — even if no FEE row
    # exists, we can stamp $0 instead of leaving NULL forever (which
    # is what happens on commission-free paper accounts).
    fees: dict[tuple[str, str], float] = {}
    seen_fills: set[tuple[str, str]] = set()
    for row in rows:
        sym = (row.get("symbol") or "").upper()
        if not sym:
            continue
        ts = row.get("transaction_time") or row.get("date")
        if not ts:
            continue
        try:
            t = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        except Exception:
            continue
        key = (sym, t.date().isoformat())
        atype = (row.get("activity_type") or "").upper()
        if atype == "FILL":
            seen_fills.add(key)
            continue
        # FEE / JNLC: fees show under 'net_amount' (negative for paid)
        # or 'commission'. Treat absolute value as the fee.
        net = row.get("net_amount") or row.get("commission") or 0
        try:
            net = float(net)
        except Exception:
            continue
        if net == 0:
            continue
        fees[key] = fees.get(key, 0.0) + abs(net)

    # Old closures whose FILL has rolled off Alpaca's activity window
    # (the endpoint typically only returns ~30 days) will never get
    # matched by symbol+date lookup. For Alpaca paper accounts +
    # standard retail options accounts, commissions are $0 and reg
    # fees are sub-cent, so stamping $0 for any closure older than
    # 30 days that's still NULL is safe + closes the loop. The
    # alternative is leaving them perpetually "pending" in the UI.
    now = datetime.now(tz=timezone.utc)
    backfill_cutoff = now - timedelta(days=30)

    matched = 0
    for c in pending:
        sym = (c.get("option_symbol") or "").upper()
        ts = _as_utc(c["closed_at"])
        if not sym or ts is None:
            continue
        key = (sym, ts.date().isoformat())
        if key in fees:
            ClosedContractsRepo.set_fee(c["closure_id"], fees[key])
            matched += 1
        elif key in seen_fills:
            # Broker saw the fill but charged $0 — stamp it so the UI
            # stops showing "pending" forever on commission-free accts.
            ClosedContractsRepo.set_fee(c["closure_id"], 0.0)
            matched += 1
        elif ts < backfill_cutoff:
            # Too old for Alpaca's activities feed to return — assume
            # $0 (Alpaca standard options pricing is commission-free).
            ClosedContractsRepo.set_fee(c["closure_id"], 0.0)
            matched += 1
    return matched


# ---------------------------------------------------------------------------
# ETrade
# ---------------------------------------------------------------------------

def _sync_etrade(project: Any, pending: list[dict[str, Any]]) -> int:
    """Match ETrade transactions to pending closures and stamp the fee."""
    from execution import BrokerReauthRequired, get_broker
    try:
        client = get_broker(project)
    except BrokerReauthRequired:
        return 0
    aid = getattr(client, "_require_account", lambda: None)()
    if not aid:
        return 0

    base = client._base
    headers = {"Accept": "application/json"}
    # Limit fetch window from oldest pending closure to now.
    oldest = min(_as_utc(c["closed_at"]) for c in pending if c.get("closed_at"))
    start_ms = int(((oldest - _MATCH_WINDOW)
                    .replace(tzinfo=timezone.utc)
                    .timestamp() * 1000)
                   if oldest else 0)

    try:
        r = client._get(
            f"{base}/v1/accounts/{aid}/transactions.json",
            params={"count": 250, "startDate": _ms_to_etrade_date(start_ms)},
            headers=headers, timeout=20,
        )
    except Exception as e:
        logger.warning("etrade transactions fetch failed: %s", e)
        return 0
    if r.status_code != 200:
        logger.warning("etrade transactions HTTP %s: %s",
                       r.status_code, r.text[:200])
        return 0
    try:
        body = r.json()
    except Exception:
        return 0
    wrap = body.get("TransactionListResponse") or body
    txns = ((wrap.get("Transaction") or wrap.get("transaction") or []))
    if isinstance(txns, dict):
        txns = [txns]

    # Bucket ETrade fees by (occ_symbol, day). ETrade exposes
    # ``commission`` + ``fee`` per row + a ``transactionDate`` epoch-ms.
    fees: dict[tuple[str, str], float] = {}
    for t in txns:
        # ETrade uses its colon-delimited option symbol. Convert to OCC
        # to match our closed_contracts.option_symbol.
        occ = _etrade_txn_to_occ(t)
        if not occ:
            continue
        ts_ms = t.get("transactionDate")
        try:
            ts = (datetime.fromtimestamp(int(ts_ms) / 1000, tz=timezone.utc)
                  if ts_ms else None)
        except Exception:
            ts = None
        if ts is None:
            continue
        try:
            commission = float(t.get("commission") or 0)
        except Exception:
            commission = 0.0
        try:
            fee = float(t.get("fee") or 0)
        except Exception:
            fee = 0.0
        total = abs(commission) + abs(fee)
        if total == 0:
            continue
        key = (occ.upper(), ts.date().isoformat())
        fees[key] = fees.get(key, 0.0) + total

    matched = 0
    for c in pending:
        sym = (c.get("option_symbol") or "").upper()
        ts = _as_utc(c["closed_at"])
        if not sym or ts is None:
            continue
        key = (sym, ts.date().isoformat())
        if key in fees:
            ClosedContractsRepo.set_fee(c["closure_id"], fees[key])
            matched += 1
    return matched


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _as_utc(ts: Any) -> datetime | None:
    if ts is None:
        return None
    if isinstance(ts, datetime):
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    try:
        d = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _ms_to_etrade_date(ms: int) -> str:
    if ms <= 0:
        return ""
    d = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    return d.strftime("%m%d%Y")


_ETRADE_OPT_RE = re.compile(
    r"^([A-Z]{1,6}):(\d{4}):(\d{1,2}):(\d{1,2}):(CALL|PUT):"
    r"(\d+(?:\.\d+)?)$"
)


def _etrade_txn_to_occ(txn: dict[str, Any]) -> str | None:
    """Best-effort OCC symbol extraction from an ETrade transaction.

    The transaction rows carry a nested ``brokerage.product`` (or
    ``Product``) with symbol + expiry pieces + strikePrice. Fall back
    to scanning the human-readable ``description`` for an ETrade
    colon-delimited symbol if the structured field isn't present.
    """
    # Structured form.
    brk = txn.get("brokerage") or txn.get("Brokerage") or {}
    prod = brk.get("product") or brk.get("Product") or {}
    sym = (prod.get("symbol") or "").upper()
    if (prod.get("securityType") == "OPTN" and sym):
        try:
            yr = int(prod.get("expiryYear") or 0)
            mo = int(prod.get("expiryMonth") or 0)
            day = int(prod.get("expiryDay") or 0)
            cp = (prod.get("callPut") or "").upper()
            strike = float(prod.get("strikePrice") or 0)
            if yr and mo and day and cp and strike > 0:
                return _build_occ(sym, yr, mo, day, cp, strike)
        except Exception:
            pass
    # Fallback: description like "AAPL:2024:12:20:CALL:200.000000"
    desc = txn.get("description") or ""
    m = _ETRADE_OPT_RE.search(desc.upper())
    if m:
        root, yr, mo, day, cp, strike = m.groups()
        return _build_occ(root, int(yr), int(mo), int(day), cp, float(strike))
    return None


def _build_occ(root: str, year: int, month: int, day: int,
               call_put: str, strike: float) -> str:
    yy = year % 100
    cp = "C" if call_put.upper().startswith("C") else "P"
    strike_th = int(round(strike * 1000))
    return f"{root}{yy:02d}{month:02d}{day:02d}{cp}{strike_th:08d}"
