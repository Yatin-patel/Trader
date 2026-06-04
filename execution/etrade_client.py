"""ETrade brokerage adapter.

Phase 2: OAuth 1.0a + every BrokerClient method wired through to the
ETrade REST v1 API (apisb.etrade.com sandbox / api.etrade.com prod).

KEY DIFFERENCES FROM ALPACA
---------------------------
* Auth: OAuth 1.0a 3-legged flow (consumer creds + per-user access token).
* Token lifetime: access tokens expire daily at midnight US Eastern. They
  can be renewed if still valid via /oauth/renew_access_token.
* Sandbox vs Production: different host AND different access token.
  Sandbox returns canned sample data — DO NOT assert on exact values
  in tests.
* Option symbols use the ETrade colon format
    ``underlier:year:month:day:optionType:strikePrice``
  rather than the OCC fixed-width format the rest of the codebase
  speaks. ``_occ_to_etrade`` / ``_etrade_to_occ`` convert both ways so
  callers stay broker-agnostic.
* Orders are a two-step preview-then-place dance. The ``previewId``
  expires 3 minutes after preview, so the place call must run
  immediately after preview returns.

Reference docs (verified by deep-research, 18/20 claims confirmed):
  https://apisb.etrade.com/docs/api/account/api-balance-v1.html
  https://apisb.etrade.com/docs/api/account/api-portfolio-v1.html
  https://apisb.etrade.com/docs/api/market/api-quote-v1.html
  https://apisb.etrade.com/docs/api/market/api-market-v1.html  (optionchains)
  https://apisb.etrade.com/docs/api/order/api-order-v1.html
"""
from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import date, datetime, time, timezone, timedelta
from typing import Any, Iterable

from db.repositories import TradingProject

from .base import BrokerClient, BrokerNotConfigured

logger = logging.getLogger(__name__)

SANDBOX_BASE = "https://apisb.etrade.com"
PRODUCTION_BASE = "https://api.etrade.com"

OAUTH_REQUEST_TOKEN_URL = "https://api.etrade.com/oauth/request_token"
OAUTH_ACCESS_TOKEN_URL = "https://api.etrade.com/oauth/access_token"
OAUTH_RENEW_URL = "https://api.etrade.com/oauth/renew_access_token"
OAUTH_REVOKE_URL = "https://api.etrade.com/oauth/revoke_access_token"

# Default JSON headers the API expects. The OAuth1Session injects the
# Authorization header automatically.
_JSON_HEADERS = {"Accept": "application/json"}
_JSON_POST_HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json",
}


# ---------------------------------------------------------------------------
# Symbol-format conversions
# ---------------------------------------------------------------------------

_OCC_RE = re.compile(r"^([A-Z.]+?)(\d{6})([CP])(\d{8})$")


def _occ_to_etrade(occ_symbol: str) -> str:
    """Convert OCC option symbol to ETrade colon-delimited format.

    OCC:    IBM231215C00200000  (root, YYMMDD, C/P, strike*1000 zero-padded)
    ETrade: IBM:2023:12:15:CALL:200.000000  (year:month:day:CALL|PUT:price)
    """
    m = _OCC_RE.match(occ_symbol.upper().strip())
    if not m:
        raise ValueError(f"unparseable OCC symbol: {occ_symbol!r}")
    root, yymmdd, cp, strike_thousandths = m.groups()
    year = 2000 + int(yymmdd[:2])
    month = int(yymmdd[2:4])
    day = int(yymmdd[4:6])
    cp_word = "CALL" if cp == "C" else "PUT"
    strike = int(strike_thousandths) / 1000.0
    # ETrade docs example uses 6 decimal places: 200.000000
    return f"{root}:{year}:{month}:{day}:{cp_word}:{strike:.6f}"


def _etrade_to_occ(root: str, year: int, month: int, day: int,
                   call_put: str, strike: float) -> str:
    """Inverse of ``_occ_to_etrade`` — given the decomposed ETrade pieces
    we get back from a portfolio / chain response, rebuild the OCC
    symbol the rest of the codebase uses."""
    cp = "C" if str(call_put).upper().startswith("C") else "P"
    yymmdd = f"{year % 100:02d}{month:02d}{day:02d}"
    strike_int = int(round(strike * 1000))
    return f"{root}{yymmdd}{cp}{strike_int:08d}"


# ---------------------------------------------------------------------------
# OAuth helpers (Phase 1, unchanged).
# ---------------------------------------------------------------------------

def _authorize_url(consumer_key: str, oauth_token: str) -> str:
    from urllib.parse import urlencode
    qs = urlencode({"key": consumer_key, "token": oauth_token})
    return f"https://us.etrade.com/e/t/etws/authorize?{qs}"


def begin_oauth(consumer_key: str, consumer_secret: str
                ) -> tuple[str, str, str]:
    from requests_oauthlib import OAuth1Session
    session = OAuth1Session(
        client_key=consumer_key,
        client_secret=consumer_secret,
        callback_uri="oob",
    )
    response = session.fetch_request_token(OAUTH_REQUEST_TOKEN_URL)
    token = response.get("oauth_token", "")
    secret = response.get("oauth_token_secret", "")
    return token, secret, _authorize_url(consumer_key, token)


def complete_oauth(consumer_key: str, consumer_secret: str,
                   request_token: str, request_token_secret: str,
                   verifier: str) -> tuple[str, str]:
    from requests_oauthlib import OAuth1Session
    session = OAuth1Session(
        client_key=consumer_key,
        client_secret=consumer_secret,
        resource_owner_key=request_token,
        resource_owner_secret=request_token_secret,
        verifier=verifier,
    )
    response = session.fetch_access_token(OAUTH_ACCESS_TOKEN_URL)
    return (response.get("oauth_token", ""),
            response.get("oauth_token_secret", ""))


def renew_access_token(consumer_key: str, consumer_secret: str,
                       access_token: str,
                       access_token_secret: str) -> bool:
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
# Internal response-shape helpers
# ---------------------------------------------------------------------------

def _unwrap(body: dict[str, Any], wrapper: str) -> dict[str, Any]:
    """ETrade wraps every payload in a PascalCase envelope object
    (BalanceResponse, AccountPortfolio, QuoteResponse, OptionChainResponse,
    PreviewOrderResponse, PlaceOrderResponse). Return the inner dict if
    present, else the body as-is — some sandbox endpoints return flat."""
    if not isinstance(body, dict):
        return {}
    if wrapper in body and isinstance(body[wrapper], dict):
        return body[wrapper]
    return body


def _as_float(v: Any, default: float = 0.0) -> float:
    """ETrade sometimes string-encodes numerics. Be tolerant."""
    if v is None:
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _as_int(v: Any, default: int = 0) -> int:
    if v is None:
        return default
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------

class ETradeClient(BrokerClient):
    broker_name = "etrade"

    def __init__(self, project: TradingProject):
        self.project = project
        if (not project.etrade_consumer_key
                or not project.etrade_consumer_secret):
            raise BrokerNotConfigured(
                "ETrade developer credentials missing on this project. "
                "Add the Consumer Key + Consumer Secret in project settings."
            )
        if (not project.etrade_access_token
                or not project.etrade_access_token_secret):
            raise BrokerNotConfigured(
                "ETrade is connected but the user hasn't completed the "
                "OAuth authorization step yet. Visit /etrade/connect."
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

    def _require_account(self) -> str:
        """Lazy-resolve the accountIdKey when the project DB row is
        empty. ETrade's OAuth flow ends with tokens but no account
        selected — historically the user had to re-visit /etrade/connect
        to pick one. Now we fetch /v1/accounts/list ourselves, pick the
        first account, and persist it back to the project row. The
        user trades immediately; the audit event explains the choice.
        """
        if self._account_id_key:
            return self._account_id_key
        accounts = self.list_accounts()
        if not accounts:
            raise BrokerNotConfigured(
                "ETrade returned no accounts for this OAuth user. "
                "Re-run /etrade/connect or check the credentials."
            )
        chosen = accounts[0]
        key = (chosen.get("accountIdKey") or "").strip()
        if not key:
            raise BrokerNotConfigured(
                "ETrade /accounts/list returned no accountIdKey on the "
                f"first row: {chosen}"
            )
        self._account_id_key = key
        # Persist back so subsequent ETradeClient instantiations skip
        # the lookup. We do this lazily and log an audit event so the
        # change is reviewable, but we don't fail the call if either
        # the DB write or the audit log fails — the in-memory key is
        # what matters for the current request.
        try:
            from db.repositories import ProjectsRepo, EventsRepo
            ProjectsRepo.update_etrade_tokens(
                self.project.project_id,
                access_token=self.project.etrade_access_token,
                access_token_secret=self.project.etrade_access_token_secret,
                account_id_key=key,
            )
            EventsRepo.log(
                self.project.project_id, "Manual", "DB_OVERRIDE",
                {
                    "action": "etrade_account_auto_selected",
                    "account_id_key": key,
                    "account_id": chosen.get("accountId"),
                    "candidates": len(accounts),
                    "narrative": [
                        "ETrade /accounts/list returned %d account(s); "
                        "auto-selected the first one. To switch, run "
                        "/etrade/connect again." % len(accounts),
                    ],
                },
            )
        except Exception:
            logger.exception("failed to persist auto-selected ETrade "
                             "accountIdKey")
        return self._account_id_key

    def list_accounts(self) -> list[dict[str, Any]]:
        """GET /v1/accounts/list — every brokerage account this OAuth
        user has access to. Used by the connect flow + the lazy
        accountIdKey resolver above."""
        try:
            r = self._session.get(
                self._url("/v1/accounts/list.json"),
                headers=_JSON_HEADERS, timeout=20,
            )
        except Exception as e:
            logger.warning("etrade /accounts/list failed: %s", e)
            return []
        if r.status_code != 200:
            logger.warning("etrade /accounts/list HTTP %s: %s",
                           r.status_code, r.text[:200])
            return []
        try:
            body = r.json()
        except Exception:
            return []
        ar = _unwrap(body, "AccountListResponse")
        acc_wrap = ar.get("Accounts") or ar.get("accounts") or {}
        accounts = (acc_wrap.get("Account") or acc_wrap.get("account")
                    or [])
        if isinstance(accounts, dict):
            accounts = [accounts]
        # Drop closed accounts — they show with accountStatus='CLOSED'.
        return [
            a for a in accounts
            if str(a.get("accountStatus", "ACTIVE")).upper() != "CLOSED"
        ]

    # ---------------- Account / positions --------------------------------

    def get_account(self) -> dict[str, Any]:
        """Map balance.json -> {cash, equity, buying_power,
        options_buying_power, portfolio_value}.

        ETrade nests money fields under ``Computed`` /
        ``ComputedBalance`` (the wrapper name varies between accounts).
        There is no top-level options_buying_power — we map to
        cashBuyingPower for cash accounts and marginBuyingPower for
        margin. ``portfolio_value`` is realTimeValues.totalAccountValue
        when ``realTimeNAV=true`` is requested, else regtEquity is the
        closest stand-in.
        """
        aid = self._require_account()
        try:
            r = self._session.get(
                self._url(f"/v1/accounts/{aid}/balance.json"),
                params={"instType": "BROKERAGE", "realTimeNAV": "true"},
                headers=_JSON_HEADERS,
                timeout=20,
            )
        except Exception as e:
            return {"error": f"network error: {e}"}
        if r.status_code != 200:
            return {"error": f"HTTP {r.status_code}", "body": r.text[:500]}
        try:
            body = r.json()
        except Exception:
            return {"error": "non-JSON response", "body": r.text[:500]}
        bal = _unwrap(body, "BalanceResponse")
        # Sandbox / live both expose Computed; some samples use ComputedBalance.
        comp = (bal.get("Computed") or bal.get("ComputedBalance")
                or bal.get("computed") or {})
        rtv = (comp.get("RealTimeValues") or comp.get("realTimeValues")
               or {})
        account_type = (
            bal.get("accountType") or bal.get("AccountType") or ""
        ).upper()
        # Pick the right buying-power flavor for an OPTIONS-trading
        # account. ETrade splits "cash" and "margin" buying-power
        # explicitly; the wheel needs the larger of the two for
        # writing CSPs (collateralized cash effectively).
        cash_bp = _as_float(comp.get("cashBuyingPower"))
        margin_bp = _as_float(comp.get("marginBuyingPower"))
        options_bp = margin_bp if account_type == "MARGIN" else cash_bp
        return {
            "cash": _as_float(comp.get("cashAvailableForInvestment")
                              or comp.get("netCash")
                              or comp.get("cashBalance")),
            "equity": _as_float(rtv.get("totalAccountValue")
                                or comp.get("regtEquity")),
            "buying_power": max(cash_bp, margin_bp),
            "options_buying_power": options_bp,
            "portfolio_value": _as_float(rtv.get("totalAccountValue")
                                          or comp.get("accountBalance")),
            "_raw_account_type": account_type,
        }

    def get_account_raw(self) -> dict[str, Any]:
        """Diagnostic dump — return the raw ETrade JSON."""
        if not self._account_id_key:
            return {"error": "no account selected"}
        try:
            r = self._session.get(
                self._url(
                    f"/v1/accounts/{self._account_id_key}/balance.json"),
                params={"instType": "BROKERAGE", "realTimeNAV": "true"},
                headers=_JSON_HEADERS, timeout=20,
            )
        except Exception as e:
            return {"error": str(e)}
        if r.status_code != 200:
            return {"error": f"HTTP {r.status_code}", "body": r.text[:500]}
        try:
            return r.json()
        except Exception:
            return {"error": "non-JSON response", "body": r.text[:500]}

    def list_positions(self) -> list[dict[str, Any]]:
        """Map portfolio.json positions to the standard shape used by the
        rest of the codebase. Options vs equities are split via
        Product.securityType; shorts come through with negative qty."""
        aid = self._require_account()
        try:
            r = self._session.get(
                self._url(f"/v1/accounts/{aid}/portfolio.json"),
                params={"count": 250},
                headers=_JSON_HEADERS, timeout=30,
            )
        except Exception as e:
            logger.warning("etrade portfolio fetch failed: %s", e)
            return []
        if r.status_code == 204:
            return []  # ETrade returns 204 for empty portfolios
        if r.status_code != 200:
            logger.warning("etrade portfolio HTTP %s: %s",
                           r.status_code, r.text[:200])
            return []
        try:
            body = r.json()
        except Exception:
            return []
        ap = _unwrap(body, "PortfolioResponse")
        ap = _unwrap(ap, "AccountPortfolio")
        # The portfolio response often nests AccountPortfolio under an
        # array because ETrade lets one user have multiple accounts in
        # the same response. We're already scoped to one accountIdKey
        # so just flatten.
        if isinstance(ap, list):
            ap = ap[0] if ap else {}
        positions = ap.get("Position") or ap.get("position") or []
        if isinstance(positions, dict):
            positions = [positions]
        out: list[dict[str, Any]] = []
        for p in positions:
            product = p.get("Product") or p.get("product") or {}
            sec_type = (product.get("securityType") or "").upper()
            qty = _as_float(p.get("quantity"))
            position_type = (p.get("positionType") or "LONG").upper()
            # Shorts MAY come back as positive qty with positionType=SHORT
            # OR as negative qty — normalize to "negative qty for short"
            # which is what the rest of the codebase expects.
            if position_type == "SHORT" and qty > 0:
                qty = -qty
            if sec_type == "OPTN":
                asset_class = "us_option"
                # Symbol on options: prefer osiKey (OCC-style), fall back
                # to rebuilding from the decomposed fields.
                osi = (p.get("osiKey") or p.get("OsiKey") or "")
                if osi:
                    sym = osi.upper()
                else:
                    root = product.get("symbol") or ""
                    try:
                        sym = _etrade_to_occ(
                            root,
                            int(product.get("expiryYear") or 0),
                            int(product.get("expiryMonth") or 0),
                            int(product.get("expiryDay") or 0),
                            product.get("callPut") or "PUT",
                            _as_float(product.get("strikePrice")),
                        )
                    except Exception:
                        sym = root
            else:
                asset_class = "us_equity"
                sym = product.get("symbol") or ""
            avg = _as_float(p.get("pricePaid")
                            or p.get("averagePrice")
                            or p.get("costPerShare"))
            mv = _as_float(p.get("marketValue") or p.get("marketValuee"))
            last = _as_float(p.get("lastTrade") or p.get("lastPrice")
                             or p.get("Quick", {}).get("lastTrade"))
            upl = _as_float(p.get("totalGain")
                            or p.get("totalGainPct"))
            out.append({
                "symbol": str(sym).upper(),
                "qty": qty,
                "asset_class": asset_class,
                "avg_entry_price": avg,
                "current_price": last,
                "market_value": mv,
                "unrealized_pl": upl,
                "side": "short" if qty < 0 else "long",
                "_etrade_raw_position_type": position_type,
            })
        return out

    def liquidate_position(self, symbol: str) -> dict[str, Any]:
        """Close a stock position at market by submitting an opposing
        market order. Options must be closed via submit_limit_option
        with BUY_CLOSE / SELL_CLOSE — this wheel pipeline only calls
        liquidate_position() on equity stop-loss exits."""
        # Find the live position to know which side we're closing.
        for p in self.list_positions():
            if (p["asset_class"] == "us_equity"
                    and p["symbol"].upper() == symbol.upper()
                    and abs(p["qty"]) > 0):
                target = p
                break
        else:
            return {"error": f"no live equity position for {symbol}"}
        qty = int(abs(target["qty"]))
        side = "SELL" if target["qty"] > 0 else "BUY"
        return self._submit_order(
            order_type="EQ",
            product={"securityType": "EQ", "symbol": symbol.upper()},
            order_action=side,
            qty=qty,
            price_type="MARKET",
            limit_price=None,
            time_in_force="GOOD_FOR_DAY",
        )

    # ---------------- Market data ----------------------------------------

    def snapshots(self, symbols: Iterable[str]) -> dict[str, Any]:
        """Batch-quote up to 25 symbols per call. Returns a dict keyed
        by symbol with attribute-style access (Scanner reads
        snap.last_price etc) so we return a small ``_Snapshot`` object
        per match."""
        syms = [str(s).upper() for s in symbols if s]
        if not syms:
            return {}
        out: dict[str, Any] = {}
        # ETrade hard caps at 25 symbols per request (50 with
        # overrideSymbolCount=true). We chunk in 25s.
        for i in range(0, len(syms), 25):
            chunk = syms[i:i + 25]
            joined = ",".join(chunk)
            try:
                r = self._session.get(
                    self._url(f"/v1/market/quote/{joined}.json"),
                    params={"detailFlag": "INTRADAY"},
                    headers=_JSON_HEADERS, timeout=20,
                )
            except Exception as e:
                logger.warning("etrade snapshots batch failed: %s", e)
                continue
            if r.status_code != 200:
                logger.warning("etrade snapshots HTTP %s for %s: %s",
                               r.status_code, joined[:60], r.text[:200])
                continue
            try:
                body = r.json()
            except Exception:
                continue
            qr = _unwrap(body, "QuoteResponse")
            qdata = qr.get("QuoteData") or []
            if isinstance(qdata, dict):
                qdata = [qdata]
            for q in qdata:
                product = q.get("Product") or {}
                sym = (product.get("symbol")
                       or q.get("symbol") or "").upper()
                if not sym:
                    continue
                detail = (q.get("Intraday") or q.get("All")
                          or q.get("intraday") or q.get("all") or {})
                last = _as_float(detail.get("lastTrade")
                                 or q.get("lastTrade"))
                change_pct = _as_float(
                    detail.get("changeClosePercentage")
                    or q.get("changeClosePercentage")
                )
                change = _as_float(detail.get("changeClose")
                                   or q.get("changeClose"))
                volume = _as_int(detail.get("totalVolume")
                                 or q.get("totalVolume"))
                prev_close = (last - change) if last and change else last
                out[sym] = _Snapshot(
                    symbol=sym, last_price=last,
                    prev_close=prev_close, volume=volume,
                    pct_change=change_pct,
                )
        return out

    def daily_bars(self, symbol: str,
                   lookback_days: int = 5) -> list[dict[str, Any]]:
        """ETrade has no native daily-OHLCV endpoint. Fall back to a
        single synthetic bar built from the live quote so the
        IV-rank / volatility filters don't NaN out the entire
        watchlist. For real historical bars the Strategist should
        pipe in yfinance (already a dependency) — out of scope here."""
        snaps = self.snapshots([symbol])
        snap = snaps.get(symbol.upper())
        if not snap:
            return []
        c = snap.last_price
        return [{
            "o": c, "h": c, "l": c, "c": c,
            "v": snap.volume,
            "t": date.today(),
        }]

    def active_us_equities(self,
                           limit: int | None = None) -> list[str]:
        """ETrade has no equity-listing endpoint. Callers fall back to
        ProjectSettings.watchlist (the Scanner already does this when
        the broker returns an empty list)."""
        return []

    # ---------------- Options --------------------------------------------

    def _option_chain_raw(self, underlying: str, contract_type: str,
                          expiration: date | None = None,
                          ) -> dict[str, Any]:
        params: dict[str, Any] = {"symbol": underlying.upper()}
        # chainType: CALL | PUT | CALLPUT (default CALLPUT)
        ct = (contract_type or "").lower()
        if ct.startswith("p"):
            params["chainType"] = "PUT"
        elif ct.startswith("c"):
            params["chainType"] = "CALL"
        else:
            params["chainType"] = "CALLPUT"
        if expiration is not None:
            params["expiryYear"] = expiration.year
            params["expiryMonth"] = expiration.month
            params["expiryDay"] = expiration.day
        try:
            r = self._session.get(
                self._url("/v1/market/optionchains.json"),
                params=params,
                headers=_JSON_HEADERS, timeout=30,
            )
        except Exception as e:
            logger.warning("etrade optionchains failed: %s", e)
            return {}
        if r.status_code != 200:
            logger.warning("etrade optionchains HTTP %s: %s",
                           r.status_code, r.text[:200])
            return {}
        try:
            return r.json()
        except Exception:
            return {}

    def list_option_contracts(self, underlying: str, contract_type: str,
                              min_dte: int, max_dte: int,
                              min_strike: float | None = None,
                              max_strike: float | None = None,
                              limit: int = 200) -> list[dict[str, Any]]:
        """Walk OptionChainResponse → filter by DTE/strike. Returns one
        dict per contract with the keys the Strategist expects:
        symbol, strike, expiration (date), open_interest."""
        body = self._option_chain_raw(underlying, contract_type)
        ocr = _unwrap(body, "OptionChainResponse")
        pairs = ocr.get("OptionPair") or ocr.get("optionPair") or []
        if isinstance(pairs, dict):
            pairs = [pairs]
        today = date.today()
        is_put = contract_type.lower().startswith("p")
        out: list[dict[str, Any]] = []
        for pair in pairs:
            leg = (pair.get("Put") if is_put else pair.get("Call")) \
                or pair.get(contract_type.title()) or {}
            if not leg:
                continue
            try:
                strike = _as_float(leg.get("strikePrice"))
                exp_y = _as_int(leg.get("expiryYear")
                                or leg.get("Product", {}).get("expiryYear"))
                exp_m = _as_int(leg.get("expiryMonth")
                                or leg.get("Product", {}).get("expiryMonth"))
                exp_d = _as_int(leg.get("expiryDay")
                                or leg.get("Product", {}).get("expiryDay"))
                if not (exp_y and exp_m and exp_d):
                    continue
                exp = date(exp_y, exp_m, exp_d)
            except Exception:
                continue
            dte = (exp - today).days
            if dte < min_dte or dte > max_dte:
                continue
            if min_strike is not None and strike < min_strike:
                continue
            if max_strike is not None and strike > max_strike:
                continue
            # osiKey is ETrade's OCC-style identifier and what the rest
            # of the wheel pipeline expects.
            osi = (leg.get("osiKey") or leg.get("OsiKey") or "").upper()
            if not osi:
                # Fall back to building it from decomposed fields.
                try:
                    osi = _etrade_to_occ(
                        underlying.upper(), exp_y, exp_m, exp_d,
                        "P" if is_put else "C", strike,
                    )
                except Exception:
                    continue
            out.append({
                "symbol": osi,
                "strike": strike,
                "expiration": exp,
                "open_interest": _as_int(leg.get("openInterest")),
            })
            if len(out) >= limit:
                break
        return out

    def option_chain_quotes(self, underlying: str,
                            expiration: date | None = None
                            ) -> dict[str, Any]:
        """Return live quotes keyed by OCC symbol. Greeks are nested
        under the ``OptionGreeks`` sub-object in ETrade responses
        (not flat) — fish them out and re-emit at the top level so the
        Strategist's _select_contract works unchanged."""
        out: dict[str, Any] = {}
        for ct in ("CALL", "PUT"):
            body = self._option_chain_raw(underlying, ct, expiration)
            ocr = _unwrap(body, "OptionChainResponse")
            pairs = ocr.get("OptionPair") or ocr.get("optionPair") or []
            if isinstance(pairs, dict):
                pairs = [pairs]
            for pair in pairs:
                leg = (pair.get("Call") if ct == "CALL"
                       else pair.get("Put")) or {}
                if not leg:
                    continue
                osi = (leg.get("osiKey") or leg.get("OsiKey") or "")
                if not osi:
                    continue
                greeks = (leg.get("OptionGreeks") or leg.get("optionGreek")
                          or leg.get("optionGreeks") or {})
                out[osi.upper()] = {
                    "bid": _as_float(leg.get("bid")),
                    "ask": _as_float(leg.get("ask")),
                    "last": _as_float(leg.get("lastPrice")),
                    "volume": _as_int(leg.get("volume")),
                    "open_interest": _as_int(leg.get("openInterest")),
                    "delta": _as_float(greeks.get("delta")),
                    "gamma": _as_float(greeks.get("gamma")),
                    "theta": _as_float(greeks.get("theta")),
                    "vega": _as_float(greeks.get("vega")),
                    "iv": _as_float(greeks.get("iv")),
                }
        return out

    def submit_market_equity(self, symbol: str, qty: int, side: str,
                             time_in_force: str = "day",
                             extended_hours: bool = False
                             ) -> dict[str, Any]:
        """Submit a market equity order. ETrade exposes extended-hours
        trading via the ``marketSession`` field on the Order rather
        than a top-level flag, so we map the Alpaca-style
        ``extended_hours`` kwarg to that. ``side`` is the wheel's
        broker-agnostic word ('buy' / 'sell'); ETrade equity orders
        use plain BUY / SELL (no open/close variants).
        """
        s = (side or "").lower()
        if s == "buy":
            order_action = "BUY"
        elif s == "sell":
            order_action = "SELL"
        else:
            return {"error": f"unknown side {side!r}"}
        product = {
            "securityType": "EQ",
            "symbol": symbol.upper(),
        }
        return self._submit_order(
            order_type="EQ",
            product=product,
            order_action=order_action,
            qty=int(qty),
            price_type="MARKET",
            limit_price=None,
            time_in_force=("GOOD_UNTIL_CANCEL"
                           if str(time_in_force).lower().startswith("g")
                           else "GOOD_FOR_DAY"),
            market_session=("EXTENDED" if extended_hours else "REGULAR"),
        )

    def submit_limit_option(self, option_symbol: str, qty: int, side: str,
                            limit_price: float,
                            time_in_force: str = "day") -> dict[str, Any]:
        """ETrade preview-then-place. ``side`` is the wheel's
        broker-agnostic word ('buy' / 'sell'); we map it to the more
        specific ETrade orderAction. The wheel uses this for:

          * Sell-to-open CSPs / CCs       -> SELL_OPEN
          * Buy-to-close an open short    -> BUY_CLOSE

        We can't tell which open/close direction the caller wants from
        ``side`` alone, so we infer:  side='sell' opens a new short
        (SELL_OPEN — what the Strategist always does), side='buy'
        closes an existing short (BUY_CLOSE — what /contracts/{cid}/
        close and /roll always do).
        """
        # Decompose the OCC symbol into ETrade Product fields.
        m = _OCC_RE.match(option_symbol.upper().strip())
        if not m:
            return {"error": f"unparseable OCC symbol: {option_symbol}"}
        root, yymmdd, cp, strike_thousandths = m.groups()
        year = 2000 + int(yymmdd[:2])
        month = int(yymmdd[2:4])
        day = int(yymmdd[4:6])
        call_put = "CALL" if cp == "C" else "PUT"
        strike = int(strike_thousandths) / 1000.0
        s = (side or "").lower()
        if s == "sell":
            order_action = "SELL_OPEN"
        elif s == "buy":
            order_action = "BUY_CLOSE"
        else:
            return {"error": f"unknown side {side!r}"}
        product = {
            "securityType": "OPTN",
            "symbol": root,
            "callPut": call_put,
            "expiryYear": year,
            "expiryMonth": month,
            "expiryDay": day,
            "strikePrice": f"{strike:.6f}",
        }
        return self._submit_order(
            order_type="OPTN",
            product=product,
            order_action=order_action,
            qty=int(qty),
            price_type="LIMIT",
            limit_price=float(limit_price),
            time_in_force=("GOOD_UNTIL_CANCEL"
                           if str(time_in_force).lower().startswith("g")
                           else "GOOD_FOR_DAY"),
        )

    def _submit_order(self, *, order_type: str, product: dict[str, Any],
                      order_action: str, qty: int,
                      price_type: str, limit_price: float | None,
                      time_in_force: str,
                      market_session: str = "REGULAR"
                      ) -> dict[str, Any]:
        """Preview-then-place. ``previewId`` expires in 3 minutes so
        we issue the place call back-to-back. ``clientOrderId`` must
        be unique per account and <=20 chars — we use the first
        20 hex chars of a fresh uuid4."""
        aid = self._require_account()
        client_order_id = uuid.uuid4().hex[:20]
        order_body: dict[str, Any] = {
            "allOrNone": False,
            "priceType": price_type,
            "orderTerm": time_in_force,
            "marketSession": market_session,
            "Instrument": [{
                "Product": product,
                "orderAction": order_action,
                "quantityType": "QUANTITY",
                "quantity": int(qty),
            }],
        }
        if price_type == "LIMIT" and limit_price is not None:
            order_body["limitPrice"] = round(float(limit_price), 2)
        preview_envelope = {
            "PreviewOrderRequest": {
                "orderType": order_type,
                "clientOrderId": client_order_id,
                "Order": [order_body],
            }
        }
        try:
            pr = self._session.post(
                self._url(f"/v1/accounts/{aid}/orders/preview.json"),
                data=json.dumps(preview_envelope),
                headers=_JSON_POST_HEADERS, timeout=20,
            )
        except Exception as e:
            return {"error": f"preview network error: {e}"}
        if pr.status_code != 200:
            return {"error": f"preview HTTP {pr.status_code}",
                    "body": pr.text[:500]}
        try:
            preview_body = pr.json()
        except Exception:
            return {"error": "preview returned non-JSON",
                    "body": pr.text[:500]}
        preview_resp = _unwrap(preview_body, "PreviewOrderResponse")
        previews = (preview_resp.get("PreviewIds")
                    or preview_resp.get("previewIds") or [])
        if isinstance(previews, dict):
            previews = [previews]
        if not previews:
            return {"error": "preview returned no previewId",
                    "preview": preview_body}
        preview_id = previews[0].get("previewId")
        cash_margin = previews[0].get("cashMargin", "CASH")
        place_envelope = {
            "PlaceOrderRequest": {
                "orderType": order_type,
                "clientOrderId": client_order_id,
                "PreviewIds": [{"previewId": preview_id,
                                "cashMargin": cash_margin}],
                "Order": [order_body],
            }
        }
        try:
            plr = self._session.post(
                self._url(f"/v1/accounts/{aid}/orders/place.json"),
                data=json.dumps(place_envelope),
                headers=_JSON_POST_HEADERS, timeout=20,
            )
        except Exception as e:
            return {"error": f"place network error: {e}",
                    "preview_id": preview_id}
        if plr.status_code != 200:
            return {"error": f"place HTTP {plr.status_code}",
                    "body": plr.text[:500],
                    "preview_id": preview_id}
        try:
            place_body = plr.json()
        except Exception:
            return {"error": "place returned non-JSON",
                    "body": plr.text[:500]}
        place_resp = _unwrap(place_body, "PlaceOrderResponse")
        orders = (place_resp.get("OrderIds") or place_resp.get("orderIds")
                  or place_resp.get("Order") or [])
        if isinstance(orders, dict):
            orders = [orders]
        order_id = ""
        for o in orders:
            if isinstance(o, dict):
                order_id = (o.get("orderId") or o.get("OrderId")
                            or order_id)
                if order_id:
                    break
        # Map to the shape the executor / orders_tracker expect.
        sym = product.get("symbol", "")
        if product.get("securityType") == "OPTN":
            try:
                sym = _etrade_to_occ(
                    sym,
                    int(product.get("expiryYear") or 0),
                    int(product.get("expiryMonth") or 0),
                    int(product.get("expiryDay") or 0),
                    product.get("callPut") or "PUT",
                    _as_float(product.get("strikePrice")),
                )
            except Exception:
                pass
        return {
            "id": str(order_id),
            "symbol": sym,
            "qty": float(qty),
            "side": order_action.split("_")[0].lower(),
            "type": price_type.lower(),
            "limit_price": (round(float(limit_price), 2)
                            if limit_price is not None else None),
            "status": "accepted",
            "submitted_at": datetime.now(tz=timezone.utc),
            "_etrade_preview_id": preview_id,
            "_etrade_order_action": order_action,
        }

    # ---------------- Market schedule ------------------------------------

    def get_clock(self) -> dict[str, Any]:
        """ETrade doesn't expose a market-clock endpoint. Permissive
        approximation: open during US RTH on weekdays. Holidays
        are NOT detected here — the runner's deeper checks
        cross-reference Alpaca's calendar separately."""
        # Use real US/Eastern (DST-aware) instead of the fixed -4
        # offset from Phase 1.
        try:
            from zoneinfo import ZoneInfo
            et = datetime.now(tz=ZoneInfo("America/New_York"))
        except Exception:
            et = datetime.now(tz=timezone.utc) + timedelta(hours=-4)
        is_weekday = et.weekday() < 5
        is_rth = (is_weekday
                  and time(9, 30) <= et.time() <= time(16, 0))
        return {
            "is_open": is_rth,
            "next_open": None,
            "next_close": None,
            "note": "approximate (no holidays)",
        }

    def get_market_clock(self) -> dict[str, Any]:
        """Alpaca client exposes both get_clock() and get_market_clock().
        Mirror that here so any call site that imported the Alpaca
        symbol works against ETrade unchanged."""
        return self.get_clock()

    def get_calendar(self, days: int = 7) -> list[dict[str, Any]]:
        return []


# ---------------------------------------------------------------------------
# Snapshot value object (matches the shape of Alpaca's snapshot so the
# Scanner / Strategist can read ``snap.last_price`` etc. interchangeably).
# ---------------------------------------------------------------------------

class _Snapshot:
    __slots__ = ("symbol", "last_price", "prev_close",
                 "volume", "pct_change")

    def __init__(self, *, symbol: str, last_price: float,
                 prev_close: float, volume: int, pct_change: float):
        self.symbol = symbol
        self.last_price = last_price
        self.prev_close = prev_close
        self.volume = volume
        self.pct_change = pct_change
