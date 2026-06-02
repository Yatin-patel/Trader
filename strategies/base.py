"""Strategy protocol — minimal so it's easy to add new ones."""
from __future__ import annotations

from typing import Any, Protocol


class Strategy(Protocol):
    name: str
    description: str

    def evaluate(self, *, project_id: str, ticker: str,
                 last_price: float, settings: dict[str, Any]) -> dict[str, Any] | None:
        """Return a proposed trade dict or None to skip.

        Trade dict contract:
          {ticker, type, option_symbol, strike, expiration, delta,
           premium, underlying_price, narrative}
        """
        ...
