"""LLM-suggested parameter adjustments (Cat 10.1).

Once a week (scheduled by runner), feed a summary of the project's recent
performance and current settings to the configured LLM and ask for
specific, actionable parameter tweaks. Persist the suggestion and surface
it in the UI for user approval.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy import text

from analytics.attribution import attribution_by_dimension
from analytics.pnl_calculator import metrics_summary
from db.connection import insert_returning_id, session_scope
from db.repositories import ProjectsRepo
from db.settings_store import ProjectSettings

logger = logging.getLogger(__name__)


# Always-tunable knobs (wheel + cross-strategy risk controls). The
# Optimizer Agent can always touch these regardless of strategy_mode.
_TUNABLE_WHEEL = [
    "csp_delta_min", "csp_delta_max", "csp_min_dte", "csp_max_dte",
    "cc_delta_min", "cc_delta_max", "scanner_min_pct_change",
    "stop_loss_dollars", "max_collateral_pct", "contracts_per_csp",
    "take_profit_enabled", "close_at_profit_pct", "min_iv_rank",
]

# Additional knobs unlocked when strategy_mode is one of the
# multi-leg spread modes. The wheel knobs above STAY in the candidate
# list because most risk caps + scanner gates apply to every mode.
_TUNABLE_SPREAD = [
    "spread_target_delta", "spread_width",
    "spread_min_dte", "spread_max_dte",
]
_TUNABLE_CALENDAR = [
    "calendar_short_dte", "calendar_long_dte",
]

# Day-trading knobs.
_TUNABLE_INTRADAY = [
    "intraday_max_trades_per_cycle",
]


_SPREAD_MODES = {
    "bull_put_spread", "bear_call_spread",
    "bull_call_spread", "bear_put_spread",
    "iron_condor",
}


def _tunable_keys(strategy_mode: str) -> list[str]:
    """Return the set of setting keys the Optimizer Agent is allowed to
    suggest changes for, scoped to the project's current strategy_mode.

    Always includes the wheel + cross-strategy knobs (risk caps apply
    everywhere). Adds spread / calendar / intraday knobs only when
    the project is actually running that mode — otherwise the LLM
    would propose changes to settings the strategy doesn't read."""
    keys = list(_TUNABLE_WHEEL)
    mode = (strategy_mode or "wheel").lower()
    if mode in _SPREAD_MODES:
        keys += _TUNABLE_SPREAD
    if mode == "calendar_spread":
        # Calendar uses BOTH the spread sizing knobs (target_delta /
        # width via the spread_* settings on the back-month leg) AND
        # the specific calendar DTE knobs.
        keys += _TUNABLE_SPREAD + _TUNABLE_CALENDAR
    if mode == "intraday_momentum":
        keys += _TUNABLE_INTRADAY
    # De-dup while preserving order.
    seen: set[str] = set()
    out: list[str] = []
    for k in keys:
        if k not in seen:
            seen.add(k)
            out.append(k)
    return out


# Back-compat: a few call sites import _TUNABLE directly. Keep it
# pointing at the wheel-mode default so they don't crash; they should
# migrate to _tunable_keys(strategy_mode).
_TUNABLE = _TUNABLE_WHEEL


def _current_settings(project_id: str,
                      tunable_keys: list[str]) -> dict[str, Any]:
    return {k: ProjectSettings.get(project_id, k) for k in tunable_keys}


def build_recommendations(project_id: str) -> dict[str, Any]:
    project = ProjectsRepo.get(project_id)
    if project is None:
        return {"error": "project not found"}

    # strategy_mode scopes which keys the Optimizer is allowed to
    # touch. A user running iron_condor should never see the LLM
    # propose csp_delta_min changes that won't affect anything — and
    # vice versa, the wheel-mode user shouldn't see spread_width.
    strategy_mode = str(ProjectSettings.get(
        project_id, "strategy_mode", default="wheel") or "wheel").lower()
    tunable_keys = _tunable_keys(strategy_mode)

    metrics = metrics_summary(project_id, period="month")
    by_delta = attribution_by_dimension(project_id, dimension="delta")
    by_dte = attribution_by_dimension(project_id, dimension="dte")
    settings = _current_settings(project_id, tunable_keys)

    from agents.llm_factory import build_llm
    from langchain_core.messages import HumanMessage, SystemMessage
    # 3500 tokens was too tight — Gemini's verbose rationales were being
    # truncated mid-string and the parser saw no closing `}`, surfacing
    # as "Get AI suggestions Failed: LLM returned no JSON" in the UI.
    llm = build_llm(purpose="chat", max_tokens=6000)
    if llm is None:
        return {"error": "no LLM configured"}

    system = SystemMessage(content=(
        "You are an options-strategy parameter tuner. The project's "
        "strategy_mode tells you which strategy is running — wheel (CSPs + "
        "CCs), one of six multi-leg spreads (bull_put_spread, "
        "bear_call_spread, bull_call_spread, bear_put_spread, iron_condor, "
        "calendar_spread), or intraday_momentum (0DTE/1DTE long calls or "
        "puts driven by RSI/MACD/VWAP signals). Look at the last 30 days of "
        "P&L attribution + current settings, then suggest ONE or TWO "
        "specific parameter changes that should improve risk-adjusted "
        "return for that strategy.\n"
        "\nIMPORTANT: respect the user's stated trading_plan in the "
        "payload. If trading_plan='conservative', do NOT suggest higher "
        "deltas, shorter DTE, narrower spread widths, lower IV-rank "
        "floors, or loosened risk caps — pick changes that reduce "
        "variance even at the cost of premium. If trading_plan="
        "'aggressive', it's OK to push the other direction. If "
        "trading_plan='balanced', pick the change with the best "
        "Sharpe/Sortino tradeoff. Never propose changing trading_plan, "
        "strategy_mode, or dry_run — those encode user intent.\n"
        "\nStrategy-specific guidance:\n"
        "- WHEEL: tune csp_delta_*, csp_min/max_dte, cc_delta_*, "
        "max_collateral_pct, scanner_min_pct_change, close_at_profit_pct, "
        "min_iv_rank.\n"
        "- VERTICAL SPREADS (bull_put/bear_call/bull_call/bear_put): "
        "spread_target_delta sets the SHORT-leg delta; spread_width is "
        "the dollar distance between legs (wider = more credit but "
        "higher max-loss); spread_min_dte/spread_max_dte set the DTE "
        "window.\n"
        "- IRON CONDOR: same spread_* knobs apply (spread_width becomes "
        "the wing width on both put + call sides). Lower "
        "spread_target_delta widens the profit zone but reduces credit.\n"
        "- CALENDAR SPREAD: tune calendar_short_dte (near-month leg) "
        "and calendar_long_dte (back-month leg). Wider gap = more "
        "theta differential but more vega exposure.\n"
        "- INTRADAY MOMENTUM: the single tunable is "
        "intraday_max_trades_per_cycle. Raise it cautiously — each new "
        "trade burns a PDT slot for sub-$25k accounts.\n"
        "\nRules:\n"
        "- Return ONLY raw JSON. No markdown fences, no commentary before "
        "or after.\n"
        "- Schema: {\"title\": str, \"rationale\": str, "
        "\"changes\": {setting_key: new_value}}.\n"
        "- KEEP RATIONALE UNDER 500 CHARACTERS. Brevity matters — a long "
        "rationale will be truncated and the suggestion will fail to "
        "deliver. State the cause and the expected effect in 2-3 sentences.\n"
        "- ONLY propose changes to keys listed in tunable_keys for this "
        "project. Suggesting a key not in the list will be rejected.\n"
        "- Respect parameter scales (see param_scales in payload). "
        "min_iv_rank is a FRACTION 0..1 (e.g. 0.30 not 30). Delta bounds "
        "are 0..1. Percentages like max_collateral_pct are 0..1. "
        "spread_width is in DOLLARS (e.g. 5.0 = $5 spread).\n"
        "- If the data is too sparse for a confident recommendation, return "
        "changes = {} but still return valid JSON with title + rationale."
    ))
    param_scales = {
        # Wheel
        "csp_delta_min": "0..1 (e.g. 0.15)",
        "csp_delta_max": "0..1 (e.g. 0.30)",
        "cc_delta_min": "0..1",
        "cc_delta_max": "0..1",
        "csp_min_dte": "int days, 1..14 typical",
        "csp_max_dte": "int days, 21..60 typical",
        "scanner_min_pct_change": "percent points (e.g. 1.5 means 1.5%)",
        "stop_loss_dollars": "USD per share, e.g. 2.0",
        "max_collateral_pct": "0..1 (fraction of buying power)",
        "contracts_per_csp": "int, 1..5 typical",
        "take_profit_enabled": "bool",
        "close_at_profit_pct": "0..1 (e.g. 0.50 = 50% of max profit)",
        "min_iv_rank": "0..1 (e.g. 0.30 means 30th percentile)",
        # Multi-leg spreads
        "spread_target_delta": "0..1 — delta of the SHORT leg "
                               "(0.20-0.35 typical for credit spreads, "
                               "0.40-0.55 for debit)",
        "spread_width":        "USD between legs / wing width "
                               "(1.0-20.0 typical)",
        "spread_min_dte":      "int days, 7..30 typical",
        "spread_max_dte":      "int days, 30..60 typical",
        # Calendar spread
        "calendar_short_dte":  "int days for near leg, 7..21 typical",
        "calendar_long_dte":   "int days for back leg, 30..90 typical",
        # Intraday
        "intraday_max_trades_per_cycle":
            "int, 1..10 — opens per cycle for 0DTE/1DTE longs",
    }
    # The user picks a trading_plan ('conservative' / 'balanced' /
    # 'aggressive') as their stated risk identity for this project.
    # Surface it to the LLM so suggested changes don't fight the
    # user's intent (e.g. don't recommend an aggressive delta on a
    # conservative project). The Optimizer's safety rails then
    # enforce step size based on the same plan.
    trading_plan = str(ProjectSettings.get(
        project_id, "trading_plan", default="balanced") or "balanced")
    payload = {
        "strategy_mode": strategy_mode,
        "metrics_30d": metrics,
        "attribution_by_delta": by_delta,
        "attribution_by_dte": by_dte,
        "current_settings": settings,
        "trading_plan": trading_plan,
        "tunable_keys": tunable_keys,
        "param_scales": param_scales,
    }
    user = HumanMessage(content=json.dumps(payload, default=str)[:8000])
    try:
        resp = llm.invoke([system, user])
        content = resp.content if isinstance(resp.content, str) else "".join(
            getattr(c, "text", "") for c in resp.content
        )
        # Defensive cleanup: Gemini sometimes ignores the "no markdown
        # fences" rule and wraps the JSON in ```json ... ``` anyway. Strip
        # any leading fence before parsing.
        stripped = content.strip()
        if stripped.startswith("```"):
            first_nl = stripped.find("\n")
            if first_nl != -1:
                stripped = stripped[first_nl + 1:]
            if stripped.endswith("```"):
                stripped = stripped[:-3]
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start == -1 or end == -1:
            logger.warning("recommendations: no JSON in LLM reply (len=%d): %s",
                           len(content), content[:400])
            tail = content[-200:] if len(content) > 200 else ""
            return {"error": "LLM returned no JSON "
                             "(reply likely truncated — try again)",
                    "raw": (content[:300] + "..." + tail)[:600]}
        try:
            parsed = json.loads(stripped[start: end + 1])
        except json.JSONDecodeError as je:
            logger.warning("recommendations: JSON parse failed: %s | raw=%s",
                           je, content[:400])
            return {"error": f"LLM returned malformed JSON: {je}",
                    "raw": content[:400]}
    except Exception as e:
        logger.exception("recommendations LLM call failed")
        return {"error": f"LLM error: {e}"}

    title = (parsed.get("title") or "Parameter tuning suggestion")[:256]
    rationale = parsed.get("rationale") or ""
    changes = parsed.get("changes") or {}
    # Filter to only tunable keys
    changes = {k: v for k, v in changes.items() if k in _TUNABLE}

    with session_scope() as s:
        rec_id = insert_returning_id(s, """
            INSERT INTO ai_recommendations
                (project_id, title, rationale, suggested_changes, status)
            VALUES (:p, :t, :r, :c, 'pending')
        """, {"p": project_id, "t": title,
              "r": str(rationale)[:4000],
              "c": json.dumps(changes)})
        s.commit()
        return {"rec_id": rec_id, "title": title,
                "rationale": rationale, "changes": changes}


def list_recommendations(project_id: str, *, limit: int = 20,
                         status: str | None = None) -> list[dict[str, Any]]:
    where = ["project_id = :p"]
    params: dict[str, Any] = {"p": project_id, "lim": int(limit)}
    if status:
        where.append("status = :st")
        params["st"] = status
    with session_scope() as s:
        rows = s.execute(text(
            f"SELECT rec_id, created_at, title, rationale, "
            f"suggested_changes, status, applied_at "
            f"FROM ai_recommendations "
            f"WHERE {' AND '.join(where)} "
            f"ORDER BY rec_id DESC LIMIT :lim"
        ), params).fetchall()
    out = []
    for r in rows:
        try:
            changes = json.loads(r[4]) if r[4] else {}
        except Exception:
            changes = {}
        out.append({
            "rec_id": int(r[0]),
            "created_at": r[1].isoformat() if r[1] else None,
            "title": r[2],
            "rationale": r[3],
            "changes": changes,
            "status": r[5],
            "applied_at": r[6].isoformat() if r[6] else None,
        })
    return out


def apply_recommendation(project_id: str, rec_id: int) -> dict[str, Any]:
    with session_scope() as s:
        row = s.execute(text("""
            SELECT suggested_changes, status FROM ai_recommendations
            WHERE rec_id = :rid AND project_id = :p
        """), {"rid": int(rec_id), "p": project_id}).fetchone()
    if not row:
        return {"error": "not found"}
    if row[1] == "applied":
        return {"error": "already applied"}
    try:
        changes = json.loads(row[0]) if row[0] else {}
    except Exception:
        changes = {}
    applied: dict[str, Any] = {}
    for k, v in changes.items():
        if k not in _TUNABLE:
            continue
        try:
            ProjectSettings.set(project_id, k, v)
            applied[k] = v
        except Exception as e:
            logger.exception("apply %s failed: %s", k, e)
    with session_scope() as s:
        s.execute(text("""
            UPDATE ai_recommendations
            SET status = 'applied', applied_at = UTC_TIMESTAMP()
            WHERE rec_id = :rid
        """), {"rid": int(rec_id)})
        s.commit()
    return {"applied": applied}
