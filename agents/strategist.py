"""Agent 2 — Underwriter / Wheel Strategist.

Claude evaluates each candidate ticker and chooses a wheel action:
  * Sell CASH_SECURED_PUT when no position exists       (delta in [csp_delta_min, csp_delta_max])
  * Sell COVERED_CALL when shares are assigned          (delta in [cc_delta_min, cc_delta_max])
  * Skip when nothing fits the configured envelope

All thresholds are loaded from project_settings — none are hardcoded here.
"""
from __future__ import annotations

import json
import logging
from datetime import date
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

from db.repositories import EventsRepo, ProjectsRepo, WheelRepo
from analytics.iv_rank import get_iv_rank, passes_iv_filter
from db.settings_store import ProjectSettings, effective_csp_band
from risk.earnings import upcoming_earnings_within
from risk.news import passes_news_filter
from execution import get_broker

logger = logging.getLogger(__name__)


def _build_llm() -> BaseChatModel | None:
    """Return a configured chat model for the chosen provider, or None."""
    from .llm_factory import build_llm
    return build_llm()


def _select_contract(quotes: dict[str, Any], contracts: list[dict[str, Any]],
                     delta_lo: float, delta_hi: float, side: str) -> dict[str, Any] | None:
    """Pick the contract that maximizes premium yield within the delta band.

    Returns the winning contract enriched with `mid`, `yield`, `spread_ratio`,
    `selection_narrative` (plain-English steps), and `runners_up` (next 4
    candidates) so the caller can write a clear audit log.
    """
    inspected = len(contracts)
    pass_filters: list[dict[str, Any]] = []
    drop_reasons: list[str] = []

    for c in contracts:
        sym = c["symbol"]
        q = quotes.get(sym) or {}
        if q.get("delta") is None:
            drop_reasons.append(f"{sym}: no greeks/quote available")
            continue
        delta_abs = abs(q["delta"])
        if not (delta_lo <= delta_abs <= delta_hi):
            drop_reasons.append(
                f"{sym}: delta {delta_abs:.3f} outside band [{delta_lo}-{delta_hi}]"
            )
            continue
        bid = q.get("bid") or 0.0
        ask = q.get("ask") or 0.0
        if bid <= 0 or ask <= 0:
            drop_reasons.append(f"{sym}: bid/ask not tradeable (bid={bid}, ask={ask})")
            continue
        mid = (bid + ask) / 2.0
        strike = float(c.get("strike") or 0.0)
        if strike <= 0:
            drop_reasons.append(f"{sym}: invalid strike {strike}")
            continue
        spread_ratio = (ask - bid) / mid if mid > 0 else 1.0
        if spread_ratio > 0.25:
            drop_reasons.append(
                f"{sym}: spread {spread_ratio*100:.1f}% wider than 25% cap"
            )
            continue
        yield_pct = mid / strike
        # Expected-value score (preferred when delta is present):
        #   EV = mid * P(OTM) - assignment_cost * P(ITM)
        # We approximate P(ITM) ≈ |delta| for short options (standard
        # textbook approximation; rough but better than yield-only). The
        # assignment_cost is the strike (we'd own shares at strike, no
        # immediate loss but capital is tied up). We discount it by a
        # nominal 1% cost-of-capital so the math favors not getting
        # assigned without rejecting every trade.
        delta_abs_v = abs(q.get("delta") or 0)
        p_itm = max(0.0, min(0.95, delta_abs_v))   # cap for safety
        p_otm = 1.0 - p_itm
        ev_per_share = mid * p_otm - (strike * 0.01) * p_itm
        # Normalize EV by strike so different price ranges compare fairly.
        ev_score = ev_per_share / strike if strike > 0 else 0.0
        # Final score blends EV with yield (2/3 EV, 1/3 yield) and gives
        # a small bonus for tight spreads. Falling back to plain yield
        # when delta is missing keeps the old behavior for chains without
        # greeks.
        if delta_abs_v > 0:
            score = (2.0 / 3.0) * ev_score + (1.0 / 3.0) * yield_pct
        else:
            score = yield_pct
        score -= spread_ratio * 0.01
        pass_filters.append({
            **c, **q,
            "mid": mid,
            "yield": yield_pct,
            "spread_ratio": spread_ratio,
            "p_itm": p_itm,
            "ev_per_share": ev_per_share,
            "ev_score": ev_score,
            "score": score,
        })

    if not pass_filters:
        return None

    pass_filters.sort(key=lambda x: x["score"], reverse=True)
    chosen = dict(pass_filters[0])
    runners = pass_filters[1:5]

    # Plain-English audit trail for THIS selection.
    narrative: list[str] = []
    narrative.append(
        f"Considered {inspected} {side} contract(s) returned by Alpaca."
    )
    narrative.append(
        f"{len(pass_filters)} survived the delta band [{delta_lo}-{delta_hi}], "
        f"tradeable bid/ask, and ≤25% spread filter."
    )
    narrative.append(
        "Ranked by premium yield (mid premium ÷ strike, with tiny spread penalty):"
    )
    label_top = "PICKED"
    for rank, c in enumerate([chosen] + runners, 1):
        label = label_top if rank == 1 else f"#{rank}"
        delta_str = f"Δ {abs(c.get('delta', 0)):.3f}"
        narrative.append(
            f"  {label}: {c.get('symbol')} "
            f"strike ${c.get('strike'):.2f} exp {c.get('expiration')} | "
            f"{delta_str} | mid ${c.get('mid'):.2f} | "
            f"yield {c.get('yield', 0)*100:.2f}% | "
            f"spread {c.get('spread_ratio', 0)*100:.1f}% | "
            f"OI {c.get('open_interest', '?')}"
        )
    if runners:
        gap = (chosen.get("yield", 0) - runners[0].get("yield", 0)) * 100
        narrative.append(
            f"Winner beats runner-up by {gap:.2f} percentage points of yield."
        )
    if drop_reasons:
        sample = drop_reasons[:4]
        narrative.append("Rejected examples: " + "; ".join(sample))
        if len(drop_reasons) > 4:
            narrative.append(f"  …plus {len(drop_reasons)-4} more rejected.")

    chosen["selection_narrative"] = narrative
    chosen["runners_up"] = [
        {k: r.get(k) for k in ("symbol", "strike", "expiration",
                               "delta", "mid", "yield", "spread_ratio",
                               "open_interest")}
        for r in runners
    ]
    return chosen


def _existing_phase(project_id: str, ticker: str, equity_positions: dict[str, dict[str, Any]]) -> str:
    """Decide which wheel phase this ticker is currently in."""
    if ticker in equity_positions and equity_positions[ticker]["qty"] >= 100:
        return "STOCK_ASSIGNED"
    open_contracts = WheelRepo.list_open(project_id)
    for c in open_contracts:
        if c["ticker"] == ticker:
            return c["strategy_phase"]
    return "NONE"


def _recently_failed_tickers(project_id: str,
                             window_minutes: int = 60) -> set[str]:
    """Return tickers that hit Executor.EXECUTE ERROR within the window.

    Used by the Strategist to suppress re-picking a trade whose order
    keeps getting rejected by the broker (insufficient BP, halted symbol,
    rejected strike, etc.). Without this the same broken trade burns one
    Alpaca API call every cycle until the underlying condition changes.
    """
    from datetime import datetime, timedelta, timezone
    cutoff = datetime.now(tz=timezone.utc) - timedelta(minutes=window_minutes)
    failed: set[str] = set()
    try:
        events = EventsRepo.recent(project_id, limit=80)
    except Exception:
        return failed
    for e in events:
        if e.get("node_name") != "Executor":
            continue
        ts = e.get("created_at")
        if ts is None:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if ts < cutoff:
            continue
        payload = e.get("payload") or {}
        for r in (payload.get("results") or []):
            status = str(r.get("status") or "").upper()
            if status not in ("ERROR", "REJECTED"):
                continue
            trade = r.get("trade") or {}
            tk = trade.get("ticker")
            if tk:
                failed.add(tk.upper())
    return failed


def analyze_wheel_node(state: dict[str, Any]) -> dict[str, Any]:
    project_id = state["project_id"]
    tickers: list[str] = state.get("target_tickers", []) or []
    if not tickers:
        return {"selected_trades": []}

    project = ProjectsRepo.get(project_id)
    if project is None:
        return {"selected_trades": []}

    # Cadence-aware DTE/delta selection. When income_cadence is set to a
    # preset (weekly|biweekly|monthly) the band overrides csp_min_dte /
    # csp_max_dte / csp_delta_min / csp_delta_max. When it's 'custom', the
    # band is just the stored csp_* values.
    band = effective_csp_band(project_id)
    csp_lo = band["delta_min"]
    csp_hi = band["delta_max"]
    csp_min_dte = band["min_dte"]
    csp_max_dte = band["max_dte"]
    cc_lo = ProjectSettings.get(project_id, "cc_delta_min")
    cc_hi = ProjectSettings.get(project_id, "cc_delta_max")
    max_contracts = ProjectSettings.get(project_id, "max_open_contracts")

    client = get_broker(project)
    live_positions = client.list_positions()
    equity_positions = {p["symbol"]: p for p in live_positions
                        if p["asset_class"] == "us_equity"}
    # Set of OCC option symbols currently held at the broker, regardless
    # of side (long or short). If we'd open the exact same contract,
    # Alpaca rejects with "cannot open a short sell while a long buy
    # order is open" (or vice versa). Catching it here avoids the
    # 4xx and the ERROR×1 banner that follows.
    broker_held_options: set[str] = {
        str(p.get("symbol") or "").upper()
        for p in live_positions
        if p.get("asset_class") != "us_equity"
    }

    open_contracts = WheelRepo.list_open(project_id)
    if len(open_contracts) >= max_contracts:
        EventsRepo.log(project_id, "Strategist", "DECIDE",
                       {"skip_reason": "max_open_contracts reached", "open": len(open_contracts)})
        return {"selected_trades": []}

    llm = _build_llm()
    trades: list[dict[str, Any]] = []
    rejections: list[dict[str, Any]] = []

    earnings_dte = int(ProjectSettings.get(project_id, "avoid_earnings_within_dte") or 0)
    min_iv_rank = float(ProjectSettings.get(project_id, "min_iv_rank", default=0.0) or 0.0)
    news_filter_on = bool(ProjectSettings.get(project_id, "news_sentiment_filter", default=False))
    news_min_score = float(ProjectSettings.get(project_id, "news_sentiment_min", default=-0.30))
    # Market-wide economic-event gate — applied ONCE per cycle for all
    # tickers (it's the same regardless of underlying).
    skip_event_days = int(ProjectSettings.get(
        project_id, "skip_event_days_within", default=3) or 0)
    event_kinds: list[str] = []
    if ProjectSettings.get(project_id, "skip_on_fomc_days", default=True):
        event_kinds.append("fomc")
    if ProjectSettings.get(project_id, "skip_on_cpi_days", default=True):
        event_kinds.append("cpi")
    if ProjectSettings.get(project_id, "skip_on_nfp_days", default=True):
        event_kinds.append("nfp")
    if ProjectSettings.get(project_id, "skip_on_pce_days", default=False):
        event_kinds.append("pce")
    blocking_event = ""
    if skip_event_days > 0 and event_kinds:
        from risk.economic_calendar import is_event_within
        hit, label = is_event_within(skip_event_days, kinds=event_kinds)
        if hit:
            blocking_event = label

    # Tickers whose most-recent order submission failed at the broker
    # within the last hour. Re-picking them just burns Alpaca API calls.
    recent_fail_minutes = int(ProjectSettings.get(
        project_id, "recent_failure_skip_minutes", default=60) or 0)
    failed_tickers: set[str] = set()
    if recent_fail_minutes > 0:
        failed_tickers = _recently_failed_tickers(
            project_id, window_minutes=recent_fail_minutes)

    for ticker in tickers:
        try:
            phase = _existing_phase(project_id, ticker, equity_positions)

            # Recent broker-side failure on this ticker — back off.
            if ticker.upper() in failed_tickers:
                rejections.append({"ticker": ticker,
                                   "reason": f"recent execution failure "
                                             f"(skipping for {recent_fail_minutes} min)"})
                EventsRepo.log(project_id, "Strategist", "SELECTION", {
                    "ticker": ticker, "outcome": "recent_failure_skip",
                    "narrative": [
                        f"Skipping {ticker}: a previous order submission "
                        f"for this underlying was rejected by the broker "
                        f"in the last {recent_fail_minutes} minute(s). "
                        "Not retrying until the cooldown elapses.",
                    ],
                })
                continue

            # Market-wide economic event gate: blocks ALL new positions
            # in the days leading up to FOMC / CPI / NFP. Applies even
            # before snapshot fetch — no point burning API calls.
            if blocking_event:
                rejections.append({"ticker": ticker,
                                   "reason": f"economic event: {blocking_event}"})
                EventsRepo.log(project_id, "Strategist", "SELECTION", {
                    "ticker": ticker, "outcome": "economic_event_skip",
                    "event": blocking_event,
                    "narrative": [
                        f"Skipping {ticker}: market-wide economic event "
                        f"({blocking_event}) within "
                        f"{skip_event_days} day(s). Binary intraday vol "
                        "could break the delta/IV thesis.",
                    ],
                })
                continue

            snap = client.snapshots([ticker]).get(ticker)
            if snap is None or snap.last_price <= 0:
                rejections.append({"ticker": ticker, "reason": "no snapshot"})
                EventsRepo.log(project_id, "Strategist", "SELECTION", {
                    "ticker": ticker, "outcome": "no_snapshot",
                    "narrative": [f"No market snapshot returned for {ticker}; skipping."],
                })
                continue

            # IV-rank filter: skip low-IV tickers (premium not worth the risk).
            if min_iv_rank > 0 and not passes_iv_filter(project_id, ticker, min_iv_rank):
                iv = get_iv_rank(project_id, ticker)
                rejections.append({"ticker": ticker,
                                   "reason": f"IV rank {iv if iv is None else f'{iv:.2f}'} "
                                             f"below floor {min_iv_rank}"})
                EventsRepo.log(project_id, "Strategist", "SELECTION", {
                    "ticker": ticker, "outcome": "low_iv_rank",
                    "iv_rank": iv,
                    "narrative": [
                        f"Skipping {ticker}: 30-day realized-vol rank "
                        f"{iv if iv is None else f'{iv:.2f}'} below configured "
                        f"floor {min_iv_rank}. Premium is not rich enough.",
                    ],
                })
                continue

            # News sentiment filter: skip ticker if recent news is strongly negative.
            if news_filter_on:
                ok, reason = passes_news_filter(ticker, news_min_score, news_filter_on)
                if not ok:
                    rejections.append({"ticker": ticker, "reason": reason})
                    EventsRepo.log(project_id, "Strategist", "SELECTION", {
                        "ticker": ticker, "outcome": "negative_news",
                        "narrative": [
                            f"Skipping {ticker}: {reason}",
                        ],
                    })
                    continue

            # Earnings filter: skip tickers reporting inside our DTE window.
            if earnings_dte > 0 and upcoming_earnings_within(ticker, earnings_dte):
                rejections.append({"ticker": ticker,
                                   "reason": f"earnings within {earnings_dte} days"})
                EventsRepo.log(project_id, "Strategist", "SELECTION", {
                    "ticker": ticker,
                    "underlying_price": snap.last_price,
                    "outcome": "earnings_skip",
                    "narrative": [
                        f"Skipping {ticker}: earnings event within "
                        f"{earnings_dte} days. Wheel discipline says avoid binary"
                        " catastrophe risk on the underlying.",
                    ],
                })
                continue

            # We already have a position on this ticker — log why we're not adding more.
            if phase not in ("NONE", "STOCK_ASSIGNED"):
                EventsRepo.log(project_id, "Strategist", "SELECTION", {
                    "ticker": ticker,
                    "underlying_price": snap.last_price,
                    "outcome": "already_open",
                    "phase": phase,
                    "narrative": [
                        f"Skipping {ticker}: an open {phase} contract is already on the books.",
                        f"Underlying is at ${snap.last_price:.2f}. The wheel will reassess this ticker once the current contract closes or expires.",
                    ],
                })
                continue

            if phase == "NONE":
                min_strike = snap.last_price * 0.80
                max_strike = snap.last_price * 1.02
                contracts = client.list_option_contracts(ticker, "put",
                                                         csp_min_dte, csp_max_dte,
                                                         min_strike=min_strike,
                                                         max_strike=max_strike)
                # Drop any contract the broker is ALREADY holding (in
                # either direction). Alpaca rejects a new short against
                # an existing long on the same OCC symbol with a
                # "cannot open a short sell while a long buy order is
                # open" 4xx; catching it here keeps the error off the
                # activity feed and lets the Strategist pick a different
                # strike on the same ticker.
                conflict_drops = [c for c in contracts
                                  if str(c.get("symbol") or "").upper()
                                  in broker_held_options]
                if conflict_drops:
                    contracts = [c for c in contracts
                                 if str(c.get("symbol") or "").upper()
                                 not in broker_held_options]
                if not contracts:
                    rejections.append({"ticker": ticker, "reason": "no put contracts in DTE window"})
                    EventsRepo.log(project_id, "Strategist", "SELECTION", {
                        "ticker": ticker, "kind": "CASH_SECURED_PUT",
                        "underlying_price": snap.last_price,
                        "outcome": "no_contracts",
                        "narrative": [
                            f"Looking for {ticker} cash-secured puts.",
                            f"Underlying at ${snap.last_price:.2f}; strikes searched ${min_strike:.2f}-${max_strike:.2f}, DTE {csp_min_dte}-{csp_max_dte}.",
                            "Alpaca returned no contracts in that window — moving on.",
                        ],
                    })
                    continue
                quotes = client.option_chain_quotes(ticker)
                chosen = _select_contract(quotes, contracts, csp_lo, csp_hi, "sell")
                if not chosen:
                    rejections.append({"ticker": ticker, "reason": f"no put in delta band [{csp_lo}, {csp_hi}]"})
                    EventsRepo.log(project_id, "Strategist", "SELECTION", {
                        "ticker": ticker, "kind": "CASH_SECURED_PUT",
                        "underlying_price": snap.last_price,
                        "outcome": "no_contract_in_envelope",
                        "narrative": [
                            f"Looking for {ticker} cash-secured puts.",
                            f"Underlying at ${snap.last_price:.2f}; received {len(contracts)} contract(s) from Alpaca.",
                            f"None fit the configured delta band [{csp_lo}-{csp_hi}] with a tradeable bid/ask and ≤25% spread.",
                            "Skipping this ticker for the cycle.",
                        ],
                    })
                    continue
                decision = _strategist_reason(llm, ticker, "CASH_SECURED_PUT",
                                              snap.last_price, chosen, csp_lo, csp_hi)
                outcome = "approved" if decision.get("approve") else "rejected_by_llm"
                selection_narrative = list(chosen.get("selection_narrative", []))
                selection_narrative.insert(0,
                    f"Evaluating {ticker} for a CASH_SECURED_PUT. Underlying = ${snap.last_price:.2f}.")
                selection_narrative.append(
                    f"LLM verdict: {outcome.upper()} — {decision.get('rationale', '(no rationale)')}"
                )
                EventsRepo.log(project_id, "Strategist", "SELECTION", {
                    "ticker": ticker, "kind": "CASH_SECURED_PUT",
                    "underlying_price": snap.last_price,
                    "outcome": outcome,
                    "chosen": {k: chosen.get(k) for k in ("symbol", "strike",
                                                           "expiration", "delta",
                                                           "mid", "yield",
                                                           "spread_ratio",
                                                           "open_interest")},
                    "runners_up": chosen.get("runners_up"),
                    "llm_rationale": decision.get("rationale"),
                    "narrative": selection_narrative,
                })
                if not decision.get("approve"):
                    rejections.append({"ticker": ticker, "reason": decision.get("rationale", "LLM rejected")})
                    continue
                trades.append({
                    "ticker": ticker,
                    "type": "CSP",
                    "option_symbol": chosen["symbol"],
                    "strike": chosen["strike"],
                    "expiration": chosen["expiration"].isoformat() if isinstance(chosen["expiration"], date) else str(chosen["expiration"]),
                    "delta": chosen.get("delta"),
                    # Pass real vega + iv from the quote so the Guardrail's
                    # net-vega cap math uses live greeks instead of the
                    # 0.10*underlying fallback (which over-estimated short-
                    # put vega by ~50x and caused false vega-cap rejections).
                    "vega": chosen.get("vega"),
                    "iv": chosen.get("iv"),
                    "premium": chosen.get("bid") or chosen.get("mid"),
                    "underlying_price": snap.last_price,
                    "rationale": decision.get("rationale", ""),
                })

            elif phase == "STOCK_ASSIGNED":
                # Prefer adjusted cost basis (entry strike − accumulated premium)
                # if we have it on either the wheel cycle or the stock position.
                cost_basis = float(equity_positions[ticker].get("avg_entry_price") or snap.last_price)
                try:
                    from analytics.wheel_cycles import get_open_cycle
                    cyc = get_open_cycle(project_id, ticker)
                    if cyc and cyc.get("cost_basis_adjusted") is not None:
                        cost_basis = float(cyc["cost_basis_adjusted"])
                except Exception:
                    pass
                min_strike = max(snap.last_price * 1.00, cost_basis)
                max_strike = snap.last_price * 1.30
                contracts = client.list_option_contracts(ticker, "call",
                                                         csp_min_dte, csp_max_dte,
                                                         min_strike=min_strike,
                                                         max_strike=max_strike)
                # Drop any OCC symbol already held at the broker (same
                # rationale as the CSP branch above).
                contracts = [c for c in contracts
                             if str(c.get("symbol") or "").upper()
                             not in broker_held_options]
                if not contracts:
                    rejections.append({"ticker": ticker, "reason": "no call contracts in DTE window"})
                    EventsRepo.log(project_id, "Strategist", "SELECTION", {
                        "ticker": ticker, "kind": "COVERED_CALL",
                        "underlying_price": snap.last_price,
                        "outcome": "no_contracts",
                        "narrative": [
                            f"Holding shares of {ticker} (cost ${cost_basis:.2f}); looking for a covered call to write.",
                            f"Strikes searched ${min_strike:.2f}-${max_strike:.2f}, DTE {csp_min_dte}-{csp_max_dte}.",
                            "No contracts available in that window.",
                        ],
                    })
                    continue
                quotes = client.option_chain_quotes(ticker)
                chosen = _select_contract(quotes, contracts, cc_lo, cc_hi, "sell")
                if not chosen:
                    rejections.append({"ticker": ticker, "reason": f"no call in delta band [{cc_lo}, {cc_hi}]"})
                    EventsRepo.log(project_id, "Strategist", "SELECTION", {
                        "ticker": ticker, "kind": "COVERED_CALL",
                        "underlying_price": snap.last_price,
                        "outcome": "no_contract_in_envelope",
                        "narrative": [
                            f"Looking for a {ticker} covered call (cost basis ${cost_basis:.2f}).",
                            f"Received {len(contracts)} call(s); none fit delta band [{cc_lo}-{cc_hi}] with acceptable spread.",
                            "Skipping this ticker for the cycle.",
                        ],
                    })
                    continue
                decision = _strategist_reason(llm, ticker, "COVERED_CALL",
                                              snap.last_price, chosen, cc_lo, cc_hi,
                                              cost_basis=cost_basis)
                outcome = "approved" if decision.get("approve") else "rejected_by_llm"
                selection_narrative = list(chosen.get("selection_narrative", []))
                selection_narrative.insert(0,
                    f"Evaluating {ticker} for a COVERED_CALL. Underlying ${snap.last_price:.2f}, cost basis ${cost_basis:.2f}.")
                selection_narrative.append(
                    f"LLM verdict: {outcome.upper()} — {decision.get('rationale', '(no rationale)')}"
                )
                EventsRepo.log(project_id, "Strategist", "SELECTION", {
                    "ticker": ticker, "kind": "COVERED_CALL",
                    "underlying_price": snap.last_price,
                    "cost_basis": cost_basis,
                    "outcome": outcome,
                    "chosen": {k: chosen.get(k) for k in ("symbol", "strike",
                                                           "expiration", "delta",
                                                           "mid", "yield",
                                                           "spread_ratio",
                                                           "open_interest")},
                    "runners_up": chosen.get("runners_up"),
                    "llm_rationale": decision.get("rationale"),
                    "narrative": selection_narrative,
                })
                if not decision.get("approve"):
                    rejections.append({"ticker": ticker, "reason": decision.get("rationale", "LLM rejected")})
                    continue

                base_trade = {
                    "ticker": ticker,
                    "type": "CC",
                    "option_symbol": chosen["symbol"],
                    "strike": chosen["strike"],
                    "expiration": chosen["expiration"].isoformat() if isinstance(chosen["expiration"], date) else str(chosen["expiration"]),
                    "delta": chosen.get("delta"),
                    "vega": chosen.get("vega"),
                    "iv": chosen.get("iv"),
                    "premium": chosen.get("bid") or chosen.get("mid"),
                    "underlying_price": snap.last_price,
                    "rationale": decision.get("rationale", ""),
                }
                trades.append(base_trade)

                # ----- 6.3 Pyramiding -----------------------------------------
                # Only pyramid when we hold enough shares to back N CCs.
                pyramid_n = int(ProjectSettings.get(project_id, "cc_pyramid_levels", default=1) or 1)
                shares_held = int(equity_positions[ticker].get("qty") or 0)
                max_pyramid_by_shares = shares_held // 100
                pyramid_n = min(pyramid_n, max(1, max_pyramid_by_shares))
                if pyramid_n > 1:
                    spacing = float(ProjectSettings.get(project_id, "cc_pyramid_spacing_pct", default=0.03) or 0.03)
                    # Pick higher strikes from the same chain for additional rungs.
                    # Sort by strike ascending; iterate from chosen strike upward.
                    chain_sorted = sorted(
                        [c for c in contracts if float(c["strike"]) > float(chosen["strike"])],
                        key=lambda c: float(c["strike"]),
                    )
                    added = 1
                    for c in chain_sorted:
                        if added >= pyramid_n:
                            break
                        next_strike_target = float(chosen["strike"]) * (1 + added * spacing)
                        if float(c["strike"]) < next_strike_target:
                            continue
                        q = quotes.get(c["symbol"]) or {}
                        bid = q.get("bid") or 0.0
                        ask = q.get("ask") or 0.0
                        if bid <= 0 or ask <= 0:
                            continue
                        mid = (bid + ask) / 2.0
                        trades.append({
                            "ticker": ticker,
                            "type": "CC",
                            "option_symbol": c["symbol"],
                            "strike": c["strike"],
                            "expiration": c["expiration"].isoformat() if isinstance(c["expiration"], date) else str(c["expiration"]),
                            "delta": q.get("delta"),
                            "vega": q.get("vega"),
                            "iv": q.get("iv"),
                            "premium": mid,
                            "underlying_price": snap.last_price,
                            "rationale": f"pyramid rung #{added + 1} of {pyramid_n} (+{(added * spacing)*100:.1f}%)",
                        })
                        added += 1
                    if added > 1:
                        EventsRepo.log(project_id, "Strategist", "SELECTION", {
                            "ticker": ticker, "kind": "CC_PYRAMID",
                            "underlying_price": snap.last_price,
                            "outcome": "approved",
                            "narrative": [
                                f"CC pyramiding ON: holding {shares_held} shares of {ticker}, "
                                f"writing {added} CCs at staggered strikes (spacing {spacing*100:.1f}%).",
                            ],
                        })

        except Exception as _strat_err:
            err_text = str(_strat_err)
            # Alpaca 42210000 = "invalid underlying symbols". These are
            # structural problems (delisted, renamed, or never existed)
            # that fire on EVERY cycle and will never self-resolve. Demote
            # to a routine SELECTION skip so the error banner stops
            # popping every 5 minutes. The user can fix it by editing
            # the watchlist; meanwhile we don't pollute the activity feed.
            is_invalid_symbol = (
                "invalid underlying" in err_text.lower()
                or "42210000" in err_text
            )
            if is_invalid_symbol:
                rejections.append({
                    "ticker": ticker,
                    "reason": "invalid symbol (Alpaca rejected; "
                              "likely renamed/delisted)",
                })
                EventsRepo.log(project_id, "Strategist", "SELECTION", {
                    "ticker": ticker,
                    "outcome": "invalid_symbol",
                    "error": err_text[:200],
                    "narrative": [
                        f"Skipping {ticker}: Alpaca rejected the symbol "
                        f"as invalid. Likely renamed (e.g. SQ -> XYZ for "
                        f"Block) or delisted. Remove it from the watchlist "
                        f"to clean up future cycles.",
                    ],
                })
                continue
            logger.exception(
                "strategist failed for ticker %s: %s",
                ticker, _strat_err,
            )
            rejections.append({
                "ticker": ticker,
                "reason": f"unexpected error: {_strat_err}",
            })
            EventsRepo.log(project_id, "Strategist", "ERROR", {
                "ticker": ticker,
                "error": str(_strat_err)[:500],
                "narrative": [
                    f"Strategist hit an unexpected error on {ticker}: "
                    f"{str(_strat_err)[:200]}. "
                    "Skipping this ticker; remaining candidates "
                    "continue to be evaluated.",
                ],
            })
            continue
    EventsRepo.log(project_id, "Strategist", "DECIDE",
                   {"candidates": tickers, "selected": trades, "rejections": rejections})

    return {"selected_trades": trades}


def _strategist_reason(llm: BaseChatModel | None, ticker: str, kind: str,
                       last_price: float, contract: dict[str, Any],
                       delta_lo: float, delta_hi: float,
                       cost_basis: float | None = None) -> dict[str, Any]:
    """Ask Claude for a final approve/reject + short rationale.

    If no LLM is configured, fall back to deterministic approval — the
    contract already passed the configured delta envelope.
    """
    if llm is None:
        return {"approve": True, "rationale": "deterministic (no LLM configured)"}

    system = SystemMessage(content=(
        "You are a conservative options-wheel risk reviewer. The contract has "
        "ALREADY passed deterministic filters for delta band, liquidity, "
        "spread, IV-rank, earnings window, and news sentiment. Your job is "
        "NOT to re-approve — it is to flag ADDITIONAL risks the rule-based "
        "filters can't see, such as: pending corporate actions, regulatory "
        "investigations, sector-wide stress, executive scandals, dividend "
        "ex-dates that risk early assignment, or unusual options activity "
        "that suggests informed counterparties.\n\n"
        "Default to approve=true (the deterministic filters did the heavy "
        "lifting). REJECT only when you have specific, named, current "
        "evidence of risk that those filters don't cover. A vague 'this "
        "looks risky' is not enough — name the catalyst.\n\n"
        "Respond with a single JSON object: "
        "{\"approve\": bool, \"rationale\": str}. The rationale must cite "
        "the SPECIFIC additional risk (or confirm none found)."
    ))
    payload = {
        "ticker": ticker,
        "strategy": kind,
        "underlying_last_price": last_price,
        "cost_basis": cost_basis,
        "option_symbol": contract.get("symbol"),
        "strike": contract.get("strike"),
        "expiration": str(contract.get("expiration")),
        "delta": contract.get("delta"),
        "iv": contract.get("iv"),
        "bid": contract.get("bid"),
        "ask": contract.get("ask"),
        "mid": contract.get("mid"),
        "open_interest": contract.get("open_interest"),
        "delta_window": [delta_lo, delta_hi],
    }
    user = HumanMessage(content=json.dumps(payload))
    # Cache + rate-limit (Cat 8.2/8.3)
    try:
        from llm_ops.cache import get_cached, put_cached
        from llm_ops.rate_limiter import allow
        from llm_ops.tracker import record_usage
        from db.settings_store import AppSettings as _AS
        provider = (_AS.get("llm_provider", "anthropic") or "anthropic").lower()
        model = (_AS.get("google_model") if provider == "google"
                 else _AS.get("anthropic_model")) or "unknown"
        sys_text = system.content
        usr_text = user.content
        cached = get_cached(model, sys_text, usr_text)
        if cached is not None:
            record_usage(project_id=None, purpose="strategist",
                         provider=provider, model=model,
                         prompt_tokens=0, completion_tokens=0,
                         cache_hit=True)
            return cached
        ok, _reason = allow(model)
        if not ok:
            return {"approve": False, "rationale": f"rate-limited: {_reason}"}
    except Exception:
        provider = model = None
        sys_text = usr_text = None

    try:
        resp = llm.invoke([system, user])
        content = resp.content if isinstance(resp.content, str) else "".join(
            getattr(c, "text", "") for c in resp.content
        )
        # Token usage (best-effort — providers expose it on usage_metadata)
        try:
            usage = getattr(resp, "usage_metadata", None) or {}
            in_t = int(usage.get("input_tokens", 0))
            out_t = int(usage.get("output_tokens", 0))
            if model:
                from llm_ops.tracker import record_usage as _r
                _r(project_id=None, purpose="strategist", provider=provider,
                   model=model, prompt_tokens=in_t, completion_tokens=out_t,
                   cache_hit=False)
        except Exception:
            pass
        # Best-effort JSON parse
        start = content.find("{")
        end = content.rfind("}")
        if start != -1 and end != -1:
            parsed = json.loads(content[start:end + 1])
            # Cache the successful parse
            try:
                from llm_ops.cache import put_cached as _pc
                if model and sys_text is not None and usr_text is not None:
                    _pc(model, sys_text, usr_text, parsed)
            except Exception:
                pass
            return parsed
    except Exception as e:
        logger.exception("strategist LLM call failed: %s", e)
        return {"approve": False, "rationale": f"LLM error: {str(e)[:200]}"}
    return {"approve": False, "rationale": "unparseable LLM response"}
