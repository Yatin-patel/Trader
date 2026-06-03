"""Read-only LangChain tools the chat assistant can call.

All tools close over a single `project_id` so the LLM never has to pass one.
Every tool returns a short string (the LLM's working memory is precious).
"""
from __future__ import annotations

import json
from typing import Any

from langchain_core.tools import StructuredTool, Tool
from pydantic import BaseModel, Field

from db.repositories import EventsRepo, PositionsRepo, ProjectsRepo, WheelRepo
from db.settings_store import ProjectSettings
from execution import AlpacaClient


def _fmt_money(v: Any) -> str:
    try:
        return f"${float(v):,.2f}"
    except Exception:
        return str(v)


def _safe(call):
    """Wrap a tool function so exceptions become readable strings."""
    def wrapped(*args, **kwargs):
        try:
            return call(*args, **kwargs)
        except Exception as e:
            return f"error: {e}"
    return wrapped


def build_tools(project_id: str | None) -> list[Tool]:
    """Return the tools the LLM can call. Falls back gracefully if no project."""
    tools: list[Tool] = []

    project = ProjectsRepo.get(project_id) if project_id else None
    # Chat tools need market-data + account APIs. Only Alpaca is wired today.
    # For ETrade projects we surface a friendly note instead of breaking.
    client = None
    if project and (getattr(project, "broker_type", "alpaca") or "alpaca") == "alpaca":
        try:
            client = AlpacaClient(project)
        except Exception:
            client = None

    # ---------- Market data ----------
    @_safe
    def get_stock_snapshot(ticker: str) -> str:
        if client is None:
            return "no project context — cannot fetch market data"
        snaps = client.snapshots([ticker.upper()])
        s = snaps.get(ticker.upper())
        if not s:
            return f"no snapshot for {ticker}"
        return (f"{s.symbol}: last={_fmt_money(s.last_price)}, "
                f"prev_close={_fmt_money(s.prev_close)}, "
                f"volume={s.volume:,}, %change={s.pct_change:+.2f}%")

    @_safe
    def get_historical_bars(ticker: str, days: int = 10) -> str:
        if client is None:
            return "no project context"
        days = max(1, min(int(days), 30))
        bars = client.daily_bars(ticker.upper(), lookback_days=days)
        if not bars:
            return f"no bars for {ticker}"
        lines = [f"{b['t'].date() if hasattr(b['t'], 'date') else b['t']}: "
                 f"O={b['o']} H={b['h']} L={b['l']} C={b['c']} V={b['v']:,}"
                 for b in bars]
        return f"{ticker.upper()} last {len(bars)} daily bars:\n" + "\n".join(lines)

    @_safe
    def get_option_chain(ticker: str, option_type: str = "put",
                         max_dte: int = 14, max_results: int = 12) -> str:
        if client is None:
            return "no project context"
        contracts = client.list_option_contracts(
            ticker.upper(),
            option_type.lower(),
            min_dte=0,
            max_dte=int(max_dte),
            limit=200,
        )
        if not contracts:
            return f"no {option_type}s within {max_dte} DTE for {ticker}"
        quotes = client.option_chain_quotes(ticker.upper())
        rows = []
        for c in contracts[:int(max_results)]:
            q = quotes.get(c["symbol"]) or {}
            delta = q.get("delta")
            bid = q.get("bid") or 0
            ask = q.get("ask") or 0
            mid = (bid + ask) / 2 if bid and ask else None
            rows.append({
                "symbol": c["symbol"],
                "strike": c["strike"],
                "exp": str(c["expiration"]),
                "delta": round(delta, 3) if delta is not None else None,
                "bid": bid, "ask": ask, "mid": mid,
                "oi": c.get("open_interest"),
            })
        return json.dumps(rows, default=str)

    # ---------- Account & clock ----------
    @_safe
    def get_account_state() -> str:
        if client is None:
            return "no project context"
        a = client.get_account()
        return (f"cash={_fmt_money(a['cash'])}, "
                f"buying_power={_fmt_money(a['buying_power'])}, "
                f"equity={_fmt_money(a['equity'])}, "
                f"portfolio_value={_fmt_money(a['portfolio_value'])}")

    @_safe
    def get_market_clock() -> str:
        if client is None:
            return "no project context"
        c = client.get_market_clock()
        return (f"is_open={c.get('is_open')}, "
                f"next_open={c.get('next_open')}, "
                f"next_close={c.get('next_close')}, "
                f"alpaca_time={c.get('timestamp')}")

    @_safe
    def get_open_positions() -> str:
        if client is None:
            return "no project context"
        ps = client.list_positions()
        if not ps:
            return "no open positions"
        rows = [{"symbol": p["symbol"], "qty": p["qty"],
                 "avg_entry": p["avg_entry_price"],
                 "current": p["current_price"],
                 "unrealized_pl": p["unrealized_pl"],
                 "asset_class": p["asset_class"]} for p in ps]
        return json.dumps(rows, default=str)

    @_safe
    def get_open_contracts() -> str:
        if not project_id:
            return "no project context"
        cs = WheelRepo.list_open(project_id)
        if not cs:
            return "no open wheel contracts in this project"
        return json.dumps(cs, default=str)

    @_safe
    def get_recent_events(limit: int = 20) -> str:
        if not project_id:
            return "no project context"
        events = EventsRepo.recent(project_id, limit=int(limit))
        summary = []
        for e in events:
            payload = e.get("payload") or {}
            short = str(payload)[:200] if payload else ""
            summary.append(
                f"[{e['created_at']}] {e['node_name']}.{e['event_type']}: {short}"
            )
        return "\n".join(summary)

    @_safe
    def get_strategy_settings() -> str:
        if not project_id:
            return "no project context"
        rows = ProjectSettings.list_for_project(project_id)
        return json.dumps({r.key: r.value for r in rows}, default=str)

    @_safe
    def get_db_positions() -> str:
        if not project_id:
            return "no project context"
        ps = PositionsRepo.list_open(project_id)
        return json.dumps(ps, default=str)

    # Structured schemas for multi-arg tools so Gemini gets a real JSON Schema.
    class _SnapshotArgs(BaseModel):
        ticker: str = Field(..., description="Stock ticker symbol, e.g. NVDA")

    class _BarsArgs(BaseModel):
        ticker: str = Field(..., description="Stock ticker symbol")
        days: int = Field(10, description="How many trading days back, 1-30")

    class _ChainArgs(BaseModel):
        ticker: str = Field(..., description="Underlying stock ticker")
        option_type: str = Field("put", description="'put' or 'call'")
        max_dte: int = Field(14, description="Maximum days-to-expiration")
        max_results: int = Field(12, description="Maximum contracts to return")

    class _EventsArgs(BaseModel):
        limit: int = Field(20, description="Number of recent events to return")

    tools.extend([
        StructuredTool.from_function(
            func=get_stock_snapshot,
            name="get_stock_snapshot",
            description="Current price, previous close, volume, and percent change for a ticker.",
            args_schema=_SnapshotArgs,
        ),
        StructuredTool.from_function(
            func=get_historical_bars,
            name="get_historical_bars",
            description="Up to 30 days of daily OHLC bars for a ticker.",
            args_schema=_BarsArgs,
        ),
        StructuredTool.from_function(
            func=get_option_chain,
            name="get_option_chain",
            description="List option contracts (puts or calls) with greeks and quotes within a DTE window.",
            args_schema=_ChainArgs,
        ),
        Tool.from_function(
            func=get_account_state,
            name="get_account_state",
            description="Get the live Alpaca account: cash, buying_power, equity, portfolio_value.",
        ),
        Tool.from_function(
            func=get_market_clock,
            name="get_market_clock",
            description="Get current market clock — is_open, next_open, next_close.",
        ),
        Tool.from_function(
            func=get_open_positions,
            name="get_open_positions",
            description="Get all live Alpaca positions (equities and options).",
        ),
        Tool.from_function(
            func=get_open_contracts,
            name="get_open_contracts",
            description="Get open wheel-strategy option contracts tracked in this project's database.",
        ),
        Tool.from_function(
            func=get_db_positions,
            name="get_db_positions",
            description="Get stock positions the trader has opened (tracked in DB, includes stop levels).",
        ),
        StructuredTool.from_function(
            func=get_recent_events,
            name="get_recent_events",
            description="The N most recent agent_events for this project.",
            args_schema=_EventsArgs,
        ),
        Tool.from_function(
            func=get_strategy_settings,
            name="get_strategy_settings",
            description="Get the current strategy/risk settings for this project (delta band, DTE, stop loss, etc).",
        ),
    ])
    return tools
