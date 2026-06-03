"""Vertical Spread Strategies.

Bull Put Spread: Sell higher put, buy lower put (bullish, credit)
Bear Call Spread: Sell lower call, buy higher call (bearish, credit)
Bull Call Spread: Buy lower call, sell higher call (bullish, debit)
Bear Put Spread: Buy higher put, sell lower put (bearish, debit)
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import text

from db.connection import insert_returning_id, session_scope
from db.repositories import EventsRepo, ProjectsRepo
from execution import AlpacaClient

logger = logging.getLogger(__name__)


def _ensure_multi_leg_table() -> None:
    """Create multi-leg orders table if it doesn't exist."""
    from strategies.iron_condor import _ensure_multi_leg_table as ensure
    ensure()


class VerticalSpreadStrategy:
    """Base class for vertical spread strategies."""

    strategy_type: str = "VERTICAL_SPREAD"

    def __init__(self, project_id: str):
        self.project_id = project_id
        self.project = ProjectsRepo.get(project_id)
        if self.project is None:
            raise ValueError(f"Project {project_id} not found")
        self.client = AlpacaClient(self.project)

    def find_contracts(
        self,
        ticker: str,
        option_type: str,
        min_dte: int,
        max_dte: int,
        price_range: tuple[float, float]
    ) -> tuple[list[dict], dict]:
        """Get option contracts and quotes for analysis."""
        snap = self.client.snapshots([ticker]).get(ticker)
        if not snap or snap.last_price <= 0:
            return [], {}

        min_strike = snap.last_price * price_range[0]
        max_strike = snap.last_price * price_range[1]

        contracts = self.client.list_option_contracts(
            ticker, option_type, min_dte, max_dte,
            min_strike=min_strike, max_strike=max_strike,
            limit=50
        )

        quotes = self.client.option_chain_quotes(ticker)
        return contracts, quotes

    def _record_spread(
        self,
        ticker: str,
        strategy: str,
        short_leg: dict,
        long_leg: dict,
        net_credit: float,
        max_loss: float,
        quantity: int,
        expiration: date | None
    ) -> int:
        """Record spread in database."""
        _ensure_multi_leg_table()

        with session_scope() as s:
            order_id = insert_returning_id(s, """
                INSERT INTO multi_leg_orders (
                    project_id, strategy_type, underlying, status,
                    leg1_symbol, leg1_side, leg1_qty,
                    leg2_symbol, leg2_side, leg2_qty,
                    net_credit, max_loss, max_profit, expiration
                )
                VALUES (
                    :p, :strat, :und, 'OPEN',
                    :l1s, :l1side, :qty,
                    :l2s, :l2side, :qty,
                    :credit, :loss, :profit, :exp
                )
            """, {
                "p": self.project_id,
                "strat": strategy,
                "und": ticker,
                "l1s": short_leg["symbol"],
                "l1side": "SELL",
                "l2s": long_leg["symbol"],
                "l2side": "BUY",
                "qty": quantity,
                "credit": net_credit,
                "loss": max_loss,
                "profit": abs(net_credit),
                "exp": expiration,
            })
            s.commit()

        return order_id if row else 0


class BullPutSpreadStrategy(VerticalSpreadStrategy):
    """Bull Put Spread: Sell higher strike put, buy lower strike put.

    Bullish strategy, collects premium (credit spread).
    Profit if stock stays above short put strike.
    """

    strategy_type = "BULL_PUT_SPREAD"

    def find_setup(
        self,
        ticker: str,
        target_delta: float = 0.25,
        spread_width: float = 5.0,
        min_dte: int = 21,
        max_dte: int = 45
    ) -> dict[str, Any] | None:
        """Find bull put spread setup."""
        snap = self.client.snapshots([ticker]).get(ticker)
        if not snap:
            return None

        contracts, quotes = self.find_contracts(
            ticker, "put", min_dte, max_dte, (0.85, 0.98)
        )

        if not contracts:
            return None

        # Find short put at target delta
        short_put = None
        best_diff = float("inf")

        for c in contracts:
            q = quotes.get(c["symbol"], {})
            delta = q.get("delta")
            if delta is None:
                continue

            diff = abs(abs(delta) - target_delta)
            if diff < best_diff:
                bid = q.get("bid", 0) or 0
                ask = q.get("ask", 0) or 0
                if bid > 0:
                    best_diff = diff
                    short_put = {**c, **q, "mid": (bid + ask) / 2}

        if not short_put:
            return None

        # Find long put (lower strike)
        target_long_strike = short_put["strike"] - spread_width
        long_put = None
        best_diff = float("inf")

        for c in contracts:
            diff = abs(c["strike"] - target_long_strike)
            if diff < best_diff and c["strike"] < short_put["strike"]:
                q = quotes.get(c["symbol"], {})
                bid = q.get("bid", 0) or 0
                ask = q.get("ask", 0) or 0
                best_diff = diff
                long_put = {**c, **q, "mid": (bid + ask) / 2 if ask > 0 else 0}

        if not long_put:
            return None

        net_credit = (short_put["mid"] - long_put["mid"]) * 100
        width = short_put["strike"] - long_put["strike"]
        max_loss = (width * 100) - net_credit

        return {
            "ticker": ticker,
            "strategy": "BULL_PUT_SPREAD",
            "underlying_price": snap.last_price,
            "expiration": short_put.get("expiration"),
            "short_leg": {
                "symbol": short_put["symbol"],
                "strike": short_put["strike"],
                "delta": short_put.get("delta"),
                "mid": short_put["mid"],
            },
            "long_leg": {
                "symbol": long_put["symbol"],
                "strike": long_put["strike"],
                "delta": long_put.get("delta"),
                "mid": long_put["mid"],
            },
            "width": width,
            "net_credit": round(net_credit, 2),
            "max_loss": round(max_loss, 2),
            "max_profit": round(net_credit, 2),
            "breakeven": round(short_put["strike"] - (net_credit / 100), 2),
        }

    def execute(self, setup: dict, quantity: int = 1) -> dict[str, Any]:
        """Execute bull put spread."""
        try:
            # Sell short put
            o1 = self.client.submit_limit_option(
                setup["short_leg"]["symbol"], quantity, "sell",
                setup["short_leg"]["mid"], time_in_force="day"
            )

            # Buy long put
            o2 = self.client.submit_limit_option(
                setup["long_leg"]["symbol"], quantity, "buy",
                setup["long_leg"]["mid"], time_in_force="day"
            )

            order_id = self._record_spread(
                setup["ticker"], self.strategy_type,
                setup["short_leg"], setup["long_leg"],
                setup["net_credit"], setup["max_loss"],
                quantity, setup.get("expiration")
            )

            EventsRepo.log(self.project_id, "BullPutSpread", "EXECUTE", {
                "order_id": order_id,
                "ticker": setup["ticker"],
                "net_credit": setup["net_credit"],
            })

            return {"success": True, "order_id": order_id, "orders": [o1, o2]}

        except Exception as e:
            return {"success": False, "error": str(e)}


class BearCallSpreadStrategy(VerticalSpreadStrategy):
    """Bear Call Spread: Sell lower strike call, buy higher strike call.

    Bearish strategy, collects premium (credit spread).
    Profit if stock stays below short call strike.
    """

    strategy_type = "BEAR_CALL_SPREAD"

    def find_setup(
        self,
        ticker: str,
        target_delta: float = 0.25,
        spread_width: float = 5.0,
        min_dte: int = 21,
        max_dte: int = 45
    ) -> dict[str, Any] | None:
        """Find bear call spread setup."""
        snap = self.client.snapshots([ticker]).get(ticker)
        if not snap:
            return None

        contracts, quotes = self.find_contracts(
            ticker, "call", min_dte, max_dte, (1.02, 1.15)
        )

        if not contracts:
            return None

        # Find short call at target delta
        short_call = None
        best_diff = float("inf")

        for c in contracts:
            q = quotes.get(c["symbol"], {})
            delta = q.get("delta")
            if delta is None:
                continue

            diff = abs(abs(delta) - target_delta)
            if diff < best_diff:
                bid = q.get("bid", 0) or 0
                ask = q.get("ask", 0) or 0
                if bid > 0:
                    best_diff = diff
                    short_call = {**c, **q, "mid": (bid + ask) / 2}

        if not short_call:
            return None

        # Find long call (higher strike)
        target_long_strike = short_call["strike"] + spread_width
        long_call = None
        best_diff = float("inf")

        for c in contracts:
            diff = abs(c["strike"] - target_long_strike)
            if diff < best_diff and c["strike"] > short_call["strike"]:
                q = quotes.get(c["symbol"], {})
                bid = q.get("bid", 0) or 0
                ask = q.get("ask", 0) or 0
                best_diff = diff
                long_call = {**c, **q, "mid": (bid + ask) / 2 if ask > 0 else 0}

        if not long_call:
            return None

        net_credit = (short_call["mid"] - long_call["mid"]) * 100
        width = long_call["strike"] - short_call["strike"]
        max_loss = (width * 100) - net_credit

        return {
            "ticker": ticker,
            "strategy": "BEAR_CALL_SPREAD",
            "underlying_price": snap.last_price,
            "expiration": short_call.get("expiration"),
            "short_leg": {
                "symbol": short_call["symbol"],
                "strike": short_call["strike"],
                "delta": short_call.get("delta"),
                "mid": short_call["mid"],
            },
            "long_leg": {
                "symbol": long_call["symbol"],
                "strike": long_call["strike"],
                "delta": long_call.get("delta"),
                "mid": long_call["mid"],
            },
            "width": width,
            "net_credit": round(net_credit, 2),
            "max_loss": round(max_loss, 2),
            "max_profit": round(net_credit, 2),
            "breakeven": round(short_call["strike"] + (net_credit / 100), 2),
        }

    def execute(self, setup: dict, quantity: int = 1) -> dict[str, Any]:
        """Execute bear call spread."""
        try:
            o1 = self.client.submit_limit_option(
                setup["short_leg"]["symbol"], quantity, "sell",
                setup["short_leg"]["mid"], time_in_force="day"
            )

            o2 = self.client.submit_limit_option(
                setup["long_leg"]["symbol"], quantity, "buy",
                setup["long_leg"]["mid"], time_in_force="day"
            )

            order_id = self._record_spread(
                setup["ticker"], self.strategy_type,
                setup["short_leg"], setup["long_leg"],
                setup["net_credit"], setup["max_loss"],
                quantity, setup.get("expiration")
            )

            EventsRepo.log(self.project_id, "BearCallSpread", "EXECUTE", {
                "order_id": order_id,
                "ticker": setup["ticker"],
                "net_credit": setup["net_credit"],
            })

            return {"success": True, "order_id": order_id, "orders": [o1, o2]}

        except Exception as e:
            return {"success": False, "error": str(e)}


class BullCallSpreadStrategy(VerticalSpreadStrategy):
    """Bull Call Spread: Buy lower strike call, sell higher strike call.

    Bullish strategy (debit spread).
    """

    strategy_type = "BULL_CALL_SPREAD"

    def find_setup(
        self,
        ticker: str,
        target_delta: float = 0.50,
        spread_width: float = 5.0,
        min_dte: int = 21,
        max_dte: int = 45
    ) -> dict[str, Any] | None:
        """Find bull call spread setup."""
        snap = self.client.snapshots([ticker]).get(ticker)
        if not snap:
            return None

        contracts, quotes = self.find_contracts(
            ticker, "call", min_dte, max_dte, (0.95, 1.10)
        )

        if not contracts:
            return None

        # Find long call at target delta
        long_call = None
        best_diff = float("inf")

        for c in contracts:
            q = quotes.get(c["symbol"], {})
            delta = q.get("delta")
            if delta is None:
                continue

            diff = abs(abs(delta) - target_delta)
            if diff < best_diff:
                bid = q.get("bid", 0) or 0
                ask = q.get("ask", 0) or 0
                if ask > 0:
                    best_diff = diff
                    long_call = {**c, **q, "mid": (bid + ask) / 2}

        if not long_call:
            return None

        # Find short call (higher strike)
        target_short_strike = long_call["strike"] + spread_width
        short_call = None
        best_diff = float("inf")

        for c in contracts:
            diff = abs(c["strike"] - target_short_strike)
            if diff < best_diff and c["strike"] > long_call["strike"]:
                q = quotes.get(c["symbol"], {})
                bid = q.get("bid", 0) or 0
                ask = q.get("ask", 0) or 0
                if bid >= 0:
                    best_diff = diff
                    short_call = {**c, **q, "mid": (bid + ask) / 2 if ask > 0 else 0}

        if not short_call:
            return None

        net_debit = (long_call["mid"] - short_call["mid"]) * 100
        width = short_call["strike"] - long_call["strike"]
        max_profit = (width * 100) - net_debit

        return {
            "ticker": ticker,
            "strategy": "BULL_CALL_SPREAD",
            "underlying_price": snap.last_price,
            "expiration": long_call.get("expiration"),
            "long_leg": {
                "symbol": long_call["symbol"],
                "strike": long_call["strike"],
                "delta": long_call.get("delta"),
                "mid": long_call["mid"],
            },
            "short_leg": {
                "symbol": short_call["symbol"],
                "strike": short_call["strike"],
                "delta": short_call.get("delta"),
                "mid": short_call["mid"],
            },
            "width": width,
            "net_debit": round(net_debit, 2),
            "max_loss": round(net_debit, 2),
            "max_profit": round(max_profit, 2),
            "breakeven": round(long_call["strike"] + (net_debit / 100), 2),
        }

    def execute(self, setup: dict, quantity: int = 1) -> dict[str, Any]:
        """Execute bull call spread."""
        try:
            o1 = self.client.submit_limit_option(
                setup["long_leg"]["symbol"], quantity, "buy",
                setup["long_leg"]["mid"], time_in_force="day"
            )

            o2 = self.client.submit_limit_option(
                setup["short_leg"]["symbol"], quantity, "sell",
                setup["short_leg"]["mid"], time_in_force="day"
            )

            # Record as negative credit (debit)
            order_id = self._record_spread(
                setup["ticker"], self.strategy_type,
                setup["short_leg"], setup["long_leg"],
                -setup["net_debit"], setup["max_loss"],
                quantity, setup.get("expiration")
            )

            return {"success": True, "order_id": order_id, "orders": [o1, o2]}

        except Exception as e:
            return {"success": False, "error": str(e)}


class BearPutSpreadStrategy(VerticalSpreadStrategy):
    """Bear Put Spread: Buy higher strike put, sell lower strike put.

    Bearish strategy (debit spread).
    """

    strategy_type = "BEAR_PUT_SPREAD"

    def find_setup(
        self,
        ticker: str,
        target_delta: float = 0.50,
        spread_width: float = 5.0,
        min_dte: int = 21,
        max_dte: int = 45
    ) -> dict[str, Any] | None:
        """Find bear put spread setup."""
        snap = self.client.snapshots([ticker]).get(ticker)
        if not snap:
            return None

        contracts, quotes = self.find_contracts(
            ticker, "put", min_dte, max_dte, (0.90, 1.05)
        )

        if not contracts:
            return None

        # Find long put at target delta
        long_put = None
        best_diff = float("inf")

        for c in contracts:
            q = quotes.get(c["symbol"], {})
            delta = q.get("delta")
            if delta is None:
                continue

            diff = abs(abs(delta) - target_delta)
            if diff < best_diff:
                bid = q.get("bid", 0) or 0
                ask = q.get("ask", 0) or 0
                if ask > 0:
                    best_diff = diff
                    long_put = {**c, **q, "mid": (bid + ask) / 2}

        if not long_put:
            return None

        # Find short put (lower strike)
        target_short_strike = long_put["strike"] - spread_width
        short_put = None
        best_diff = float("inf")

        for c in contracts:
            diff = abs(c["strike"] - target_short_strike)
            if diff < best_diff and c["strike"] < long_put["strike"]:
                q = quotes.get(c["symbol"], {})
                bid = q.get("bid", 0) or 0
                ask = q.get("ask", 0) or 0
                if bid >= 0:
                    best_diff = diff
                    short_put = {**c, **q, "mid": (bid + ask) / 2 if ask > 0 else 0}

        if not short_put:
            return None

        net_debit = (long_put["mid"] - short_put["mid"]) * 100
        width = long_put["strike"] - short_put["strike"]
        max_profit = (width * 100) - net_debit

        return {
            "ticker": ticker,
            "strategy": "BEAR_PUT_SPREAD",
            "underlying_price": snap.last_price,
            "expiration": long_put.get("expiration"),
            "long_leg": {
                "symbol": long_put["symbol"],
                "strike": long_put["strike"],
                "delta": long_put.get("delta"),
                "mid": long_put["mid"],
            },
            "short_leg": {
                "symbol": short_put["symbol"],
                "strike": short_put["strike"],
                "delta": short_put.get("delta"),
                "mid": short_put["mid"],
            },
            "width": width,
            "net_debit": round(net_debit, 2),
            "max_loss": round(net_debit, 2),
            "max_profit": round(max_profit, 2),
            "breakeven": round(long_put["strike"] - (net_debit / 100), 2),
        }

    def execute(self, setup: dict, quantity: int = 1) -> dict[str, Any]:
        """Execute bear put spread."""
        try:
            o1 = self.client.submit_limit_option(
                setup["long_leg"]["symbol"], quantity, "buy",
                setup["long_leg"]["mid"], time_in_force="day"
            )

            o2 = self.client.submit_limit_option(
                setup["short_leg"]["symbol"], quantity, "sell",
                setup["short_leg"]["mid"], time_in_force="day"
            )

            order_id = self._record_spread(
                setup["ticker"], self.strategy_type,
                setup["short_leg"], setup["long_leg"],
                -setup["net_debit"], setup["max_loss"],
                quantity, setup.get("expiration")
            )

            return {"success": True, "order_id": order_id, "orders": [o1, o2]}

        except Exception as e:
            return {"success": False, "error": str(e)}


def list_vertical_spreads(
    project_id: str,
    strategy: str | None = None,
    status: str | None = None
) -> list[dict[str, Any]]:
    """List vertical spread positions."""
    _ensure_multi_leg_table()

    sql = """
        SELECT order_id, strategy_type, underlying, status,
               leg1_symbol, leg1_side, leg2_symbol, leg2_side,
               net_credit, max_loss, max_profit, expiration, opened_at
        FROM multi_leg_orders
        WHERE project_id = :p
    """
    params: dict[str, Any] = {"p": project_id}

    strategies = ["BULL_PUT_SPREAD", "BEAR_CALL_SPREAD", "BULL_CALL_SPREAD", "BEAR_PUT_SPREAD"]
    if strategy:
        sql += " AND strategy_type = :strat"
        params["strat"] = strategy
    else:
        sql += f" AND strategy_type IN ({','.join(repr(s) for s in strategies)})"

    if status:
        sql += " AND status = :s"
        params["s"] = status

    sql += " ORDER BY opened_at DESC"

    with session_scope() as s:
        rows = s.execute(text(sql), params).fetchall()

    return [
        {
            "order_id": r[0],
            "strategy_type": r[1],
            "underlying": r[2],
            "status": r[3],
            "short_leg": r[4],
            "long_leg": r[6],
            "net_credit": float(r[8]) if r[8] else None,
            "max_loss": float(r[9]) if r[9] else None,
            "max_profit": float(r[10]) if r[10] else None,
            "expiration": r[11].isoformat() if r[11] else None,
            "opened_at": r[12].isoformat() if r[12] else None,
        }
        for r in rows
    ]