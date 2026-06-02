"""BrokerClient — common interface every brokerage adapter implements.

Phase 1 of the multi-broker work: defines what the agents (Scanner /
Strategist / Guardrail / Executor / TakeProfit / etc.) expect from
*any* broker. Implementations live in:

  * execution.alpaca_client.AlpacaClient   — fully working
  * execution.etrade_client.ETradeClient   — OAuth + skeleton

All call sites should obtain a client via execution.get_broker(project),
NOT by instantiating AlpacaClient directly.
"""
from __future__ import annotations

import abc
from datetime import date
from typing import Any, Iterable


class BrokerClient(abc.ABC):
    """Abstract brokerage interface.

    Every method documents shape contracts. Concrete adapters must return
    the same field names so the agents stay broker-agnostic.
    """

    broker_name: str = "unknown"   # adapters override

    # ---------------- Account / positions --------------------------------

    @abc.abstractmethod
    def get_account(self) -> dict[str, Any]:
        """Return at minimum:
            {cash, buying_power, equity, portfolio_value,
             options_buying_power}
        All values are floats. Adapters may include extra keys."""

    @abc.abstractmethod
    def get_account_raw(self) -> dict[str, Any]:
        """Dump everything the broker returns — used for diagnostics."""

    @abc.abstractmethod
    def list_positions(self) -> list[dict[str, Any]]:
        """Each item must have:
            symbol, qty (float), asset_class ('us_equity'|'us_option'),
            avg_entry_price, current_price, market_value, unrealized_pl"""

    @abc.abstractmethod
    def liquidate_position(self, symbol: str) -> dict[str, Any]:
        """Close-at-market. Return order dict or {'error': str}."""

    # ---------------- Market data ----------------------------------------

    @abc.abstractmethod
    def snapshots(self, symbols: Iterable[str]) -> dict[str, Any]:
        """One snapshot per symbol with: symbol, last_price, prev_close,
        volume, pct_change. Missing symbols may be omitted."""

    @abc.abstractmethod
    def daily_bars(self, symbol: str,
                   lookback_days: int = 5) -> list[dict[str, Any]]:
        """OHLCV bars, oldest first. Each: {o, h, l, c, v, t}."""

    @abc.abstractmethod
    def active_us_equities(self,
                           limit: int | None = None) -> list[str]:
        """List of tradeable US equity symbols."""

    # ---------------- Options --------------------------------------------

    @abc.abstractmethod
    def list_option_contracts(self, underlying: str, contract_type: str,
                              min_dte: int, max_dte: int,
                              min_strike: float | None = None,
                              max_strike: float | None = None,
                              limit: int = 200) -> list[dict[str, Any]]:
        """Each contract: {symbol, strike, expiration (date), delta?, ...}"""

    @abc.abstractmethod
    def option_chain_quotes(self, underlying: str,
                            expiration: date | None = None) -> dict[str, Any]:
        """Quote keyed by option symbol: {symbol: {bid, ask, last, ...}}"""

    @abc.abstractmethod
    def submit_limit_option(self, option_symbol: str, qty: int, side: str,
                            limit_price: float,
                            time_in_force: str = "day") -> dict[str, Any]:
        """Submit a limit option order. Side: 'buy' | 'sell'."""

    # ---------------- Market schedule ------------------------------------

    @abc.abstractmethod
    def get_clock(self) -> dict[str, Any]:
        """{is_open: bool, next_open: datetime, next_close: datetime}"""

    @abc.abstractmethod
    def get_calendar(self, days: int = 7) -> list[dict[str, Any]]:
        """Upcoming trading sessions."""


class BrokerNotConfigured(RuntimeError):
    """Raised when an adapter's credentials are missing or expired.
    UI catches this and shows a reconnect button."""
