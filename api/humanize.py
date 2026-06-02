"""Translate raw agent_events JSON into human-readable activity lines."""
from __future__ import annotations

from typing import Any


def _fmt_money(v: Any) -> str:
    try:
        return f"${float(v):,.2f}"
    except Exception:
        return str(v)


def _short_ticker_list(items: list[Any], limit: int = 6) -> str:
    if not items:
        return "none"
    head = ", ".join(str(x) for x in items[:limit])
    extra = len(items) - limit
    return head + (f" (+{extra} more)" if extra > 0 else "")


def humanize_event(event: dict[str, Any]) -> dict[str, Any]:
    node = event.get("node_name", "")
    et = event.get("event_type", "")
    payload = event.get("payload") or {}
    if isinstance(payload, str):
        payload = {"raw": payload}

    icon, kind, message = "•", "info", f"{node} {et}"

    if node == "Scanner" and et == "SCAN":
        icon, kind = "🔍", "scan"
        narrative = payload.get("narrative") or []
        if narrative:
            message = "\n".join(narrative)
        else:
            message = (
                f"Scanned {payload.get('universe_size', '?')} tickers, "
                f"{payload.get('passed_filters', 0)} passed filters → "
                f"{_short_ticker_list(payload.get('selected', []))}"
            )
    elif node == "Strategist" and et == "SELECTION":
        outcome = payload.get("outcome", "")
        if outcome == "approved":
            icon, kind = "🎯", "decide"
        elif outcome == "rejected_by_llm":
            icon, kind = "🚫", "decide"
        else:
            icon, kind = "🔎", "decide"
        narrative = payload.get("narrative") or []
        message = "\n".join(narrative) if narrative else f"Selection {outcome}"
    elif node == "Strategist" and et == "DECIDE":
        sel = payload.get("selected", []) or []
        cands = payload.get("candidates", []) or []
        rejections = payload.get("rejections", []) or []
        if not sel:
            # Surface the first rejection reason so the user knows *why*
            reason_bits = []
            for r in rejections[:3]:
                reason_bits.append(f"{r.get('ticker','?')}: {r.get('reason','?')}")
            extra = "" if not reason_bits else " — " + "; ".join(reason_bits)
            icon, kind = "🤔", "decide"
            message = (
                f"Evaluated {len(cands)} candidate(s); 0 approved{extra}"
            )
        else:
            icon, kind = "✅", "decide"
            picks = "; ".join(
                f"{t.get('ticker','?')} {t.get('type','?')} strike {t.get('strike','?')}"
                + (f" Δ{float(t['delta']):+.2f}" if t.get('delta') is not None else "")
                for t in sel
            )
            message = f"Selected {len(sel)} trade(s): {picks}"
    elif node == "Guardrail" and et == "RISK":
        bp = payload.get("buying_power", 0)
        approved = payload.get("approved_trades", []) or []
        actions = payload.get("actions", []) or []
        bits = [f"buying power {_fmt_money(bp)}", f"{len(approved)} trade(s) approved"]
        if actions:
            stops = sum(1 for a in actions if a.get("action") in ("liquidated", "would_liquidate"))
            if stops:
                bits.append(f"{stops} stop-loss action(s)")
        icon = "⚠️" if actions else "🛡️"
        kind = "guardrail-alert" if actions else "guardrail"
        message = "; ".join(bits)
    elif node == "Executor" and et == "EXECUTE":
        results = payload.get("results", []) or []
        if not results:
            icon, kind = "💤", "execute"
            message = "Nothing to execute this cycle"
        else:
            tally: dict[str, int] = {}
            for r in results:
                k = r.get("status", "?")
                tally[k] = tally.get(k, 0) + 1
            icon, kind = ("📦", "execute-dry") if "DRY_RUN" in tally else ("📤", "execute")
            message = f"{len(results)} order(s): " + ", ".join(
                f"{k}×{v}" for k, v in tally.items()
            )
    elif node == "Worker" and et == "LOOP":
        if payload.get("skipped") == "market_closed":
            icon, kind = "🌙", "closed"
            wait = payload.get("sleeping_seconds")
            nxt = payload.get("next_open")
            if nxt:
                message = f"Market closed — sleeping until {nxt}"
            elif wait:
                message = f"Market closed — sleeping {wait}s"
            else:
                message = "Market closed"
        else:
            icon, kind = "🔁", "cycle"
            message = (
                f"Cycle {payload.get('cycle', '?')} complete, "
                f"{payload.get('trades', 0)} trade(s)"
            )
    elif node == "Analytics" and et == "CLOSURE":
        narrative = payload.get("narrative") or []
        pnl = payload.get("realized_pnl", 0)
        icon = "💰" if pnl > 0 else ("🔻" if pnl < 0 else "📕")
        kind = "execute" if pnl >= 0 else "guardrail-alert"
        if narrative:
            message = "\n".join(narrative)
        else:
            ticker = payload.get("ticker", "?")
            reason = payload.get("reason", "closed")
            message = f"{ticker} closed ({reason}) → P&L ${pnl:+.2f}"
    elif node == "Admin" and et == "RESET_PAPER":
        icon, kind = "💰", "admin"
        message = f"Reset paper account to {_fmt_money(payload.get('cash', 0))}"
    elif et == "ERROR":
        icon, kind = "❌", "error"
        message = f"Error in {node}: {payload.get('err', 'unknown')}"

    return {
        "event_id": event.get("event_id"),
        "node": node,
        "event_type": et,
        "icon": icon,
        "kind": kind,
        "message": message,
        "created_at": event.get("created_at"),
    }


def summarize_pipeline(events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Pick the latest event for each pipeline node so the UI can render its state."""
    nodes = {
        "Scanner":    {"status": "idle", "summary": "Waiting", "ts": None, "kind": "pending"},
        "Strategist": {"status": "idle", "summary": "Waiting", "ts": None, "kind": "pending"},
        "Guardrail":  {"status": "idle", "summary": "Waiting", "ts": None, "kind": "pending"},
        "Executor":   {"status": "idle", "summary": "Waiting", "ts": None, "kind": "pending"},
    }
    for e in events:  # events come newest-first
        n = e.get("node_name")
        if n in nodes and nodes[n]["ts"] is None:
            h = humanize_event(e)
            nodes[n] = {
                "status": "active",
                "summary": h["message"],
                "ts": h["created_at"],
                "kind": h["kind"],
            }
    return nodes
