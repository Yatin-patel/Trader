"""Mode-coherence self-healer.

The continuous Optimizer Agent only ever proposes LLM-driven nudges to
already-working settings. It doesn't catch the class of bug where a
project is in a *structurally inconsistent* state — the user picked a
strategy_mode but didn't (or couldn't) set the toggles that mode needs
to actually trade. Two real-world cases this module fixes:

  1. ``intraday_momentum`` mode requires ``intraday_scanner_enabled=True``
     plus one of (``allow_0dte``, ``allow_1dte``). If the user changed
     strategy_mode but those toggles are still default-off, the scanner
     never produces signals and the executor never trades.

  2. ``contracts_per_csp`` × strike × 100 exceeds the per-ticker
     concentration cap, so the Guardrail rejects every cycle. The
     Strategist keeps picking the same ticker, the Guardrail keeps
     rejecting, no trades fire.

  3. Spread/calendar modes need their leg-sizing knobs populated
     (``spread_width``, calendar DTE pair). A 0 width means "no wing"
     which the strategy can't build.

  4. ETrade tokens have expired and the project still has
     strategy_mode != 'paused' — the worker cycles burn no-op events
     forever until the user re-OAuths. We log a clear actionable event
     so the dashboard surfaces "Reconnect ETrade".

Repairs that flip a default-valued setting to the value the mode
requires are applied automatically (we treat them as completing what
the mode-switch implied). Repairs that change a user-touched value are
NEVER applied — we just emit a clear advisory event so the user can
decide. The 24-hour manual-change cooldown from optimizer_agent is
respected via _recent_manual_change.

This runs as a pre-LLM step inside ``optimizer_agent.run_for_project``
so it gets the same scheduling cadence + ET-window gate the Optimizer
already has.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from db.repositories import EventsRepo, ProjectsRepo
from db.settings_store import ProjectSettings

logger = logging.getLogger(__name__)


_SPREAD_MODES = {
    "bull_put_spread", "bear_call_spread",
    "bull_call_spread", "bear_put_spread",
    "iron_condor",
}


def _user_touched(project_id: str, key: str, hours: int = 24 * 7) -> bool:
    """Did the user manually change this key in the last week?

    Default = 7 days so we don't auto-flip something the user thoughtfully
    chose just because they let our default sit for a while. The
    optimizer_agent's own MANUAL_CHANGE_COOLDOWN_HOURS is 24h; we use a
    longer window for structural settings because the cost of overwriting
    user intent is higher than the cost of leaving a small misconfig.
    """
    try:
        from intelligence.optimizer_agent import _recent_manual_change
        recent, _ = _recent_manual_change(project_id, key, hours)
        return bool(recent)
    except Exception:
        return False


def _apply(project_id: str, key: str, value: Any) -> None:
    """Set + log a Manual.SETTING_CHANGE on behalf of the self-healer.

    Tagging it as Manual (not Optimizer) means the standard 24-hour
    cooldown applies to anything we set — the LLM tuner won't try to
    revert it on the next cycle."""
    try:
        old = ProjectSettings.get(project_id, key)
    except Exception:
        old = None
    ProjectSettings.set(project_id, key, value)
    try:
        EventsRepo.log(project_id, "Manual", "SETTING_CHANGE", {
            "key": key, "old": old, "new": value,
            "source": "mode_coherence_self_heal",
        })
    except Exception:
        logger.exception("failed to log mode-coherence repair")


def check_and_repair(project_id: str) -> dict[str, Any]:
    """Run all coherence checks for ``project_id``. Returns a report dict.

    The report has:
        applied: list of {key, old, new, reason} for repairs we made
        advisories: list of {issue, narrative, suggested_fix} for things
                    we won't auto-fix (user touched the setting, or the
                    fix needs human action like re-OAuth)
    """
    applied: list[dict[str, Any]] = []
    advisories: list[dict[str, Any]] = []

    project = ProjectsRepo.get(project_id)
    if project is None:
        return {"applied": [], "advisories": []}

    mode = str(ProjectSettings.get(
        project_id, "strategy_mode", default="wheel") or "wheel").lower()

    # ----- 1. Intraday-momentum needs the scanner + a DTE allowance -----
    if mode == "intraday_momentum":
        scanner_on = bool(ProjectSettings.get(
            project_id, "intraday_scanner_enabled", default=False))
        if not scanner_on:
            if _user_touched(project_id, "intraday_scanner_enabled"):
                advisories.append({
                    "issue": "intraday_scanner_disabled_in_intraday_mode",
                    "narrative": (
                        "strategy_mode is 'intraday_momentum' but the "
                        "user explicitly disabled intraday_scanner_enabled "
                        "in the last 7 days. The scanner won't fire so no "
                        "intraday trades will open. Either re-enable the "
                        "scanner or switch to 'paused'."
                    ),
                })
            else:
                _apply(project_id, "intraday_scanner_enabled", True)
                applied.append({
                    "key": "intraday_scanner_enabled",
                    "old": False, "new": True,
                    "reason": ("required for intraday_momentum mode — "
                               "the scanner gates every intraday cycle"),
                })

        allow_0 = bool(ProjectSettings.get(
            project_id, "allow_0dte", default=False))
        allow_1 = bool(ProjectSettings.get(
            project_id, "allow_1dte", default=False))
        if not (allow_0 or allow_1):
            # Prefer 1DTE — lower variance than 0DTE. User can opt into
            # 0DTE explicitly if they want it.
            if _user_touched(project_id, "allow_1dte"):
                advisories.append({
                    "issue": "no_intraday_dte_allowance",
                    "narrative": (
                        "intraday_momentum mode is on but neither "
                        "allow_0dte nor allow_1dte is enabled, and the "
                        "user explicitly toggled allow_1dte off recently. "
                        "No day-trading opens will fire."
                    ),
                })
            else:
                _apply(project_id, "allow_1dte", True)
                applied.append({
                    "key": "allow_1dte", "old": False, "new": True,
                    "reason": ("intraday_momentum needs at least one DTE "
                               "allowance; 1DTE is the safer default"),
                })

        # 0 trades-per-cycle = the strategist never opens anything
        try:
            max_per_cycle = int(ProjectSettings.get(
                project_id, "intraday_max_trades_per_cycle",
                default=3) or 0)
        except Exception:
            max_per_cycle = 0
        if max_per_cycle <= 0 and not _user_touched(
                project_id, "intraday_max_trades_per_cycle"):
            _apply(project_id, "intraday_max_trades_per_cycle", 3)
            applied.append({
                "key": "intraday_max_trades_per_cycle",
                "old": max_per_cycle, "new": 3,
                "reason": "0 = strategist would never open a trade",
            })

    # ----- 2. Spread modes need spread_width > 0 ------------------------
    if mode in _SPREAD_MODES:
        try:
            width = float(ProjectSettings.get(
                project_id, "spread_width", default=0.0) or 0.0)
        except Exception:
            width = 0.0
        if width <= 0 and not _user_touched(project_id, "spread_width"):
            # The setup-Optimizer's tier-scaled default is the right
            # baseline; we don't know the tier here so use a balanced $5.
            _apply(project_id, "spread_width", 5.0)
            applied.append({
                "key": "spread_width", "old": width, "new": 5.0,
                "reason": ("spread modes need a non-zero leg distance / "
                           "wing width to build a setup"),
            })

    if mode == "calendar_spread":
        for dte_key, default_dte in (("calendar_short_dte", 14),
                                     ("calendar_long_dte", 45)):
            try:
                v = int(ProjectSettings.get(
                    project_id, dte_key, default=0) or 0)
            except Exception:
                v = 0
            if v <= 0 and not _user_touched(project_id, dte_key):
                _apply(project_id, dte_key, default_dte)
                applied.append({
                    "key": dte_key, "old": v, "new": default_dte,
                    "reason": ("calendar_spread requires a valid DTE for "
                               "both legs"),
                })

    # ----- 3. Wheel: Strategist→Guardrail concentration / collateral
    # block loop. Symptom: Guardrail RISK events with cleared=False
    # citing concentration_cap or collateral_cap, no Executor SUBMIT
    # events for the same ticker, repeating every cycle. Likely fix:
    # lower contracts_per_csp by 1 when the user hasn't touched it.
    if mode in ("wheel", "wheel_plus_dca"):
        report = _diagnose_wheel_block(project_id)
        if report:
            repaired = _try_repair_wheel_block(project_id, report)
            if repaired:
                applied.append(repaired)
            else:
                advisories.append(report)

    # ----- 4. Scanner returns zero candidates loop (silent dead-cycle)
    # Symptom: the Scanner has run N cycles in a row with passed_filters=0,
    # AND there's a non-empty watchlist. Almost always volume_threshold or
    # scanner_min_pct_change is too aggressive for the account's actual
    # watchlist (e.g. 3M shares/day on a small-cap watchlist). Auto-relax
    # the most likely culprit per cycle so trading resumes within ~5 min
    # without operator intervention.
    if mode in ("wheel", "wheel_plus_dca"):
        scanner_repair = _check_scanner_zero_candidates(project_id)
        if scanner_repair is not None:
            applied.append(scanner_repair)

    # ----- 5. Watchlist doesn't fit BP (small-account revenue trap) -----
    # Symptom: project's watchlist has tickers, but every ticker's
    # typical strike × 100 exceeds the account's options_buying_power
    # so the Strategist can't propose anything. Detector hits the
    # broker once to snapshot prices; auto-applies the tier-correct
    # CHEAP watchlist when the current one is unusable.
    if mode in ("wheel", "wheel_plus_dca", "intraday_momentum"):
        bp_repair = _check_watchlist_fits_bp(project_id, project)
        if bp_repair is not None:
            if bp_repair.get("auto_applied"):
                applied.append(bp_repair["auto_applied"])
            else:
                advisories.append(bp_repair["advisory"])

    # ----- 5. ETrade tokens dead -----------------------------------------
    if mode != "paused" and getattr(project, "broker_type", "") == "etrade":
        if not getattr(project, "etrade_access_token", ""):
            advisories.append({
                "issue": "etrade_oauth_not_completed",
                "narrative": (
                    "ETrade project has no access token. Visit "
                    "/etrade/connect to complete the OAuth handshake "
                    "with a sandbox test customer (for sandbox env) "
                    "or your live brokerage login (for production)."
                ),
            })
        else:
            try:
                from execution import BrokerReauthRequired, get_broker
                get_broker(project).get_account()
            except BrokerReauthRequired:
                advisories.append({
                    "issue": "etrade_tokens_expired",
                    "narrative": (
                        "ETrade access tokens are past the daily "
                        "midnight-ET expiry and renewal failed. The "
                        "worker cycles burn no-op events until the user "
                        "re-authorizes. Visit /etrade/connect to "
                        "reconnect."
                    ),
                })
            except Exception:
                pass

    # Emit a single audit event summarizing what the self-healer did.
    if applied or advisories:
        EventsRepo.log(project_id, "Optimizer", "SELF_HEAL", {
            "mode": mode,
            "applied": applied,
            "advisories": advisories,
            "narrative": _narrate(mode, applied, advisories),
        })

    return {"applied": applied, "advisories": advisories}


def _check_scanner_zero_candidates(
    project_id: str,
) -> dict[str, Any] | None:
    """Detect 'Scanner returned 0 candidates for the last 5+ cycles
    despite a non-empty watchlist' and auto-relax the most-restrictive
    filter so trading resumes.

    Why: on 2026-06-08 Yatin-Minimum sat idle for hours because
    volume_threshold was 3M (mega-cap default) but the watchlist was
    all small-caps trading 300-900k shares mid-day. The user noticed
    the lost trading window and rightly objected that this should
    work autonomously regardless of account size. This is that fix.

    Auto-fix order (least disruptive first):
      1. If volume_threshold > 500k and the rejections are dominated by
         "volume X below threshold Y" → halve volume_threshold (capped
         at 100k floor).
      2. If scanner_min_pct_change > 0.5 and rejections include
         "%-change below floor" → halve it (capped at 0.25 floor).
      3. Otherwise advisory only.
    """
    try:
        events = EventsRepo.recent(project_id, limit=80)
    except Exception:
        return None
    # Look at last 5 SCAN events. Require all 5 to have passed_filters=0
    # AND universe_size>0 (i.e. it scanned something, just rejected all).
    scans = [e for e in events
             if e.get("node_name") == "Scanner"
             and e.get("event_type") == "SCAN"]
    if len(scans) < 5:
        return None
    last_5 = scans[:5]
    for s in last_5:
        pl = s.get("payload") or {}
        if pl.get("passed_filters") != 0:
            return None
        if (pl.get("universe_size") or 0) <= 0:
            return None
    # 5 consecutive dead scans. Diagnose the dominant rejection reason.
    # Walk rejected_sample across the 5 scans and bucket.
    vol_rej = 0
    pct_rej = 0
    other_rej = 0
    for s in last_5:
        pl = s.get("payload") or {}
        for r in (pl.get("rejected_sample") or []):
            reason = str(r.get("reason") or "").lower()
            if "volume" in reason and "below threshold" in reason:
                vol_rej += 1
            elif "%-change" in reason or "pct" in reason:
                pct_rej += 1
            else:
                other_rej += 1
    # Volume threshold dominates → halve it.
    if vol_rej >= max(pct_rej, other_rej):
        try:
            cur = int(ProjectSettings.get(
                project_id, "volume_threshold", default=500_000)
                or 500_000)
        except Exception:
            cur = 500_000
        if cur > 100_000 and not _user_touched(
                project_id, "volume_threshold", hours=24):
            new = max(100_000, cur // 2)
            if new < cur:
                _apply(project_id, "volume_threshold", new)
                return {
                    "key":    "volume_threshold",
                    "old":    cur,
                    "new":    new,
                    "reason": (
                        f"Scanner returned 0 candidates for 5 cycles "
                        f"despite a non-empty watchlist. Volume "
                        f"rejections dominated ({vol_rej} of "
                        f"{vol_rej+pct_rej+other_rej}). Halved "
                        f"volume_threshold from {cur} to {new} so "
                        f"the small-cap watchlist can actually pass."
                    ),
                }
    # Pct-change dominates → halve it.
    if pct_rej >= max(vol_rej, other_rej):
        try:
            cur_p = float(ProjectSettings.get(
                project_id, "scanner_min_pct_change", default=1.5)
                or 1.5)
        except Exception:
            cur_p = 1.5
        if cur_p > 0.25 and not _user_touched(
                project_id, "scanner_min_pct_change", hours=24):
            new_p = max(0.25, cur_p / 2)
            if new_p < cur_p:
                _apply(project_id, "scanner_min_pct_change", new_p)
                return {
                    "key":    "scanner_min_pct_change",
                    "old":    cur_p,
                    "new":    new_p,
                    "reason": (
                        f"5 dead scans, %-change rejections dominated. "
                        f"Halved scanner_min_pct_change from {cur_p} "
                        f"to {new_p}."
                    ),
                }
    return None


def _check_watchlist_fits_bp(
    project_id: str,
    project: Any,
) -> dict[str, Any] | None:
    """Detect the "watchlist exists but nothing fits BP" trap.

    A small account ($5k options_bp) with a watchlist of mega-caps
    (NVDA $200, MSFT $416) can't open a single CSP — strike × 100 is
    several × the available BP. The Scanner finds 0 candidates and
    the project sits idle forever despite looking healthy on paper.

    On detection:
      * If options_bp is truly tiny (<$500) we surface an advisory —
        the account literally can't trade.
      * If at least one watchlist ticker would fit, we filter the
        watchlist down to those tickers AND lower scanner_max_price
        so the Scanner stops cutting them with a stale price cap.
      * If NOTHING fits and we have a tier-appropriate cheap watchlist
        available, we replace the watchlist with that.

    Returns:
        {auto_applied: {...}}  — when we made a repair
        {advisory: {...}}      — when we surface it for human action
        None                   — when watchlist is fine
    """
    if _user_touched(project_id, "watchlist", hours=24):
        # User explicitly chose the current watchlist in the last day —
        # respect their intent. Self-healer surfaces advisory only.
        pass

    watchlist = (ProjectSettings.get(
        project_id, "watchlist", default="") or "").strip()
    if not watchlist:
        return None

    try:
        from execution import BrokerReauthRequired, get_broker
        client = get_broker(project)
        account = client.get_account()
    except BrokerReauthRequired:
        # Already handled by the etrade-tokens check; don't double-log.
        return None
    except Exception:
        return None

    options_bp = float(account.get("options_buying_power") or 0)
    if options_bp <= 0:
        # Fall back to cash buying power — some brokers don't split it
        # out (etrade). Without ANY BP signal we can't filter.
        options_bp = float(account.get("buying_power") or 0)
    if options_bp <= 0:
        return None

    syms = [s.strip().upper() for s in watchlist.split(",") if s.strip()]
    if not syms:
        return None

    # Single broker call to snapshot every watchlist ticker. ~200ms.
    try:
        snaps = client.snapshots(syms)
    except Exception:
        return None

    max_strike = options_bp / 100.0
    kept: list[str] = []
    dropped: list[str] = []
    for sym in syms:
        snap = snaps.get(sym) if snaps else None
        price = getattr(snap, "last_price", None) if snap else None
        if price is None or price <= 0:
            kept.append(sym)  # be conservative — keep unknowns
            continue
        # Strategist picks ~5% OTM strikes for CSPs. If price*1.05
        # already busts BP, this ticker can't be traded.
        if price * 1.05 <= max_strike:
            kept.append(sym)
        else:
            dropped.append(sym)

    if not dropped:
        return None  # everything fits — nothing to repair

    if _user_touched(project_id, "watchlist", hours=24):
        # User just set this — surface but don't auto-fix.
        return {
            "advisory": {
                "issue": "watchlist_doesnt_fit_bp",
                "narrative": (
                    f"Watchlist has {len(syms)} tickers but "
                    f"{len(dropped)} of them require more collateral "
                    f"than current options BP (${options_bp:,.0f}). "
                    f"Names that don't fit: "
                    f"{', '.join(dropped[:6])}"
                    + (f" (+{len(dropped)-6} more)"
                       if len(dropped) > 6 else "")
                    + f". {len(kept)} tickers still tradeable."
                ),
                "suggested_fix": (
                    f"Set scanner_max_price={max_strike:.0f} OR drop "
                    f"the expensive tickers from the watchlist OR "
                    f"click Optimize Now (now BP-aware) to right-size "
                    f"automatically."
                ),
            },
        }

    # User hasn't touched watchlist recently → auto-shrink it to the
    # tickers that actually fit, and align scanner_max_price.
    new_watchlist = ",".join(kept) if kept else ""
    if not new_watchlist:
        # Nothing in the watchlist fits — surface advisory; we don't
        # want to silently swap in a random cheap watchlist.
        return {
            "advisory": {
                "issue": "watchlist_entirely_too_expensive",
                "narrative": (
                    f"None of the {len(syms)} watchlist tickers fits "
                    f"in current options BP (${options_bp:,.0f}). "
                    f"Project can't open any CSPs as configured."
                ),
                "suggested_fix": (
                    "Click Optimize Now — it'll pick a tier-correct "
                    "watchlist of names that fit the account size."
                ),
            },
        }

    _apply(project_id, "watchlist", new_watchlist)
    _apply(project_id, "scanner_max_price", round(max_strike, 2))
    return {
        "auto_applied": {
            "key":    "watchlist",
            "old":    watchlist,
            "new":    new_watchlist,
            "reason": (
                f"Dropped {len(dropped)} ticker(s) too expensive for "
                f"current options BP ${options_bp:,.0f}: "
                f"{', '.join(dropped[:6])}"
                + (f" (+{len(dropped)-6} more)"
                   if len(dropped) > 6 else "")
                + f". Also lowered scanner_max_price to "
                f"${max_strike:.0f}. Add tickers back when the "
                f"account grows."
            ),
        },
    }


def _try_repair_wheel_block(
    project_id: str,
    report: dict[str, Any],
) -> dict[str, Any] | None:
    """If the wheel is in a Guardrail-block loop AND the proximate cause
    is the concentration cap (the most common Strategist→Guardrail
    misconfig), drop contracts_per_csp by 1 to give the trade room to
    fit under the cap. Returns the applied-repair dict on success, None
    when we couldn't auto-fix (user touched the setting, already at 1,
    or the cause isn't concentration). The user keeps full control —
    a single manual change pauses this auto-fix for 7 days."""
    cause = (report.get("narrative") or "").lower()
    if "concentration" not in cause:
        # Collateral / BP issues need a different fix the user must opt
        # into (raising max_collateral_pct relaxes a risk cap), so we
        # leave those as advisories.
        return None
    if _user_touched(project_id, "contracts_per_csp"):
        return None
    try:
        cur = int(ProjectSettings.get(
            project_id, "contracts_per_csp", default=1) or 1)
    except Exception:
        return None
    if cur <= 1:
        return None
    new = cur - 1
    _apply(project_id, "contracts_per_csp", new)
    return {
        "key":    "contracts_per_csp",
        "old":    cur,
        "new":    new,
        "reason": (
            f"Strategist→Guardrail no-fill loop detected: "
            f"{report.get('narrative')}. "
            f"Lowering contracts_per_csp from {cur} to {new} so the "
            f"proposed trade's collateral fits under the per-ticker "
            f"concentration cap. Continuous optimizer + manual changes "
            f"keep precedence — this fires only when the user hasn't "
            f"touched the key in the last 7 days."
        ),
    }


def _diagnose_wheel_block(project_id: str) -> dict[str, Any] | None:
    """Look at the last hour of events. If we see the
    Strategist-picks → Guardrail-blocks loop firing for the SAME
    reason every cycle with no SUBMIT events, surface an advisory."""
    cutoff = datetime.now(tz=timezone.utc) - timedelta(hours=1)
    try:
        events = EventsRepo.recent(project_id, limit=200)
    except Exception:
        return None

    guard_block_reasons: dict[str, int] = {}
    executor_submits = 0
    for e in events:
        ts = e.get("created_at")
        if ts is None:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if ts < cutoff:
            continue
        if e.get("node_name") == "Guardrail" and e.get("event_type") == "RISK":
            pl = e.get("payload") or {}
            # Guardrail emits TWO RISK rows per cycle: a per-trade
            # rejection (payload has ``rejected`` + ``reason``) and a
            # cycle summary (payload has ``approved_trades`` /
            # ``actions`` lists). We count only the per-trade rejections.
            if pl.get("rejected") and pl.get("reason"):
                reason = str(pl.get("reason"))
                # Bucket by first ~80 chars so per-ticker variants merge.
                key = reason[:80]
                guard_block_reasons[key] = (
                    guard_block_reasons.get(key, 0) + 1)
        if (e.get("node_name") == "Executor"
                and e.get("event_type") == "EXECUTE"):
            # The cutoff check above already ensures `ts >= cutoff`, so
            # we count submits inside the same window — old SUBMITTED
            # rows from yesterday must NOT mask a current-hour loop.
            pl = e.get("payload") or {}
            for r in (pl.get("results") or []):
                if str(r.get("status") or "").upper() == "SUBMITTED":
                    executor_submits += 1

    if executor_submits > 0:
        return None  # things are flowing — false positive
    if not guard_block_reasons:
        return None

    # The most-frequent block reason in the last hour.
    top_reason, top_count = max(
        guard_block_reasons.items(), key=lambda kv: kv[1])
    if top_count < 3:
        return None  # noise threshold — need at least 3 blocks/hr

    suggested = []
    text = top_reason.lower()
    if "concentration" in text:
        try:
            cur = int(ProjectSettings.get(
                project_id, "contracts_per_csp", default=1) or 1)
        except Exception:
            cur = 1
        if cur > 1:
            suggested.append(
                f"contracts_per_csp is {cur}. The proposed trade's "
                f"collateral ({cur} × strike × 100) exceeds the "
                f"per-ticker concentration cap. Lower contracts_per_csp "
                f"to {cur - 1} OR raise max_concentration_per_ticker "
                f"(currently a fraction of max_equity_allocation)."
            )
        else:
            suggested.append(
                "max_concentration_per_ticker is too tight — even a "
                "1-contract CSP busts the cap. Raise it (e.g., 0.30 → "
                "0.40) or pick smaller-strike underlyings."
            )
    if "collateral" in text and "buying power" in text:
        suggested.append(
            "options_buying_power is exhausted by existing positions. "
            "Wait for some to close, or raise max_collateral_pct."
        )
    if not suggested:
        suggested.append(
            "Inspect the Guardrail reason and either widen the "
            "relevant cap or pick smaller trades.")

    return {
        "issue": "wheel_blocked_by_guardrail_loop",
        "narrative": (
            f"Strategist keeps picking trades that the Guardrail blocks: "
            f"\"{top_reason}\" ({top_count}× in the last hour) with 0 "
            f"Executor submissions. The wheel is in a no-fill loop."
        ),
        "suggested_fix": "; ".join(suggested),
    }


def _narrate(mode: str, applied: list[dict[str, Any]],
             advisories: list[dict[str, Any]]) -> list[str]:
    out: list[str] = []
    if applied:
        out.append(
            f"Mode-coherence self-healer applied "
            f"{len(applied)} setting fix(es) so the '{mode}' strategy can "
            f"actually trade: "
            + ", ".join(f"{a['key']}={a['new']!r}" for a in applied)
        )
        for a in applied:
            out.append(f"  - {a['key']}: {a['reason']}")
    if advisories:
        out.append(
            f"{len(advisories)} advisory item(s) need user action:")
        for ad in advisories:
            out.append(f"  - {ad.get('issue')}: "
                       f"{ad.get('narrative')}")
            if ad.get("suggested_fix"):
                out.append(f"    fix: {ad['suggested_fix']}")
    return out
