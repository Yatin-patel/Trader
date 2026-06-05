"""Iron Condor Strategy.

A neutral options strategy that profits from low volatility.
Sells an OTM put spread and an OTM call spread simultaneously.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import text

from db.connection import insert_returning_id, session_scope
from db.repositories import EventsRepo, ProjectsRepo
from db.settings_store import ProjectSettings  # noqa: F401  (legacy import)
from execution import get_broker

logger = logging.getLogger(__name__)

# NB: the multi_leg_orders table is owned by db/schema_mysql.sql — no
# need to create it lazily here. The old _ensure_multi_leg_table() used
# T-SQL syntax (IF NOT EXISTS … BEGIN/END, IDENTITY) that doesn't run on
# the MySQL production database anyway, so leaving it would break the
# first call.


class IronCondorStrategy:
    """Iron Condor: Sell OTM put spread + Sell OTM call spread.

    Structure:
    - Buy lower strike put (wing protection)
    - Sell higher strike put (collect premium)
    - Sell lower strike call (collect premium)
    - Buy higher strike call (wing protection)
    """

    def __init__(self, project_id: str):
        self.project_id = project_id
        self.project = ProjectsRepo.get(project_id)
        if self.project is None:
            raise ValueError(f"Project {project_id} not found")
        self.client = get_broker(self.project)

    def find_setup(
        self,
        ticker: str,
        target_delta: float = 0.20,
        wing_width: float = 5.0,
        min_dte: int = 21,
        max_dte: int = 45
    ) -> dict[str, Any] | None:
        """Find an iron condor setup.

        Args:
            ticker: Underlying symbol
            target_delta: Target delta for short strikes
            wing_width: Dollar width of each spread
            min_dte: Minimum days to expiration
            max_dte: Maximum days to expiration

        Returns:
            Iron condor setup or None if not viable
        """
        snap = self.client.snapshots([ticker]).get(ticker)
        if not snap or snap.last_price <= 0:
            return None

        underlying_price = snap.last_price

        # Get put contracts
        puts = self.client.list_option_contracts(
            ticker, "put", min_dte, max_dte,
            min_strike=underlying_price * 0.85,
            max_strike=underlying_price * 0.98,
            limit=50
        )

        # Get call contracts
        calls = self.client.list_option_contracts(
            ticker, "call", min_dte, max_dte,
            min_strike=underlying_price * 1.02,
            max_strike=underlying_price * 1.15,
            limit=50
        )

        if not puts or not calls:
            return None

        quotes = self.client.option_chain_quotes(ticker)

        # Find short put (target delta)
        short_put = self._find_delta_strike(puts, quotes, target_delta, "put")
        if not short_put:
            return None

        # Find long put (wing)
        long_put = self._find_wing_strike(puts, quotes, short_put["strike"], -wing_width)

        # Find short call (target delta)
        short_call = self._find_delta_strike(calls, quotes, target_delta, "call")
        if not short_call:
            return None

        # Find long call (wing)
        long_call = self._find_wing_strike(calls, quotes, short_call["strike"], wing_width)

        if not long_put or not long_call:
            return None

        # Calculate credits and max loss
        short_put_mid = short_put.get("mid", 0)
        long_put_mid = long_put.get("mid", 0)
        short_call_mid = short_call.get("mid", 0)
        long_call_mid = long_call.get("mid", 0)

        put_spread_credit = short_put_mid - long_put_mid
        call_spread_credit = short_call_mid - long_call_mid
        net_credit = put_spread_credit + call_spread_credit

        # Max loss is width minus credit (per spread, whichever is wider)
        put_width = abs(short_put["strike"] - long_put["strike"])
        call_width = abs(long_call["strike"] - short_call["strike"])
        max_width = max(put_width, call_width)
        max_loss = (max_width - net_credit) * 100  # Per contract

        return {
            "ticker": ticker,
            "underlying_price": underlying_price,
            "expiration": short_put.get("expiration"),
            "legs": {
                "long_put": {
                    "symbol": long_put["symbol"],
                    "strike": long_put["strike"],
                    "delta": long_put.get("delta"),
                    "mid": long_put_mid,
                },
                "short_put": {
                    "symbol": short_put["symbol"],
                    "strike": short_put["strike"],
                    "delta": short_put.get("delta"),
                    "mid": short_put_mid,
                },
                "short_call": {
                    "symbol": short_call["symbol"],
                    "strike": short_call["strike"],
                    "delta": short_call.get("delta"),
                    "mid": short_call_mid,
                },
                "long_call": {
                    "symbol": long_call["symbol"],
                    "strike": long_call["strike"],
                    "delta": long_call.get("delta"),
                    "mid": long_call_mid,
                },
            },
            "put_spread_credit": round(put_spread_credit * 100, 2),
            "call_spread_credit": round(call_spread_credit * 100, 2),
            "net_credit": round(net_credit * 100, 2),
            "max_loss": round(max_loss, 2),
            "max_profit": round(net_credit * 100, 2),
            "risk_reward_ratio": round(max_loss / (net_credit * 100), 2) if net_credit > 0 else None,
        }

    def _find_delta_strike(
        self,
        contracts: list[dict],
        quotes: dict,
        target_delta: float,
        option_type: str
    ) -> dict[str, Any] | None:
        """Find contract closest to target delta."""
        best = None
        best_diff = float("inf")

        for c in contracts:
            q = quotes.get(c["symbol"], {})
            delta = q.get("delta")
            if delta is None:
                continue

            delta_abs = abs(delta)
            diff = abs(delta_abs - target_delta)

            if diff < best_diff:
                bid = q.get("bid", 0) or 0
                ask = q.get("ask", 0) or 0
                if bid > 0 and ask > 0:
                    best_diff = diff
                    best = {**c, **q, "mid": (bid + ask) / 2}

        return best

    def _find_wing_strike(
        self,
        contracts: list[dict],
        quotes: dict,
        short_strike: float,
        offset: float
    ) -> dict[str, Any] | None:
        """Find wing strike at specified offset from short strike."""
        target = short_strike + offset

        best = None
        best_diff = float("inf")

        for c in contracts:
            diff = abs(c["strike"] - target)
            if diff < best_diff:
                q = quotes.get(c["symbol"], {})
                bid = q.get("bid", 0) or 0
                ask = q.get("ask", 0) or 0
                if bid >= 0:
                    best_diff = diff
                    best = {**c, **q, "mid": (bid + ask) / 2 if ask > 0 else bid}

        return best

    def execute(
        self,
        setup: dict[str, Any],
        quantity: int = 1
    ) -> dict[str, Any]:
        """Execute iron condor trade.

        Args:
            setup: Setup from find_setup()
            quantity: Number of iron condors to open

        Returns:
            Execution result
        """
        legs = setup["legs"]
        orders: list[Any] = []
        atomic = None

        try:
            # Prefer atomic 4-leg submission when the broker supports it
            # (Alpaca v2 mleg, ETrade IRON_CONDOR). Falls back to
            # individual legs otherwise — same as before.
            if getattr(self.client, "supports_multi_leg", lambda: False)():
                # net_credit on the setup is per-contract DOLLARS; mleg
                # net_limit_price is per-share, negative for credits.
                net_credit_per_share = (
                    float(setup["net_credit"]) / 100.0
                )
                mleg_legs = [
                    {"symbol": legs["long_put"]["symbol"], "side": "buy",
                     "ratio_qty": 1,
                     "position_intent": "buying_to_open"},
                    {"symbol": legs["short_put"]["symbol"], "side": "sell",
                     "ratio_qty": 1,
                     "position_intent": "selling_to_open"},
                    {"symbol": legs["short_call"]["symbol"], "side": "sell",
                     "ratio_qty": 1,
                     "position_intent": "selling_to_open"},
                    {"symbol": legs["long_call"]["symbol"], "side": "buy",
                     "ratio_qty": 1,
                     "position_intent": "buying_to_open"},
                ]
                atomic = self.client.submit_multi_leg_option(
                    legs=mleg_legs, qty=quantity,
                    net_limit_price=-net_credit_per_share,
                    time_in_force="day",
                )
                orders.append({"leg": "iron_condor_atomic",
                               "order": atomic})
            else:
                # Legacy leg-by-leg submission.
                o1 = self.client.submit_limit_option(
                    legs["long_put"]["symbol"], quantity, "buy",
                    legs["long_put"]["mid"], time_in_force="day"
                )
                orders.append({"leg": "long_put", "order": o1})
                o2 = self.client.submit_limit_option(
                    legs["short_put"]["symbol"], quantity, "sell",
                    legs["short_put"]["mid"], time_in_force="day"
                )
                orders.append({"leg": "short_put", "order": o2})
                o3 = self.client.submit_limit_option(
                    legs["short_call"]["symbol"], quantity, "sell",
                    legs["short_call"]["mid"], time_in_force="day"
                )
                orders.append({"leg": "short_call", "order": o3})
                o4 = self.client.submit_limit_option(
                    legs["long_call"]["symbol"], quantity, "buy",
                    legs["long_call"]["mid"], time_in_force="day"
                )
                orders.append({"leg": "long_call", "order": o4})

            # Record multi-leg order
            with session_scope() as s:
                order_id = insert_returning_id(s, """
                    INSERT INTO multi_leg_orders (
                        project_id, strategy_type, underlying, status,
                        leg1_symbol, leg1_side, leg1_qty,
                        leg2_symbol, leg2_side, leg2_qty,
                        leg3_symbol, leg3_side, leg3_qty,
                        leg4_symbol, leg4_side, leg4_qty,
                        net_credit, max_loss, max_profit, expiration
                    )
                    VALUES (
                        :p, 'IRON_CONDOR', :und, 'OPEN',
                        :l1s, 'BUY', :qty, :l2s, 'SELL', :qty,
                        :l3s, 'SELL', :qty, :l4s, 'BUY', :qty,
                        :credit, :loss, :profit, :exp
                    )
                """, {
                    "p": self.project_id,
                    "und": setup["ticker"],
                    "l1s": legs["long_put"]["symbol"],
                    "l2s": legs["short_put"]["symbol"],
                    "l3s": legs["short_call"]["symbol"],
                    "l4s": legs["long_call"]["symbol"],
                    "qty": quantity,
                    "credit": setup["net_credit"],
                    "loss": setup["max_loss"],
                    "profit": setup["max_profit"],
                    "exp": setup["expiration"],
                })
                s.commit()

            EventsRepo.log(self.project_id, "IronCondor", "EXECUTE", {
                "atomic": atomic is not None,
                "order_id": order_id,
                "ticker": setup["ticker"],
                "net_credit": setup["net_credit"],
                "max_loss": setup["max_loss"],
                "legs": orders,
            })

            return {
                "success": True,
                "order_id": order_id,
                "orders": orders,
                "net_credit": setup["net_credit"],
            }

        except Exception as e:
            logger.exception("Iron condor execution failed: %s", e)
            return {"success": False, "error": str(e), "partial_orders": orders}


def list_iron_condors(
    project_id: str,
    status: str | None = None
) -> list[dict[str, Any]]:
    """List iron condor positions."""
    sql = """
        SELECT order_id, underlying, status, leg1_symbol, leg2_symbol,
               leg3_symbol, leg4_symbol, net_credit, max_loss, max_profit,
               expiration, opened_at, closed_at, realized_pnl
        FROM multi_leg_orders
        WHERE project_id = :p AND strategy_type = 'IRON_CONDOR'
    """
    params: dict[str, Any] = {"p": project_id}

    if status:
        sql += " AND status = :s"
        params["s"] = status

    sql += " ORDER BY opened_at DESC"

    with session_scope() as s:
        rows = s.execute(text(sql), params).fetchall()

    return [
        {
            "order_id": r[0],
            "underlying": r[1],
            "status": r[2],
            "legs": [r[3], r[4], r[5], r[6]],
            "net_credit": float(r[7]) if r[7] else None,
            "max_loss": float(r[8]) if r[8] else None,
            "max_profit": float(r[9]) if r[9] else None,
            "expiration": r[10].isoformat() if r[10] else None,
            "opened_at": r[11].isoformat() if r[11] else None,
            "closed_at": r[12].isoformat() if r[12] else None,
            "realized_pnl": float(r[13]) if r[13] else None,
        }
        for r in rows
    ]