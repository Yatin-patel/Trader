"""In-process fake AlpacaClient.

Implements the full BrokerClient surface area with deterministic synthetic
data. Used by integration tests so we never touch the real Alpaca API.

Design choice: a single class with simple in-memory state, not a hierarchy
of mocks. The state is mutable so tests can change it (e.g. simulate an
order rejection) by assigning to the instance attributes between calls.

Coverage: covers the methods Worker._run_one_cycle() exercises end to end,
i.e. Scanner -> Strategist -> Guardrail -> Executor. Methods not used in
the wheel pipeline raise NotImplementedError to make test failures loud.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable


@dataclass
class FakeSnapshot:
    symbol: str
    last_price: float
    prev_close: float
    volume: int
    pct_change: float


@dataclass
class FakeOrder:
    id: str
    symbol: str
    qty: float
    side: str
    type: str = "limit"
    limit_price: float | None = None
    status: str = "accepted"
    submitted_at: datetime = field(
        default_factory=lambda: datetime.now(tz=timezone.utc))


class FakeAlpacaClient:
    """Minimal stand-in for execution.AlpacaClient.

    Construct it with the universe + strike grid you want the strategist
    to see. Defaults give a plausible mid-cap wheel watchlist that always
    has tradeable contracts.
    """

    def __init__(self, project, *,
                 cash: float = 25_000.0,
                 buying_power: float = 50_000.0,
                 options_buying_power: float = 25_000.0,
                 equity: float = 25_000.0,
                 market_open: bool = True,
                 universe_prices: dict[str, float] | None = None,
                 reject_orders: bool = False):
        self.project = project
        self._account = {
            "cash": cash,
            "buying_power": buying_power,
            "options_buying_power": options_buying_power,
            "equity": equity,
            "portfolio_value": equity,
        }
        self._market_open = market_open
        self._prices = universe_prices or {
            # Liquid mid-caps that pass typical scanner filters
            "F":    14.5, "SOFI": 18.2, "HOOD": 22.0,
            "NIO":   5.8, "PLTR": 25.3, "COIN": 28.1,
        }
        self._positions: list[dict[str, Any]] = []
        self._submitted_orders: list[FakeOrder] = []
        self.reject_orders = reject_orders

    # ------------------ Account / positions ----------------------------
    def get_account(self) -> dict[str, Any]:
        return dict(self._account)

    def get_account_raw(self) -> dict[str, Any]:
        return dict(self._account)

    def list_positions(self) -> list[dict[str, Any]]:
        return list(self._positions)

    # ------------------ Market data ------------------------------------
    def snapshots(self, symbols: Iterable[str]) -> dict[str, Any]:
        out: dict[str, FakeSnapshot] = {}
        for sym in symbols:
            sym = sym.upper()
            price = self._prices.get(sym)
            if price is None:
                continue
            # Synthetic 2.5% up day so volume + pct_change filters pass.
            prev = price / 1.025
            out[sym] = FakeSnapshot(
                symbol=sym, last_price=price, prev_close=prev,
                volume=5_000_000,
                pct_change=2.5,
            )
        return out

    def daily_bars(self, symbol: str, lookback_days: int = 5
                   ) -> list[dict[str, Any]]:
        price = self._prices.get(symbol.upper(), 10.0)
        today = date.today()
        out = []
        for i in range(lookback_days):
            d = today - timedelta(days=i)
            # Tiny synthetic wiggle so IV-rank-style calcs don't NaN.
            mult = 1.0 + (i * 0.001) - 0.002
            out.append({
                "o": price * mult, "h": price * (mult + 0.005),
                "l": price * (mult - 0.005), "c": price * mult,
                "v": 5_000_000, "t": d,
            })
        out.reverse()
        return out

    def active_us_equities(self, limit: int | None = None) -> list[str]:
        out = list(self._prices.keys())
        return out if limit is None else out[:limit]

    # ------------------ Options ----------------------------------------
    def list_option_contracts(self, underlying: str, contract_type: str,
                              min_dte: int = 7, max_dte: int = 45,
                              min_strike: float | None = None,
                              max_strike: float | None = None,
                              limit: int = 200) -> list[dict[str, Any]]:
        underlying = underlying.upper()
        price = self._prices.get(underlying)
        if price is None:
            return []
        # Synthesize one expiration about 21 days out and a strike ladder
        # straddling the underlying.
        exp = date.today() + timedelta(days=21)
        if (exp - date.today()).days < min_dte or \
           (exp - date.today()).days > max_dte:
            return []
        contracts = []
        for delta_pct in (-0.15, -0.10, -0.05, 0, 0.05, 0.10):
            strike = round(price * (1 + delta_pct), 0)
            if min_strike is not None and strike < min_strike:
                continue
            if max_strike is not None and strike > max_strike:
                continue
            sym = f"{underlying}{exp.strftime('%y%m%d')}" \
                  f"{'P' if contract_type=='put' else 'C'}" \
                  f"{int(strike*1000):08d}"
            contracts.append({
                "symbol": sym,
                "strike": strike,
                "expiration": exp,
                "open_interest": 500,
            })
        return contracts

    def option_chain_quotes(self, underlying: str,
                            expiration: date | None = None
                            ) -> dict[str, Any]:
        """Return synthetic quotes for every contract on this underlying
        whose strike is reasonable. The strategist will pick the
        highest-EV one within the configured delta band.

        Delta model uses a simple linear approximation:
            put_delta  ≈ -0.5 - (moneyness * 2.5)  clamped to [-0.99, -0.01]
            call_delta ≈ +0.5 - (moneyness * 2.5)  clamped to [+0.01, +0.99]
        where ``moneyness = (strike - spot) / spot``. This gives the
        textbook shape: OTM puts have |delta| near 0, ATM near 0.5, ITM
        approaching 1. Without this, every OTM put in the synthetic
        chain ended up at |delta|>0.50 and the strategist rejected all
        of them — defeating the point of the test.
        """
        underlying = underlying.upper()
        price = self._prices.get(underlying)
        if price is None:
            return {}
        # Tag each contract with whether we generated it as a put or call —
        # parsing OCC symbol strings is brittle for tickers that contain
        # the letter 'P' (e.g. PLTR), and we already know what we asked for.
        puts = self.list_option_contracts(underlying, "put",
                                          min_dte=0, max_dte=60)
        calls = self.list_option_contracts(underlying, "call",
                                           min_dte=0, max_dte=60)
        tagged = [(c, True) for c in puts] + [(c, False) for c in calls]
        out: dict[str, Any] = {}
        for c, is_put in tagged:
            strike = float(c["strike"])
            moneyness = (strike - price) / price
            if is_put:
                delta = max(-0.99, min(-0.01, -0.5 - moneyness * 2.5))
            else:
                delta = max(0.01, min(0.99, 0.5 - moneyness * 2.5))
            intrinsic = max(0.0, (strike - price) if is_put
                            else (price - strike))
            extrinsic = abs(delta) * price * 0.05 + 0.10
            mid = intrinsic + extrinsic
            out[c["symbol"]] = {
                "bid": round(mid * 0.95, 2),
                "ask": round(mid * 1.05, 2),
                "delta": round(delta, 4),
                "iv": 0.45,
                "open_interest": 500,
            }
        return out

    def submit_limit_option(self, option_symbol: str, qty: int, side: str,
                            limit_price: float,
                            time_in_force: str = "day") -> dict[str, Any]:
        if self.reject_orders:
            raise RuntimeError("test forced rejection")
        order = FakeOrder(
            id=f"fake-{len(self._submitted_orders)+1:05d}",
            symbol=option_symbol, qty=float(qty), side=side,
            type="limit", limit_price=limit_price, status="accepted",
        )
        self._submitted_orders.append(order)
        return {
            "id": order.id, "symbol": order.symbol, "qty": order.qty,
            "side": order.side, "type": order.type,
            "limit_price": order.limit_price, "status": order.status,
            "submitted_at": order.submitted_at,
        }

    def submit_market_equity(self, symbol: str, qty: int, side: str,
                             time_in_force: str = "day",
                             extended_hours: bool = False
                             ) -> dict[str, Any]:
        order = FakeOrder(
            id=f"fake-eq-{len(self._submitted_orders)+1:05d}",
            symbol=symbol, qty=float(qty), side=side, type="market",
            status="accepted",
        )
        self._submitted_orders.append(order)
        return {
            "id": order.id, "symbol": order.symbol, "qty": order.qty,
            "side": order.side, "type": order.type,
            "status": order.status, "submitted_at": order.submitted_at,
        }

    def liquidate_position(self, symbol: str) -> dict[str, Any]:
        self._positions = [p for p in self._positions
                           if p.get("symbol") != symbol]
        return {"status": "liquidated", "symbol": symbol}

    # ------------------ Market schedule --------------------------------
    def get_market_clock(self) -> dict[str, Any]:
        now = datetime.now(tz=timezone.utc)
        return {
            "is_open": self._market_open,
            "timestamp": now,
            "next_open": now + timedelta(hours=12),
            "next_close": now + timedelta(hours=6) if self._market_open else None,
        }

    def get_clock(self) -> dict[str, Any]:
        return self.get_market_clock()

    def get_calendar(self, days: int = 7) -> list[dict[str, Any]]:
        today = date.today()
        return [{"date": (today + timedelta(days=i)).isoformat(),
                 "open": "09:30", "close": "16:00"}
                for i in range(days) if (today + timedelta(days=i)).weekday() < 5]

    # ------------------ Test introspection helpers ---------------------
    def submitted_orders(self) -> list[FakeOrder]:
        return list(self._submitted_orders)

    def reset_orders(self) -> None:
        self._submitted_orders.clear()
