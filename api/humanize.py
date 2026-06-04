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
    """Pick the latest event for each pipeline node so the UI can render
    its state.

    Special case: the Executor node only runs when Guardrail approves at
    least one trade. When Guardrail blocks every trade, the user sees
    "Executor: Waiting" (or a stale OK from a prior cycle) with no
    indication that this cycle was decided + blocked. We post-process the
    Executor node to synthesize a "Blocked by Guardrail: ..." summary
    when the latest Guardrail decision in the current cycle approved 0
    trades and includes per-trade rejection reasons.
    """
    nodes = {
        "Scanner":    {"status": "idle", "summary": "Waiting", "ts": None, "kind": "pending"},
        "Strategist": {"status": "idle", "summary": "Waiting", "ts": None, "kind": "pending"},
        "Guardrail":  {"status": "idle", "summary": "Waiting", "ts": None, "kind": "pending"},
        "Executor":   {"status": "idle", "summary": "Waiting", "ts": None, "kind": "pending"},
    }
    latest_executor_ts: Any = None
    latest_guardrail_final: dict[str, Any] | None = None
    guardrail_rejections: list[dict[str, Any]] = []

    for e in events:  # events come newest-first
        n = e.get("node_name")
        et = e.get("event_type")
        payload = e.get("payload") or {}
        if isinstance(payload, dict) and n == "Guardrail" and et == "RISK":
            # Final summary: has approved_trades key. Per-trade rejection:
            # has rejected + reason keys.
            if "approved_trades" in payload and latest_guardrail_final is None:
                latest_guardrail_final = {**e, "payload": payload}
            elif "rejected" in payload and "reason" in payload:
                # Only collect rejections from the most recent cycle —
                # i.e. those that fall in the same time bucket as the
                # latest_guardrail_final. We accept any rejection newer
                # than the last Executor execution.
                guardrail_rejections.append({**e, "payload": payload})
        if n == "Executor" and et == "EXECUTE" and latest_executor_ts is None:
            latest_executor_ts = e.get("created_at")
        if n in nodes and nodes[n]["ts"] is None:
            h = humanize_event(e)
            nodes[n] = {
                "status": "active",
                "summary": h["message"],
                "ts": h["created_at"],
                "kind": h["kind"],
            }

    # Surface "Blocked by Guardrail" on the Executor card when this cycle
    # never reached the executor.
    if (latest_guardrail_final is not None
            and not (latest_guardrail_final["payload"].get("approved_trades") or [])):
        gts = latest_guardrail_final.get("created_at")
        # Newer than the last Executor.EXECUTE event? Then this cycle was
        # decided + blocked and the executor sat out.
        gts_newer = (latest_executor_ts is None) or (
            gts is not None and gts > latest_executor_ts
        )
        if gts_newer:
            # Collect per-trade rejection reasons captured in the same
            # cycle (i.e. newer than the last Executor.EXECUTE).
            relevant = [
                r for r in guardrail_rejections
                if (latest_executor_ts is None
                    or (r.get("created_at") is not None
                        and r["created_at"] > latest_executor_ts))
            ]
            # Dedupe by ticker — repeated cycles all reject the same
            # trades for the same reason. Showing each ticker once is
            # the useful signal.
            bits: list[str] = []
            seen_tickers: set[str] = set()
            for r in relevant:
                pl = r.get("payload") or {}
                rj = pl.get("rejected") or {}
                tk = (rj.get("ticker") or "?").upper()
                if tk in seen_tickers:
                    continue
                seen_tickers.add(tk)
                reason = (pl.get("reason") or "").split(":")[0].strip()
                if reason:
                    bits.append(f"{tk} ({reason})")
                else:
                    bits.append(tk)
                if len(bits) >= 4:
                    break
            if bits:
                msg = "Blocked by Guardrail: " + ", ".join(bits)
            else:
                # No Guardrail per-trade rejections — i.e. the Strategist
                # gave it 0 trades to evaluate. Look back at the latest
                # Strategist SELECTION events to find WHY everything got
                # skipped (economic event, all already-open, low IV, etc).
                msg = _strategist_skip_summary(events, gts) \
                    or "Blocked by Guardrail this cycle — no approved trades"
            nodes["Executor"] = {
                "status": "active",
                "summary": msg,
                "ts": gts,
                "kind": "guardrail-alert",
            }
    return nodes


# Maps a Strategist SELECTION outcome code to a short human label used
# on the Executor card when EVERY ticker got skipped for that reason.
_STRATEGIST_OUTCOME_LABELS: dict[str, str] = {
    "economic_event_skip": "scheduled macro event",
    "earnings_skip":       "earnings within window",
    "low_iv_rank":         "IV rank below floor",
    "negative_news":       "negative news sentiment",
    "no_snapshot":         "no market data",
    "no_contracts":        "no contracts in DTE band",
    "no_contract_in_envelope": "no contracts in delta band",
    "already_open":        "position already open",
    "rejected_by_llm":     "rejected by Claude",
    "recent_failure_skip": "broker rejected recently",
}


def _strategist_skip_summary(events: list[dict[str, Any]],
                              cutoff: Any) -> str:
    """Build "Strategist skipped: <reason>" for the cycle ending at
    ``cutoff`` (the Guardrail final-summary timestamp). Returns an
    empty string if we can't determine a useful reason.

    Strategy:
      * Tally SELECTION events from the most recent cycle by outcome.
      * If one outcome dominates (>= 70% of the count) surface it; if
        it was economic_event_skip, include the event name.
      * Otherwise list the top 2-3 outcomes so the user knows the
        ticker list got cut for mixed reasons.
    """
    from collections import Counter
    counts: Counter[str] = Counter()
    event_label = ""
    # SELECTIONs fire BEFORE the cycle's DECIDE. So "this cycle"'s
    # SELECTIONs live in the interval (prev_decide_ts, latest_decide_ts].
    # If there's no prior DECIDE in the event window we just take
    # everything up to the latest DECIDE.
    latest_decide_ts: Any = None
    prev_decide_ts: Any = None
    for e in events:
        if (e.get("node_name") == "Strategist"
                and e.get("event_type") == "DECIDE"):
            if latest_decide_ts is None:
                latest_decide_ts = e.get("created_at")
            else:
                prev_decide_ts = e.get("created_at")
                break
    for e in events:
        if (e.get("node_name") != "Strategist"
                or e.get("event_type") != "SELECTION"):
            continue
        ts = e.get("created_at")
        if (latest_decide_ts is not None and ts is not None
                and ts > latest_decide_ts):
            continue  # SELECTION from a NEWER cycle than the one we care about
        if (prev_decide_ts is not None and ts is not None
                and ts <= prev_decide_ts):
            break  # walked into a prior cycle — stop
        pl = e.get("payload") or {}
        outcome = pl.get("outcome") or ""
        if not outcome or outcome == "approved":
            continue
        counts[outcome] += 1
        if outcome == "economic_event_skip" and not event_label:
            event_label = str(pl.get("event") or "")
    if not counts:
        return ""
    total = sum(counts.values())
    top, top_n = counts.most_common(1)[0]
    if top_n / total >= 0.7:
        label = _STRATEGIST_OUTCOME_LABELS.get(top, top.replace("_", " "))
        if top == "economic_event_skip" and event_label:
            return f"Strategist skipped all {total} ticker(s): {event_label}"
        return (f"Strategist skipped all {total} ticker(s): {label}")
    top3 = counts.most_common(3)
    bits = [f"{_STRATEGIST_OUTCOME_LABELS.get(o, o.replace('_', ' '))}"
            f" ({n})" for o, n in top3]
    return f"Strategist skipped {total} ticker(s) — " + ", ".join(bits)
