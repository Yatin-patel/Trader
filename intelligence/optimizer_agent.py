"""Continuous Optimizer Agent.

Runs every ``optimizer_interval_minutes`` (AppSettings, default 30)
for each active project. Pipeline per project:

  1. Build a recommendation via the existing
     ``intelligence.recommendations.build_recommendations`` (which
     feeds 30 d P&L attribution + current settings + per-bucket
     win-rate to the configured LLM and asks for ONE or TWO specific
     parameter changes). That's already audited as an
     ``ai_recommendations`` row.
  2. If the project has ``optimizer_auto_apply=True`` AND the LLM
     returned changes that pass the safety rails (see below), apply
     the recommendation automatically and log
     ``Optimizer.AUTO_APPLY``. Otherwise just leave the
     recommendation pending for the user to apply via the UI.

Safety rails (auto-apply only)
------------------------------
* Only keys in ``_AUTO_APPLY_BOUNDS`` may be auto-applied. Strategy
  mode, dry_run, and other "kill switch" knobs are explicitly NOT in
  this list — those require human judgment.
* Every numeric change is clamped to a sane absolute range.
* No single change may move a value by more than 50% per cycle (so a
  bad recommendation can move slowly toward a bad outcome, not jump
  there in one tick).
* At most ``MAX_AUTO_APPLIES_PER_CYCLE`` changes per project per
  cycle.
"""
from __future__ import annotations

import logging
from typing import Any

from db.repositories import EventsRepo
from db.settings_store import ProjectSettings

logger = logging.getLogger(__name__)


MAX_AUTO_APPLIES_PER_CYCLE = 2

# After a Manual.SETTING_CHANGE on a given key, the Optimizer must leave
# that key alone for this many hours. Stops the "I tuned X 5 minutes ago
# and now it's back" problem we saw when the LLM kept proposing
# min_iv_rank changes that undid the user's manual setting.
MANUAL_CHANGE_COOLDOWN_HOURS = 24


def _recent_manual_change(project_id: str, key: str,
                          hours: float) -> tuple[bool, str | None]:
    """Did the user manually change ``key`` within the last ``hours``?
    Returns (was_recent, when_iso_or_none).

    NB: uses a direct SQL query rather than ``EventsRepo.recent``.
    Active projects can log 500+ events/hour (Scanner SCAN, Strategist
    SELECTION ×N, Guardrail RISK ×N, Worker LOOP every 2 min, etc.), so
    a count-bounded fetch only reaches back ~20 minutes and misses the
    Manual.SETTING_CHANGE row we're looking for. The (project_id,
    created_at DESC) index on agent_events makes the time-filtered
    query fast.
    """
    from datetime import datetime, timedelta, timezone
    import json as _json
    from sqlalchemy import text as _text
    from db.connection import session_scope
    cutoff = datetime.now(tz=timezone.utc) - timedelta(hours=hours)
    try:
        with session_scope() as s:
            rows = s.execute(_text("""
                SELECT created_at, payload FROM agent_events
                WHERE project_id = :p
                  AND node_name = 'Manual'
                  AND event_type = 'SETTING_CHANGE'
                  AND created_at > :c
                ORDER BY created_at DESC
                LIMIT 50
            """), {"p": project_id, "c": cutoff}).fetchall()
    except Exception:
        return (False, None)
    for r in rows:
        try:
            pl = _json.loads(r[1]) if r[1] else {}
        except Exception:
            continue
        if pl.get("key") == key:
            ts = r[0]
            return (True, ts.isoformat() if hasattr(ts, "isoformat")
                    else str(ts))
    return (False, None)


# Settings the Optimizer NEVER auto-applies — these encode the user's
# stated intent for the project and changing them silently would be a
# trust violation. trading_plan in particular is the project's risk
# identity (conservative/balanced/aggressive); the agent reads it but
# doesn't get to vote on it. strategy_mode is the same idea ("what
# this project trades"). Everything in this set is excluded BEFORE
# the value-clamp / step-size rules are even consulted.
_PROTECTED_KEYS: frozenset[str] = frozenset({
    "trading_plan",
    "strategy_mode",
    "dry_run",
    "auto_roll_enabled",
    "use_extended_hours",
})

# Per-plan step-size cap. Conservative plans want the Optimizer to nudge
# settings; Aggressive plans want it to react faster. None of these
# allow risk caps to relax beyond the absolute clamp range below.
_PLAN_MAX_REL_STEP: dict[str, float] = {
    "conservative": 0.15,
    "balanced":     0.50,
    "aggressive":   0.75,
}

# Keys auto-apply will touch, plus their absolute clamp range. Anything
# not in this list is left for human review even if the LLM suggests
# changing it.
_AUTO_APPLY_BOUNDS: dict[str, tuple[float, float]] = {
    # --- Wheel (CSP + CC) -----------------------------------------------
    "csp_delta_min":           (0.05, 0.45),
    "csp_delta_max":           (0.15, 0.60),
    "csp_min_dte":             (0,    21),
    "csp_max_dte":             (5,    60),
    "cc_delta_min":            (0.05, 0.45),
    "cc_delta_max":            (0.15, 0.60),
    "scanner_min_pct_change":  (0.0,  5.0),
    "stop_loss_dollars":       (0.5,  25.0),
    "max_collateral_pct":      (0.10, 0.95),
    "min_iv_rank":             (0.0,  0.70),
    "close_at_profit_pct":     (0.20, 0.95),
    # contracts_per_csp is a structural change — small accounts get
    # hurt if it goes from 1 → 3 without operator review. Excluded.
    # max_concentration_per_ticker / per_sector are risk caps. Excluded.
    # take_profit_enabled is a bool: leave to humans.
    # --- Multi-leg spreads ---------------------------------------------
    # delta on the SHORT leg of the spread / iron condor.
    "spread_target_delta":     (0.10, 0.50),
    # USD dollar width between legs (or wing width for an iron condor).
    "spread_width":            (1.0,  20.0),
    "spread_min_dte":          (0,    30),
    "spread_max_dte":          (5,    90),
    # --- Calendar spread ------------------------------------------------
    "calendar_short_dte":      (1,    30),
    "calendar_long_dte":       (30,   120),
    # --- Day-trading (intraday momentum) -------------------------------
    # Hard cap on per-cycle 0DTE/1DTE opens. PDT 5-day window cap is
    # enforced independently in agents/executor.py — this just keeps
    # the open-count sane within a single cycle.
    "intraday_max_trades_per_cycle": (1, 10),
}

def _safe_to_apply(key: str, old: Any, new: Any,
                   max_rel_step: float,
                   project_id: str | None = None) -> tuple[bool, str]:
    """Return (ok, reason_if_blocked). Applies clamp + step-size + type
    rules + 24-hour manual-change cooldown to one proposed change.
    ``max_rel_step`` is derived from the project's trading_plan
    (conservative/balanced/aggressive)."""
    if key in _PROTECTED_KEYS:
        return (False, f"{key} is project intent — never auto-changed")
    if key not in _AUTO_APPLY_BOUNDS:
        return (False, f"{key} is not in the auto-apply whitelist")
    # Manual-change cooldown: if the user touched this key recently,
    # leave it alone so the LLM can't quietly roll back their decision.
    if project_id is not None:
        recent, ts = _recent_manual_change(
            project_id, key, MANUAL_CHANGE_COOLDOWN_HOURS)
        if recent:
            return (False, (
                f"user manually changed {key} at {ts}; respecting "
                f"{MANUAL_CHANGE_COOLDOWN_HOURS}h cooldown"
            ))
    lo, hi = _AUTO_APPLY_BOUNDS[key]
    try:
        n = float(new)
    except (TypeError, ValueError):
        return (False, f"value {new!r} is not numeric")
    if not (lo <= n <= hi):
        return (False, f"value {n} outside safe range [{lo}, {hi}]")
    if old is not None:
        try:
            o = float(old)
            if o > 0:
                rel = abs(n - o) / o
                if rel > max_rel_step:
                    return (False, (
                        f"step too large ({rel*100:.0f}% > "
                        f"{max_rel_step*100:.0f}% allowed under "
                        f"current trading_plan)"
                    ))
        except (TypeError, ValueError):
            pass
    return (True, "")


def run_for_project(project_id: str) -> dict[str, Any]:
    """Build a recommendation for ``project_id`` and (if opted in) auto-
    apply changes that pass the safety rails. Returns a structured
    report so the scheduler can log it."""
    from intelligence.recommendations import (
        build_recommendations, apply_recommendation,
    )
    # Pre-LLM step: deterministic mode-coherence self-healer. Catches
    # the class of bug where the user picked a strategy_mode but the
    # supporting toggles weren't flipped, OR where the wheel pipeline
    # is in a Strategist→Guardrail no-fill loop. Runs every tick (cheap)
    # so misconfigs get noticed within the optimizer cadence rather
    # than waiting on the LLM to randomly suggest a fix.
    try:
        from intelligence.mode_coherence import check_and_repair
        heal = check_and_repair(project_id)
    except Exception:
        logger.exception("mode_coherence check failed for %s", project_id)
        heal = {"applied": [], "advisories": []}

    rec = build_recommendations(project_id)
    if "error" in rec:
        return {"project_id": project_id, "status": "no_recommendation",
                "error": rec["error"],
                "self_heal": heal}

    rec_id = rec.get("rec_id")
    changes = rec.get("changes") or {}

    auto_apply = bool(ProjectSettings.get(
        project_id, "optimizer_auto_apply", default=False))
    if not auto_apply:
        return {"project_id": project_id, "status": "rec_built",
                "rec_id": rec_id, "title": rec.get("title"),
                "changes": changes, "auto_apply": False,
                "self_heal": heal}

    # Step-size cap is plan-aware: conservative plans only allow tiny
    # nudges, aggressive plans allow larger reactions. The plan itself
    # is in _PROTECTED_KEYS so the agent can never overwrite it.
    plan = str(ProjectSettings.get(
        project_id, "trading_plan", default="balanced") or "balanced"
    ).lower()
    max_rel_step = _PLAN_MAX_REL_STEP.get(plan, _PLAN_MAX_REL_STEP["balanced"])

    # Auto-apply pass — vet each proposed change against safety rails.
    safe_changes: dict[str, Any] = {}
    rejections: list[dict[str, Any]] = []
    for k, v in list(changes.items())[:MAX_AUTO_APPLIES_PER_CYCLE]:
        current = ProjectSettings.get(project_id, k, default=None)
        ok, reason = _safe_to_apply(k, current, v, max_rel_step,
                                     project_id=project_id)
        if not ok:
            rejections.append({"key": k, "value": v,
                               "current": current, "reason": reason})
            continue
        safe_changes[k] = v

    if not safe_changes:
        EventsRepo.log(project_id, "Optimizer", "REVIEW_REQUIRED", {
            "rec_id": rec_id, "title": rec.get("title"),
            "changes": changes, "rejections": rejections,
            "narrative": [
                "Auto-apply considered the LLM recommendation but every "
                "proposed change failed a safety rail. Recommendation "
                "stays pending for human review.",
            ],
        })
        return {"project_id": project_id, "status": "blocked_by_rails",
                "rec_id": rec_id, "rejections": rejections,
                "self_heal": heal}

    # Persist via the recommendations module's apply_recommendation,
    # which handles the actual ProjectSettings.set + status update.
    # We narrowed the rec's changes to safe_changes above; rewrite the
    # stored row so apply_recommendation only writes those keys.
    from db.connection import session_scope
    from sqlalchemy import text
    import json as _json
    with session_scope() as s:
        s.execute(text(
            "UPDATE ai_recommendations "
            "SET suggested_changes = :c WHERE rec_id = :r"
        ), {"c": _json.dumps(safe_changes), "r": int(rec_id)})
        s.commit()
    applied = apply_recommendation(project_id, int(rec_id))

    EventsRepo.log(project_id, "Optimizer", "AUTO_APPLY", {
        "rec_id": rec_id, "title": rec.get("title"),
        "applied": applied.get("applied") or {},
        "rejections": rejections,
        "narrative": [
            f"Optimizer auto-applied {len(applied.get('applied') or {})} "
            f"setting change(s) from the LLM recommendation: "
            f"{applied.get('applied')}. "
            f"{len(rejections)} change(s) were rejected by safety rails.",
        ],
    })
    return {"project_id": project_id, "status": "auto_applied",
            "rec_id": rec_id, "applied": applied.get("applied") or {},
            "rejections": rejections}


def run_all_active() -> list[dict[str, Any]]:
    """Iterate every active project. Used by the scheduler tick."""
    from db.repositories import ProjectsRepo
    results: list[dict[str, Any]] = []
    for proj in ProjectsRepo.list_active():
        try:
            results.append(run_for_project(proj.project_id))
        except Exception as e:
            logger.exception("optimizer failed for %s: %s",
                             proj.project_id, e)
            results.append({"project_id": proj.project_id,
                            "status": "error", "error": str(e)})
    return results
