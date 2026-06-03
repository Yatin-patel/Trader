"""Anomaly detection on event rates (Cat 10.3).

Compares the last hour's per-node event rate to the prior 24-hour baseline.
Flags spikes (3× baseline) or droughts (silent nodes that were previously
active). Persisted to anomalies and dispatched via the notifier.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text

from db.connection import session_scope

logger = logging.getLogger(__name__)


def detect_anomalies(project_id: str) -> list[dict[str, Any]]:
    now = datetime.now(tz=timezone.utc)
    hour_ago = now - timedelta(hours=1)
    day_ago = now - timedelta(hours=24)

    out: list[dict[str, Any]] = []

    with session_scope() as s:
        # Per-node-per-hour rate over last 24h.
        rows = s.execute(text("""
            SELECT node_name, event_type, COUNT(*) AS n
            FROM agent_events
            WHERE project_id = :p AND created_at >= :since
            GROUP BY node_name, event_type
        """), {"p": project_id, "since": day_ago}).fetchall()
        baseline_per_hour: dict[tuple[str, str], float] = {}
        for r in rows:
            baseline_per_hour[(r[0], r[1])] = float(r[2]) / 24.0

        last_hour_rows = s.execute(text("""
            SELECT node_name, event_type, COUNT(*) AS n
            FROM agent_events
            WHERE project_id = :p AND created_at >= :since
            GROUP BY node_name, event_type
        """), {"p": project_id, "since": hour_ago}).fetchall()
        observed: dict[tuple[str, str], int] = {}
        for r in last_hour_rows:
            observed[(r[0], r[1])] = int(r[2])

        # Spikes
        for key, obs in observed.items():
            base = baseline_per_hour.get(key, 0)
            if base < 1.0:
                continue  # too rare to flag meaningfully
            ratio = obs / base
            if ratio >= 3.0:
                out.append({
                    "kind": f"spike:{key[0]}.{key[1]}",
                    "severity": "warn",
                    "baseline_per_hour": round(base, 2),
                    "observed_last_hour": obs,
                    "ratio": round(ratio, 2),
                })

        # Droughts: previously busy, now silent
        for key, base in baseline_per_hour.items():
            if base < 2.0:
                continue
            if key in observed:
                continue
            out.append({
                "kind": f"drought:{key[0]}.{key[1]}",
                "severity": "warn",
                "baseline_per_hour": round(base, 2),
                "observed_last_hour": 0,
                "ratio": 0.0,
            })

    # Special check: ERROR storm in the last hour
    with session_scope() as s:
        row = s.execute(text("""
            SELECT COUNT(*) FROM agent_events
            WHERE project_id = :p AND event_type = 'ERROR'
              AND created_at >= :since
        """), {"p": project_id, "since": hour_ago}).fetchone()
        n = int(row[0] or 0)
        if n >= 10:
            out.append({
                "kind": "error_storm:1h",
                "severity": "error",
                "baseline_per_hour": 0,
                "observed_last_hour": n,
                "ratio": float(n),
            })

    # Persist
    if out:
        with session_scope() as s:
            import json as _json
            for item in out:
                s.execute(text("""
                    INSERT INTO anomalies
                        (project_id, kind, severity,
                         baseline_value, observed_value, details)
                    VALUES (:p, :k, :sev, :bv, :ov, :d)
                """), {"p": project_id, "k": item["kind"],
                       "sev": item["severity"],
                       "bv": item["baseline_per_hour"],
                       "ov": item["observed_last_hour"],
                       "d": _json.dumps(item)})
            s.commit()
        # Fire critical anomalies through the notifier
        try:
            from notifications.dispatcher import dispatch
            critical = [a for a in out if a["severity"] in ("error", "critical")]
            if critical:
                dispatch(project_id,
                         title=f"⚠️  Anomalies detected ({len(critical)})",
                         body="\n".join(f"  {a['kind']} → {a['observed_last_hour']} "
                                        f"(baseline {a['baseline_per_hour']}/h)"
                                        for a in critical),
                         severity="error", event_type="ANOMALY",
                         payload={"anomalies": critical})
        except Exception:
            logger.exception("anomaly notifier dispatch failed")
    return out


def list_anomalies(project_id: str, limit: int = 50) -> list[dict[str, Any]]:
    with session_scope() as s:
        rows = s.execute(text("""
            SELECT anomaly_id, kind, detected_at, severity,
                   baseline_value, observed_value, details
            FROM anomalies
            WHERE project_id = :p
            ORDER BY anomaly_id DESC
            LIMIT :lim
        """), {"p": project_id, "lim": int(limit)}).fetchall()
    import json as _json
    out = []
    for r in rows:
        try:
            details = _json.loads(r[6]) if r[6] else None
        except Exception:
            details = None
        out.append({
            "anomaly_id": int(r[0]),
            "kind": r[1],
            "detected_at": r[2].isoformat() if r[2] else None,
            "severity": r[3],
            "baseline_value": float(r[4]) if r[4] is not None else None,
            "observed_value": float(r[5]) if r[5] is not None else None,
            "details": details,
        })
    return out
