"""LLM token + cost tracking (Cat 8.1).

Pricing table is a best-effort estimate. Update as Anthropic/Google change rates.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text

from db.connection import session_scope

# Rough per-1M-token USD prices (input/output). Free-tier models = 0.
_PRICES: dict[str, tuple[float, float]] = {
    "claude-opus-4-7":               (15.00, 75.00),
    "claude-sonnet-4-6":              (3.00, 15.00),
    "claude-haiku-4-5-20251001":      (0.80,  4.00),
    "gemini-2.5-pro":                 (1.25,  5.00),
    "gemini-2.5-flash":               (0.075, 0.30),
    "gemini-2.5-flash-lite":          (0.0,   0.0),
    "gemini-2.0-flash":               (0.10,  0.40),
}


def _estimate_cost(model: str, in_tok: int, out_tok: int) -> float:
    inp, outp = _PRICES.get(model, (0.0, 0.0))
    return (in_tok / 1_000_000) * inp + (out_tok / 1_000_000) * outp


def record_usage(*, project_id: str | None, purpose: str, provider: str,
                 model: str, prompt_tokens: int = 0,
                 completion_tokens: int = 0,
                 cache_hit: bool = False) -> int:
    total = int(prompt_tokens) + int(completion_tokens)
    cost = _estimate_cost(model, int(prompt_tokens), int(completion_tokens))
    with session_scope() as s:
        row = s.execute(text("""
            INSERT INTO dbo.llm_usage
                (project_id, purpose, provider, model, prompt_tokens,
                 completion_tokens, total_tokens, cost_usd, cache_hit)
            OUTPUT INSERTED.usage_id
            VALUES (:p, :pp, :pv, :m, :it, :ot, :tt, :c, :ch)
        """), {"p": project_id, "pp": purpose, "pv": provider, "m": model,
               "it": int(prompt_tokens), "ot": int(completion_tokens),
               "tt": total, "c": float(cost),
               "ch": 1 if cache_hit else 0}).fetchone()
        s.commit()
        return int(row[0])


def usage_summary(project_id: str | None = None) -> dict[str, Any]:
    where = []
    params: dict[str, Any] = {}
    if project_id:
        where.append("project_id = :p")
        params["p"] = project_id
    today = datetime.now(tz=timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0)
    last_30d = datetime.now(tz=timezone.utc) - timedelta(days=30)

    where_clause = f"WHERE {' AND '.join(where)}" if where else ""
    out: dict[str, Any] = {}
    with session_scope() as s:
        # Totals all-time
        row = s.execute(text(
            f"SELECT COUNT(*), ISNULL(SUM(total_tokens),0), "
            f"ISNULL(SUM(cost_usd),0), ISNULL(SUM(CASE WHEN cache_hit=1 THEN 1 ELSE 0 END),0) "
            f"FROM dbo.llm_usage {where_clause}"
        ), params).fetchone()
        out["all_time"] = {
            "calls": int(row[0]), "tokens": int(row[1]),
            "cost_usd": float(row[2]), "cache_hits": int(row[3]),
        }
        # Today
        today_params = {**params, "since": today}
        today_where = where_clause + (" AND " if where_clause else "WHERE ") + "created_at >= :since"
        row = s.execute(text(
            f"SELECT COUNT(*), ISNULL(SUM(total_tokens),0), "
            f"ISNULL(SUM(cost_usd),0) "
            f"FROM dbo.llm_usage {today_where}"
        ), today_params).fetchone()
        out["today"] = {
            "calls": int(row[0]), "tokens": int(row[1]),
            "cost_usd": float(row[2]),
        }
        # 30 days breakdown by model
        month_where = where_clause + (" AND " if where_clause else "WHERE ") + "created_at >= :since"
        month_params = {**params, "since": last_30d}
        rows = s.execute(text(
            f"SELECT model, COUNT(*), ISNULL(SUM(total_tokens),0), "
            f"ISNULL(SUM(cost_usd),0) "
            f"FROM dbo.llm_usage {month_where} GROUP BY model"
        ), month_params).fetchall()
        out["by_model_30d"] = [{
            "model": r[0], "calls": int(r[1]),
            "tokens": int(r[2]), "cost_usd": float(r[3]),
        } for r in rows]
    return out


def list_usage(project_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    where = []
    params: dict[str, Any] = {"lim": int(limit)}
    if project_id:
        where.append("project_id = :p")
        params["p"] = project_id
    where_clause = f"WHERE {' AND '.join(where)}" if where else ""
    with session_scope() as s:
        rows = s.execute(text(
            f"SELECT TOP (:lim) usage_id, project_id, purpose, provider, model, "
            f"prompt_tokens, completion_tokens, total_tokens, cost_usd, "
            f"cache_hit, created_at FROM dbo.llm_usage {where_clause} "
            f"ORDER BY usage_id DESC"
        ), params).fetchall()
    return [{
        "usage_id": int(r[0]),
        "project_id": r[1], "purpose": r[2], "provider": r[3],
        "model": r[4],
        "prompt_tokens": int(r[5]), "completion_tokens": int(r[6]),
        "total_tokens": int(r[7]), "cost_usd": float(r[8]),
        "cache_hit": bool(r[9]),
        "created_at": r[10].isoformat() if r[10] else None,
    } for r in rows]
