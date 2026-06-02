"""ETrade brokerage adapter.

Phase 1 deliverable: OAuth 1.0a flow + endpoint URLs + method stubs. Once
you have ETrade developer credentials (etrade.com/etx/ris/developer) and
complete the OAuth dance via /etrade/connect, this client will be used by
get_broker(project) for any project whose broker_type == "etrade".

KEY DIFFERENCES FROM ALPACA
---------------------------
* Auth: OAuth 1.0a 3-legged flow (consumer creds + per-user access token).
* Token lifetime: access tokens expire daily at midnight US Eastern. They
  can be renewed if still valid, otherwise the user must re-authorize.
* Sandbox vs Production: different host AND different access token. Once
  a user generates a sandbox token, switching to production requires a
  fresh OAuth dance.
* Endpoint shapes differ from Alpaca — every adapter method below has a
  TODO marker showing the ETrade endpoint that needs implementing.

Reference docs: https://apisb.etrade.com/docs/api/
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Any, Iterable

from db.repositories import TradingProject

from .base import BrokerClient, BrokerNotConfigured

logger = logging.getLogger(__name__)

# Sandbox vs Production endpoints. ETrade gives separate creds for each.
SANDBOX_BASE = "https://apisb.etrade.com"
PRODUCTION_BASE = "https://api.etrade.com"

# OAuth endpoints (these are the same for both envs, but tokens are NOT
# interchangeable across envs).
OAUTH_REQUEST_TOKEN_URL = "https://api.etrade.com/oauth/request_token"
OAUTH_ACCESS_TOKEN_URL = "https://api.etrade.com/oauth/access_token"
OAUTH_RENEW_URL = "https://api.etrade.com/oauth/renew_access_token"
OAUTH_REVOKE_URL = "https://api.etrade.com/oauth/revoke_access_token"


def _authorize_url(consumer_key: str, oauth_token: str) -> str:
    """Build the URL the user must visit to grant access."""
    from urllib.parse import urlencode
    qs = urlencode({"key": consumer_key, "token": oauth_token})
    return f"https://us.etrade.com/e/t/etws/authorize?{qs}"


# ---------------------------------------------------------------------------
# OAuth helpers — usable from API routes without instantiating the client.
# ---------------------------------------------------------------------------

def begin_oauth(consumer_key: str, consumer_secret: str
                ) -> tuple[str, str, str]:
    """Step 1: fetch a request token. Returns
    (oauth_token, oauth_token_secret, authorize_url) — store the secret
    server-side, redirect the user to authorize_url."""
    from requests_oauthlib import OAuth1Session
    session = OAuth1Session(
        client_key=consumer_key,
        client_secret=consumer_secret,
        callback_uri="oob",  # ETrade gives the verifier code on-screen
    )
    response = session.fetch_request_token(OAUTH_REQUEST_TOKEN_URL)
    token = response.get("oauth_token", "")
    secret = response.get("oauth_token_secret", "")
    return token, secret, _authorize_url(consumer_key, token)


def complete_oauth(consumer_key: str, consumer_secret: str,
                   request_token: str, request_token_secret: str,
                   verifier: str) -> tuple[str, str]:
    """Step 3 (after user authorizes): exchange the request token for the
    final access token. Returns (access_token, access_token_secret)."""
    from requests_oauthlib import OAuth1Session
    session = OAuth1Session(
        client_key=consumer_key,
        client_secret=consumer_secret,
        resource_owner_key=request_token,
        resource_owner_secret=request_token_secret,
        verifier=verifier,
    )
    response = session.fetch_access_token(OAUTH_ACCESS_TOKEN_URL)
    return response.get("oauth_token", ""), response.get("oauth_token_secret", "")


def renew_access_token(consumer_key: str, consumer_secret: str,
                       access_token: str, access_token_secret: str) -> bool:
    """Renew an unexpired access token (resets the midnight-ET clock).
    Returns True on success. Use a daily cron at ~23:00 UTC."""
    from requests_oauthlib import OAuth1Session
    try:
        session = OAuth1Session(
            client_key=consumer_key, client_secret=consumer_secret,
            resource_owner_key=access_token,
            resource_owner_secret=access_token_secret,
        )
        r = session.get(OAUTH_RENEW_URL)
        return r.status_code == 200
    except Exception as e:
        logger.warning("ETrade token renewal failed: %s", e)
        return False


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------

class ETradeClient(BrokerClient):
    broker_name = "etrade"

    def __init__(self, project: TradingProject):
        self.project = project
        if not project.etrade_consumer_key or not project.etrade_consumer_secret:
            raise BrokerNotConfigured(
                "ETrade developer credentials missing on this project. "
                "Add the Consumer Key + Consumer Secret in project settings."
            )
        if not project.etrade_access_token or not project.etrade_access_token_secret:
            raise BrokerNotConfigured(
                "ETrade is connected but the user hasn't completed the OAuth "
                "authorization step yet. Visit /etrade/connect."
            )

        from requests_oauthlib import OAuth1Session
        self._session = OAuth1Session(
            client_key=project.etrade_consumer_key,
            client_secret=project.etrade_consumer_secret,
            resource_owner_key=project.etrade_access_token,
            resource_owner_secret=project.etrade_access_token_secret,
        )
        self._base = (PRODUCTION_BASE
                      if project.etrade_environment == "production"
                      else SANDBOX_BASE)
        self._account_id_key = project.etrade_account_id_key or ""

    def _url(self, path: str) -> str:
        return f"{self._base}{path}"

    # ---------------- Account / positions --------------------------------

    def get_account(self) -> dict[str, Any]:
        """TODO: GET /v1/accounts/{accountIdKey}/balance.json
        Map ETrade Balance response → standard shape."""
        raise NotImplementedError(
            "ETrade get_account: implement via /v1/accounts/{idKey}/balance.json"
        )

    def get_account_raw(self) -> dict[str, Any]:
        if not self._account_id_key:
            return {"error": "no account selected"}
        r = self._session.get(
            self._url(f"/v1/accounts/{self._account_id_key}/balance.json"),
            params={"instType": "BROKERAGE", "realTimeNAV": "true"},
        )
        if r.status_code != 200:
            return {"error": f"HTTP {r.status_code}", "body": r.text[:500]}
        try:
            return r.json()
        except Exception:
            return {"error": "non-JSON response", "body": r.text[:500]}

    def list_positions(self) -> list[dict[str, Any]]:
        """TODO: GET /v1/accounts/{accountIdKey}/portfolio.json
        Walk position rows, map to standard shape."""
        raise NotImplementedError(
            "ETrade list_positions: implement via /v1/accounts/{idKey}/portfolio.json"
        )

    def liquidate_position(self, symbol: str) -> dict[str, Any]:
        """TODO: POST /v1/accounts/{accountIdKey}/orders/preview.json
        then /place.json with market-close payload. ETrade requires a
        preview/place 2-step for every order."""
        raise NotImplementedError(
            "ETrade liquidate_position: implement preview+place dance"
        )

    # ---------------- Market data ----------------------------------------

    def snapshots(self, symbols: Iterable[str]) -> dict[str, Any]:
        """TODO: GET /v1/market/quote/{symbol1,symbol2,...}.json
        Map ETrade QuoteData response → {symbol: Snapshot}."""
        raise NotImplementedError(
            "ETrade snapshots: implement via /v1/market/quote/{syms}.json"
        )

    def daily_bars(self, symbol: str,
                   lookback_days: int = 5) -> list[dict[str, Any]]:
        """TODO: ETrade doesn't expose historical OHLCV directly. Options:
            * Use a free data source (yfinance) for backtest bars.
            * Use the live quote stream for current bars only."""
        raise NotImplementedError(
            "ETrade daily_bars: ETrade has no native OHLCV endpoint — "
            "consider piping yfinance here as a free fallback."
        )

    def active_us_equities(self,
                           limit: int | None = None) -> list[str]:
        """TODO: ETrade has no full equity-list endpoint. Use the project's
        watchlist (project_settings.watchlist) as the universe instead."""
        raise NotImplementedError(
            "ETrade active_us_equities: use project watchlist instead"
        )

    # ---------------- Options --------------------------------------------

    def list_option_contracts(self, underlying: str, contract_type: str,
                              min_dte: int, max_dte: int,
                              min_strike: float | None = None,
                              max_strike: float | None = None,
                              limit: int = 200) -> list[dict[str, Any]]:
        """TODO: GET /v1/market/optionchains.json?symbol=...
        Walk OptionChainResponse → filter by DTE/strike."""
        raise NotImplementedError(
            "ETrade list_option_contracts: implement via /v1/market/optionchains.json"
        )

    def option_chain_quotes(self, underlying: str,
                            expiration: date | None = None
                            ) -> dict[str, Any]:
        """TODO: same endpoint as list_option_contracts but keep bid/ask/etc."""
        raise NotImplementedError(
            "ETrade option_chain_quotes: implement via /v1/market/optionchains.json"
        )

    def submit_limit_option(self, option_symbol: str, qty: int, side: str,
                            limit_price: float,
                            time_in_force: str = "day") -> dict[str, Any]:
        """TODO: ETrade requires a preview/place 2-step. Build the XML/JSON
        payload, POST /v1/accounts/{idKey}/orders/preview.json, then
        /place.json with the returned previewId."""
        raise NotImplementedError(
            "ETrade submit_limit_option: implement preview+place dance"
        )

    # ---------------- Market schedule ------------------------------------

    def get_clock(self) -> dict[str, Any]:
        """ETrade doesn't expose a market-clock endpoint. Fall back to the
        Alpaca clock or NYSE calendar derivation. For Phase 1 we return a
        permissive 'open during US RTH' approximation."""
        from datetime import datetime, time, timezone, timedelta
        now = datetime.now(tz=timezone.utc)
        # Convert to US Eastern (DST-naive approximation: UTC-4 in summer,
        # UTC-5 in winter). For Phase 2 we'll use a real tz lib.
        et_offset = timedelta(hours=-4)
        et = now + et_offset
        is_weekday = et.weekday() < 5
        is_rth = is_weekday and time(9, 30) <= et.time() <= time(16, 0)
        return {
            "is_open": is_rth,
            "next_open": None,
            "next_close": None,
            "note": "approximate (no holidays)",
        }

    def get_calendar(self, days: int = 7) -> list[dict[str, Any]]:
        return []   # not exposed by ETrade
