"""Calendar Spread Strategy (Horizontal Spread).

Sells near-term option and buys longer-term option at same strike.
Profits from time decay differential and potential volatility expansion.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text

from db.connection import insert_returning_id, session_scope
from db.repositories import EventsRepo, ProjectsRepo
from execution import get_broker

logger = logging.getLogger(__name__)


class CalendarSpreadStrategy:
    """Calendar Spread: Same strike, different expirations.

    - Sell short-term option (faster decay)
    - Buy long-term option (protection + potential)

    Can be implemented with puts or calls.
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
        option_type: str = "call",
        short_dte: int = 14,
        long_dte: int = 45,
        target_strike: float | None = None
    ) -> dict[str, Any] | None:
        """Find calendar spread setup.

        Args:
            ticker: Underlying symbol
            option_type: 'call' or 'put'
            short_dte: Target DTE for short leg
            long_dte: Target DTE for long leg
            target_strike: Specific strike (ATM if None)

        Returns:
            Calendar spread setup or None
        """
        snap = self.client.snapshots([ticker]).get(ticker)
        if not snap or snap.last_price <= 0:
            return None

        underlying_price = snap.last_price

        if target_strike is None:
            target_strike = underlying_price

        # Get short-term contracts
        short_contracts = self.client.list_option_contracts(
            ticker, option_type,
            min_dte=max(1, short_dte - 7),
            max_dte=short_dte + 7,
            min_strike=target_strike * 0.95,
            max_strike=target_strike * 1.05,
            limit=20
        )

        # Get long-term contracts
        long_contracts = self.client.list_option_contracts(
            ticker, option_type,
            min_dte=long_dte - 14,
            max_dte=long_dte + 14,
            min_strike=target_strike * 0.95,
            max_strike=target_strike * 1.05,
            limit=20
        )

        if not short_contracts or not long_contracts:
            return None

        quotes = self.client.option_chain_quotes(ticker)

        # Find best short contract (closest to target strike, shortest DTE)
        short_contract = self._find_best_contract(
            short_contracts, quotes, target_strike, prefer_short=True
        )

        if not short_contract:
            return None

        # Find matching long contract (same strike, longer DTE)
        long_contract = None
        for c in long_contracts:
            if abs(c["strike"] - short_contract["strike"]) < 0.01:
                q = quotes.get(c["symbol"], {})
                bid = q.get("bid", 0) or 0
                ask = q.get("ask", 0) or 0
                if ask > 0:
                    long_contract = {**c, **q, "mid": (bid + ask) / 2}
                    break

        # If no exact match, find closest strike
        if not long_contract:
            long_contract = self._find_best_contract(
                long_contracts, quotes, short_contract["strike"], prefer_short=False
            )

        if not long_contract:
            return None

        # Ensure long expiration is after short
        short_exp = short_contract.get("expiration")
        long_exp = long_contract.get("expiration")

        if isinstance(short_exp, date) and isinstance(long_exp, date):
            if long_exp <= short_exp:
                return None

        # Calendar is a debit spread
        net_debit = (long_contract["mid"] - short_contract["mid"]) * 100

        # Max loss is net debit (if stock moves dramatically)
        max_loss = abs(net_debit)

        # Max profit is difficult to calculate precisely
        # Estimate based on theta differential
        short_theta = abs(short_contract.get("theta", 0) or 0)
        long_theta = abs(long_contract.get("theta", 0) or 0)
        theta_advantage = short_theta - long_theta

        return {
            "ticker": ticker,
            "option_type": option_type,
            "strike": short_contract["strike"],
            "underlying_price": underlying_price,
            "short_leg": {
                "symbol": short_contract["symbol"],
                "expiration": str(short_contract.get("expiration")),
                "dte": self._calc_dte(short_contract.get("expiration")),
                "delta": short_contract.get("delta"),
                "theta": short_contract.get("theta"),
                "iv": short_contract.get("iv"),
                "mid": short_contract["mid"],
            },
            "long_leg": {
                "symbol": long_contract["symbol"],
                "expiration": str(long_contract.get("expiration")),
                "dte": self._calc_dte(long_contract.get("expiration")),
                "delta": long_contract.get("delta"),
                "theta": long_contract.get("theta"),
                "iv": long_contract.get("iv"),
                "mid": long_contract["mid"],
            },
            "net_debit": round(net_debit, 2),
            "max_loss": round(max_loss, 2),
            "theta_advantage_daily": round(theta_advantage * 100, 2),
            "dte_spread": (
                self._calc_dte(long_contract.get("expiration")) -
                self._calc_dte(short_contract.get("expiration"))
            ),
        }

    def _find_best_contract(
        self,
        contracts: list[dict],
        quotes: dict,
        target_strike: float,
        prefer_short: bool
    ) -> dict[str, Any] | None:
        """Find best contract based on strike and DTE preferences."""
        candidates = []

        for c in contracts:
            q = quotes.get(c["symbol"], {})
            bid = q.get("bid", 0) or 0
            ask = q.get("ask", 0) or 0

            if bid <= 0 or ask <= 0:
                continue

            strike_diff = abs(c["strike"] - target_strike)
            dte = self._calc_dte(c.get("expiration"))

            # Score: prefer closer strike, with DTE preference
            score = -strike_diff
            if prefer_short:
                score -= dte * 0.1  # Prefer shorter DTE
            else:
                score += dte * 0.1  # Prefer longer DTE

            candidates.append({
                **c, **q,
                "mid": (bid + ask) / 2,
                "score": score,
            })

        if not candidates:
            return None

        candidates.sort(key=lambda x: x["score"], reverse=True)
        return candidates[0]

    def _calc_dte(self, expiration: date | str | None) -> int:
        """Calculate days to expiration."""
        if expiration is None:
            return 0
        if isinstance(expiration, str):
            expiration = date.fromisoformat(expiration)
        return (expiration - date.today()).days

    def execute(
        self,
        setup: dict[str, Any],
        quantity: int = 1
    ) -> dict[str, Any]:
        """Execute calendar spread.

        Args:
            setup: Setup from find_setup()
            quantity: Number of spreads

        Returns:
            Execution result
        """
        try:
            # Sell short-term option
            o1 = self.client.submit_limit_option(
                setup["short_leg"]["symbol"], quantity, "sell",
                setup["short_leg"]["mid"], time_in_force="day"
            )

            # Buy long-term option
            o2 = self.client.submit_limit_option(
                setup["long_leg"]["symbol"], quantity, "buy",
                setup["long_leg"]["mid"], time_in_force="day"
            )

            # Record in database
            with session_scope() as s:
                order_id = insert_returning_id(s, """
                    INSERT INTO multi_leg_orders (
                        project_id, strategy_type, underlying, status,
                        leg1_symbol, leg1_side, leg1_qty,
                        leg2_symbol, leg2_side, leg2_qty,
                        net_credit, max_loss, expiration
                    )
                    VALUES (
                        :p, 'CALENDAR_SPREAD', :und, 'OPEN',
                        :l1s, 'SELL', :qty,
                        :l2s, 'BUY', :qty,
                        :credit, :loss, :exp
                    )
                """, {
                    "p": self.project_id,
                    "und": setup["ticker"],
                    "l1s": setup["short_leg"]["symbol"],
                    "l2s": setup["long_leg"]["symbol"],
                    "qty": quantity,
                    "credit": -setup["net_debit"],  # Negative = debit
                    "loss": setup["max_loss"],
                    "exp": setup["short_leg"]["expiration"],
                })
                s.commit()

            EventsRepo.log(self.project_id, "CalendarSpread", "EXECUTE", {
                "order_id": order_id,
                "ticker": setup["ticker"],
                "strike": setup["strike"],
                "net_debit": setup["net_debit"],
                "dte_spread": setup["dte_spread"],
            })

            return {
                "success": True,
                "order_id": order_id,
                "orders": [o1, o2],
                "net_debit": setup["net_debit"],
            }

        except Exception as e:
            logger.exception("Calendar spread execution failed: %s", e)
            return {"success": False, "error": str(e)}


def roll_short_leg(
    project_id: str,
    order_id: int,
    new_dte: int = 14
) -> dict[str, Any]:
    """Roll the short leg of a calendar spread to new expiration.

    Args:
        project_id: Trading project ID
        order_id: Multi-leg order ID
        new_dte: Target DTE for new short leg

    Returns:
        Roll execution result
    """
    # Get existing position
    with session_scope() as s:
        row = s.execute(text("""
            SELECT underlying, leg1_symbol, leg1_qty, leg2_symbol
            FROM multi_leg_orders
            WHERE project_id = :p AND order_id = :oid AND status = 'OPEN'
        """), {"p": project_id, "oid": order_id}).fetchone()

    if not row:
        return {"error": "Position not found or not open"}

    ticker, old_short, qty, long_leg = row

    project = ProjectsRepo.get(project_id)
    if not project:
        return {"error": "Project not found"}

    client = AlpacaClient(project)

    # Determine option type and strike from old short
    # Symbol format: AAPL240119C00150000
    old_strike = None
    option_type = "call"

    if old_short:
        # Extract from OCC symbol
        try:
            strike_str = old_short[-8:]
            old_strike = int(strike_str) / 1000
            option_type = "call" if "C" in old_short else "put"
        except Exception:
            pass

    if old_strike is None:
        return {"error": "Could not parse old short leg"}

    try:
        # Buy to close old short
        snap_old = client.snapshots([old_short])
        old_quote = client.option_chain_quotes(ticker).get(old_short, {})
        old_ask = old_quote.get("ask", 0)

        if old_ask > 0:
            btc = client.submit_limit_option(old_short, qty, "buy", old_ask)
        else:
            return {"error": "No price for old short leg"}

        # Sell new short with same strike, new expiration
        new_contracts = client.list_option_contracts(
            ticker, option_type,
            min_dte=new_dte - 5,
            max_dte=new_dte + 5,
            min_strike=old_strike * 0.99,
            max_strike=old_strike * 1.01,
            limit=10
        )

        quotes = client.option_chain_quotes(ticker)

        new_short = None
        for c in new_contracts:
            if abs(c["strike"] - old_strike) < 1:
                q = quotes.get(c["symbol"], {})
                if q.get("bid", 0) > 0:
                    new_short = {**c, **q, "mid": (q.get("bid", 0) + q.get("ask", 0)) / 2}
                    break

        if not new_short:
            return {"error": "Could not find new short leg", "btc_order": btc}

        # Sell new short
        sto = client.submit_limit_option(
            new_short["symbol"], qty, "sell",
            new_short.get("bid", new_short["mid"])
        )

        # Update database
        with session_scope() as s:
            s.execute(text("""
                UPDATE multi_leg_orders
                SET leg1_symbol = :new_sym, expiration = :exp
                WHERE order_id = :oid
            """), {
                "oid": order_id,
                "new_sym": new_short["symbol"],
                "exp": new_short.get("expiration"),
            })
            s.commit()

        # Calculate roll credit/debit
        roll_credit = new_short.get("bid", 0) - old_ask

        EventsRepo.log(project_id, "CalendarSpread", "ROLL", {
            "order_id": order_id,
            "old_short": old_short,
            "new_short": new_short["symbol"],
            "roll_credit": round(roll_credit * 100, 2),
        })

        return {
            "success": True,
            "old_short": old_short,
            "new_short": new_short["symbol"],
            "roll_credit": round(roll_credit * 100, 2),
            "orders": [btc, sto],
        }

    except Exception as e:
        logger.exception("Calendar roll failed: %s", e)
        return {"error": str(e)}


def list_calendar_spreads(
    project_id: str,
    status: str | None = None
) -> list[dict[str, Any]]:
    """List calendar spread positions."""
    sql = """
        SELECT order_id, underlying, status, leg1_symbol, leg2_symbol,
               net_credit, max_loss, expiration, opened_at
        FROM multi_leg_orders
        WHERE project_id = :p AND strategy_type = 'CALENDAR_SPREAD'
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
            "short_leg": r[3],
            "long_leg": r[4],
            "net_debit": -float(r[5]) if r[5] else None,  # Stored as negative credit
            "max_loss": float(r[6]) if r[6] else None,
            "short_expiration": r[7].isoformat() if r[7] else None,
            "opened_at": r[8].isoformat() if r[8] else None,
        }
        for r in rows
    ]