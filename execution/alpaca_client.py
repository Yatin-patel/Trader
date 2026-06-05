"""Thin, project-scoped wrapper around alpaca-py.

Each tenant gets its own client instance. Keys come from `TradingProject`,
never the environment. Endpoints (paper vs live) come from the same record.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable

from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.requests import (
    OptionChainRequest,
    StockBarsRequest,
    StockSnapshotRequest,
)
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import (
    AssetClass,
    AssetStatus,
    ContractType,
    OrderSide,
    OrderStatus,
    OrderType,
    TimeInForce,
)
from alpaca.trading.requests import (
    GetAssetsRequest,
    GetOptionContractsRequest,
    LimitOrderRequest,
    MarketOrderRequest,
    StopLossRequest,
    TakeProfitRequest,
    TrailingStopOrderRequest,
)

from db.repositories import TradingProject

from .base import BrokerClient


@dataclass
class Snapshot:
    symbol: str
    last_price: float
    prev_close: float
    volume: int
    pct_change: float


class AlpacaClient(BrokerClient):
    broker_name = "alpaca"

    def __init__(self, project: TradingProject):
        self.project = project
        paper = "paper" in (project.alpaca_base_url or "")
        self.trading = TradingClient(
            api_key=project.alpaca_api_key,
            secret_key=project.alpaca_secret_key,
            paper=paper,
            url_override=project.alpaca_base_url or None,
        )
        self.stock_data = StockHistoricalDataClient(
            api_key=project.alpaca_api_key,
            secret_key=project.alpaca_secret_key,
        )
        self.option_data = OptionHistoricalDataClient(
            api_key=project.alpaca_api_key,
            secret_key=project.alpaca_secret_key,
        )

    # ------------------ Account / positions ----------------------------------
    def get_account(self) -> dict[str, Any]:
        a = self.trading.get_account()
        return {
            "cash": float(a.cash),
            "buying_power": float(a.buying_power),
            "equity": float(a.equity),
            "portfolio_value": float(a.portfolio_value),
            "options_buying_power": float(getattr(a, "options_buying_power", 0) or 0),
            # last_equity = equity at the prior close. Used by the
            # projects-dashboard's "Today's Gain/Loss" column.
            "last_equity": float(getattr(a, "last_equity", 0) or 0),
        }

    def get_account_raw(self) -> dict[str, Any]:
        """Dump every field Alpaca returns on the account object.

        Used for diagnosing buying-power and PDT issues. Strings are returned
        as-is so the caller sees the exact values Alpaca reports.
        """
        a = self.trading.get_account()
        if hasattr(a, "model_dump"):
            return a.model_dump(mode="json")
        out: dict[str, Any] = {}
        for k in dir(a):
            if k.startswith("_"):
                continue
            try:
                v = getattr(a, k)
            except Exception:
                continue
            if callable(v):
                continue
            try:
                out[k] = v if isinstance(v, (str, int, float, bool, type(None))) else str(v)
            except Exception:
                out[k] = "<unprintable>"
        return out

    def list_positions(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for p in self.trading.get_all_positions():
            out.append({
                "symbol": p.symbol,
                "qty": float(p.qty),
                "asset_class": str(p.asset_class.value if hasattr(p.asset_class, "value") else p.asset_class),
                "avg_entry_price": float(p.avg_entry_price),
                "current_price": float(p.current_price) if p.current_price else None,
                "market_value": float(p.market_value) if p.market_value else None,
                "unrealized_pl": float(p.unrealized_pl) if p.unrealized_pl else None,
            })
        return out

    # ------------------ Market data ------------------------------------------
    def snapshots(self, symbols: Iterable[str]) -> dict[str, Snapshot]:
        symbols = list({s.upper() for s in symbols if s})
        if not symbols:
            return {}
        req = StockSnapshotRequest(symbol_or_symbols=symbols, feed=self.project.alpaca_data_feed)
        raw = self.stock_data.get_stock_snapshot(req)
        out: dict[str, Snapshot] = {}
        for sym, snap in raw.items():
            last = float(snap.latest_trade.price) if snap.latest_trade else 0.0
            prev = float(snap.previous_daily_bar.close) if snap.previous_daily_bar else 0.0
            vol = int(snap.daily_bar.volume) if snap.daily_bar else 0
            pct = ((last - prev) / prev * 100.0) if prev else 0.0
            out[sym] = Snapshot(symbol=sym, last_price=last, prev_close=prev,
                                volume=vol, pct_change=pct)
        return out

    def active_us_equities(self, limit: int | None = None) -> list[str]:
        req = GetAssetsRequest(status=AssetStatus.ACTIVE, asset_class=AssetClass.US_EQUITY)
        assets = self.trading.get_all_assets(req)
        syms = [a.symbol for a in assets if a.tradable and not a.symbol.endswith(("W", "U"))]
        return syms if limit is None else syms[:limit]

    def daily_bars(self, symbol: str, lookback_days: int = 5) -> list[dict[str, Any]]:
        end = datetime.now(tz=timezone.utc)
        start = end - timedelta(days=lookback_days * 2)
        req = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame.Day,
            start=start,
            end=end,
            feed=self.project.alpaca_data_feed,
        )
        bars = self.stock_data.get_stock_bars(req).data.get(symbol, [])
        return [{"o": float(b.open), "h": float(b.high), "l": float(b.low),
                 "c": float(b.close), "v": int(b.volume), "t": b.timestamp} for b in bars[-lookback_days:]]

    # ------------------ Options ----------------------------------------------
    def list_option_contracts(self, underlying: str, contract_type: str,
                              min_dte: int, max_dte: int,
                              min_strike: float | None = None,
                              max_strike: float | None = None,
                              limit: int = 200) -> list[dict[str, Any]]:
        today = date.today()
        req = GetOptionContractsRequest(
            underlying_symbols=[underlying],
            type=ContractType.PUT if contract_type.lower() == "put" else ContractType.CALL,
            expiration_date_gte=today + timedelta(days=min_dte),
            expiration_date_lte=today + timedelta(days=max_dte),
            strike_price_gte=str(min_strike) if min_strike is not None else None,
            strike_price_lte=str(max_strike) if max_strike is not None else None,
            limit=limit,
            status=AssetStatus.ACTIVE,
        )
        page = self.trading.get_option_contracts(req)
        contracts = []
        for c in page.option_contracts or []:
            contracts.append({
                "symbol": c.symbol,
                "underlying": c.underlying_symbol,
                "strike": float(c.strike_price),
                "expiration": c.expiration_date,
                "type": str(c.type.value if hasattr(c.type, "value") else c.type),
                "open_interest": int(c.open_interest or 0),
                "size": int(c.size or 100),
            })
        return contracts

    def option_chain_quotes(self, underlying: str, expiration: date | None = None) -> dict[str, Any]:
        req = OptionChainRequest(underlying_symbol=underlying,
                                 expiration_date=expiration)
        try:
            chain = self.option_data.get_option_chain(req)
        except Exception:
            return {}
        out: dict[str, Any] = {}
        for sym, snap in chain.items():
            greeks = getattr(snap, "greeks", None)
            quote = getattr(snap, "latest_quote", None)
            out[sym] = {
                "delta": float(greeks.delta) if greeks and greeks.delta is not None else None,
                "gamma": float(greeks.gamma) if greeks and greeks.gamma is not None else None,
                "theta": float(greeks.theta) if greeks and greeks.theta is not None else None,
                "vega":  float(greeks.vega)  if greeks and greeks.vega  is not None else None,
                "iv":    float(snap.implied_volatility) if getattr(snap, "implied_volatility", None) is not None else None,
                "bid": float(quote.bid_price) if quote else None,
                "ask": float(quote.ask_price) if quote else None,
            }
        return out

    # ------------------ Orders -----------------------------------------------
    def submit_market_equity(self, symbol: str, qty: int, side: str,
                             time_in_force: str = "day",
                             extended_hours: bool = False) -> dict[str, Any]:
        req = MarketOrderRequest(
            symbol=symbol,
            qty=qty,
            side=OrderSide.BUY if side.lower() == "buy" else OrderSide.SELL,
            time_in_force=TimeInForce(time_in_force.lower()),
            extended_hours=extended_hours,
        )
        order = self.trading.submit_order(req)
        return self._order_dict(order)

    def submit_limit_option(self, option_symbol: str, qty: int, side: str,
                            limit_price: float, time_in_force: str = "day") -> dict[str, Any]:
        req = LimitOrderRequest(
            symbol=option_symbol,
            qty=qty,
            side=OrderSide.SELL if side.lower() == "sell" else OrderSide.BUY,
            time_in_force=TimeInForce(time_in_force.lower()),
            limit_price=round(limit_price, 2),
        )
        order = self.trading.submit_order(req)
        return self._order_dict(order)

    # ---------------- Multi-leg (atomic, no partial-fill risk) ----------

    def supports_multi_leg(self) -> bool:
        # Alpaca's v2 /orders supports order_class=mleg for up to 4
        # option legs as of 2024.
        return True

    def submit_multi_leg_option(
        self,
        legs: list[dict[str, Any]],
        qty: int,
        net_limit_price: float,
        time_in_force: str = "day",
    ) -> dict[str, Any]:
        """Submit an atomic multi-leg options order via Alpaca's
        ``order_class=mleg`` REST endpoint.

        We POST directly rather than going through alpaca-py so the
        payload doesn't depend on a particular SDK minor version
        shipping OptionLegRequest (different SDK builds spell the
        field differently). The endpoint is the same `/v2/orders`
        path used by single-leg orders; we authenticate with the
        same APCA-API-KEY-ID / APCA-API-SECRET-KEY headers the SDK
        attaches.
        """
        import json as _json

        import requests as _requests

        base = (self.project.alpaca_base_url
                or "https://paper-api.alpaca.markets").rstrip("/")
        url = f"{base}/v2/orders"
        headers = {
            "APCA-API-KEY-ID":     self.project.alpaca_api_key,
            "APCA-API-SECRET-KEY": self.project.alpaca_secret_key,
            "Content-Type":        "application/json",
            "Accept":              "application/json",
        }
        payload_legs = []
        for leg in legs:
            side = str(leg.get("side") or "").lower()
            intent = str(leg.get("position_intent") or "").lower()
            if not intent:
                # Default to opening intents — strategies submit closes
                # via single-leg flow today.
                intent = ("buying_to_open" if side == "buy"
                          else "selling_to_open")
            payload_legs.append({
                "symbol":          leg["symbol"],
                "ratio_qty":       str(int(leg.get("ratio_qty") or 1)),
                "side":            "buy" if side == "buy" else "sell",
                "position_intent": intent,
            })
        body = {
            "order_class":    "mleg",
            "qty":             str(int(qty)),
            "type":            "limit",
            "time_in_force":   time_in_force.lower(),
            "limit_price":     str(round(float(net_limit_price), 2)),
            "legs":            payload_legs,
        }
        r = _requests.post(url, headers=headers, data=_json.dumps(body),
                           timeout=20)
        if r.status_code >= 400:
            # Surface Alpaca's machine-readable error so the executor
            # can classify "4xx with code:4*" as a routine broker
            # rejection (insufficient BP, halted symbol, etc.) just
            # like single-leg orders do.
            raise RuntimeError(f"alpaca mleg HTTP {r.status_code}: {r.text[:500]}")
        try:
            return r.json()
        except Exception:
            return {"status": "submitted", "body": r.text[:500]}

    def liquidate_position(self, symbol: str) -> dict[str, Any]:
        try:
            order = self.trading.close_position(symbol)
            return self._order_dict(order)
        except Exception as e:
            return {"error": str(e), "symbol": symbol}

    def submit_trailing_stop(
        self,
        symbol: str,
        qty: int,
        side: str,
        trail_percent: float | None = None,
        trail_price: float | None = None,
        time_in_force: str = "day"
    ) -> dict[str, Any]:
        """Submit a trailing stop order.

        Args:
            symbol: Stock or option symbol
            qty: Number of shares/contracts
            side: 'buy' or 'sell'
            trail_percent: Trailing percentage (e.g., 0.05 for 5%)
            trail_price: Trailing dollar amount (alternative to percent)
            time_in_force: Order duration

        Returns:
            Order details dict
        """
        req = TrailingStopOrderRequest(
            symbol=symbol,
            qty=qty,
            side=OrderSide.SELL if side.lower() == "sell" else OrderSide.BUY,
            time_in_force=TimeInForce(time_in_force.lower()),
            trail_percent=str(trail_percent * 100) if trail_percent else None,
            trail_price=str(trail_price) if trail_price else None,
        )
        order = self.trading.submit_order(req)
        return self._order_dict(order)

    def submit_bracket_order(
        self,
        symbol: str,
        qty: int,
        side: str,
        limit_price: float | None = None,
        take_profit_price: float | None = None,
        stop_loss_price: float | None = None,
        time_in_force: str = "day"
    ) -> dict[str, Any]:
        """Submit a bracket (OCO) order with take-profit and stop-loss.

        Args:
            symbol: Stock or option symbol
            qty: Number of shares/contracts
            side: 'buy' or 'sell'
            limit_price: Entry limit price (None for market order)
            take_profit_price: Take profit limit price
            stop_loss_price: Stop loss trigger price
            time_in_force: Order duration

        Returns:
            Order details dict with parent and child order IDs
        """
        order_side = OrderSide.BUY if side.lower() == "buy" else OrderSide.SELL

        # Build the request
        if limit_price:
            req = LimitOrderRequest(
                symbol=symbol,
                qty=qty,
                side=order_side,
                time_in_force=TimeInForce(time_in_force.lower()),
                limit_price=round(limit_price, 2),
                order_class="bracket",
                take_profit=TakeProfitRequest(limit_price=round(take_profit_price, 2)) if take_profit_price else None,
                stop_loss=StopLossRequest(stop_price=round(stop_loss_price, 2)) if stop_loss_price else None,
            )
        else:
            req = MarketOrderRequest(
                symbol=symbol,
                qty=qty,
                side=order_side,
                time_in_force=TimeInForce(time_in_force.lower()),
                order_class="bracket",
                take_profit=TakeProfitRequest(limit_price=round(take_profit_price, 2)) if take_profit_price else None,
                stop_loss=StopLossRequest(stop_price=round(stop_loss_price, 2)) if stop_loss_price else None,
            )

        order = self.trading.submit_order(req)
        result = self._order_dict(order)

        # Include child order IDs
        if hasattr(order, "legs") and order.legs:
            result["child_orders"] = [
                {"id": str(leg.id), "type": str(leg.order_type.value if hasattr(leg.order_type, "value") else leg.order_type)}
                for leg in order.legs
            ]

        return result

    @staticmethod
    def _order_dict(order: Any) -> dict[str, Any]:
        return {
            "id": str(order.id),
            "symbol": order.symbol,
            "qty": float(order.qty) if order.qty else None,
            "side": str(order.side.value if hasattr(order.side, "value") else order.side),
            "type": str(order.order_type.value if hasattr(order.order_type, "value") else order.order_type),
            "status": str(order.status.value if hasattr(order.status, "value") else order.status),
            "submitted_at": getattr(order, "submitted_at", None),
        }

    # ------------------ Market clock -----------------------------------------
    def is_market_open(self) -> bool:
        try:
            return bool(self.trading.get_clock().is_open)
        except Exception:
            return False

    def reset_paper_account(self, cash: float = 100000.0) -> dict[str, Any]:
        """Reset the Alpaca *paper* account balance. Refuses on live URLs."""
        if "paper-api" not in (self.project.alpaca_base_url or ""):
            raise RuntimeError("Refusing to reset a non-paper account.")
        import httpx
        url = self.project.alpaca_base_url.rstrip("/") + "/v2/account/reset"
        headers = {
            "APCA-API-KEY-ID":     self.project.alpaca_api_key,
            "APCA-API-SECRET-KEY": self.project.alpaca_secret_key,
        }
        with httpx.Client(timeout=15.0) as c:
            r = c.post(url, json={"cash": float(cash)}, headers=headers)
            r.raise_for_status()
            return r.json()

    def get_market_clock(self) -> dict[str, Any]:
        """Return is_open, current Alpaca-side timestamp, next open and next close."""
        try:
            clock = self.trading.get_clock()
            return {
                "is_open":    bool(clock.is_open),
                "timestamp":  clock.timestamp,
                "next_open":  clock.next_open,
                "next_close": clock.next_close,
            }
        except Exception:
            return {"is_open": False, "timestamp": None,
                    "next_open": None, "next_close": None}

    # ------------- BrokerClient interface aliases ----------------------------
    # Required by the broker-agnostic ABC. Map to the existing
    # Alpaca-specific methods so call sites can pick either name.

    def get_clock(self) -> dict[str, Any]:
        return self.get_market_clock()

    def get_calendar(self, days: int = 7) -> list[dict[str, Any]]:
        try:
            from datetime import date, timedelta
            from alpaca.trading.requests import GetCalendarRequest
            req = GetCalendarRequest(
                start=date.today(),
                end=date.today() + timedelta(days=days),
            )
            sessions = self.trading.get_calendar(req)
            return [
                {"date": str(s.date),
                 "open": str(s.open) if s.open else None,
                 "close": str(s.close) if s.close else None}
                for s in sessions
            ]
        except Exception:
            return []
