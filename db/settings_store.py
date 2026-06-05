"""Database-backed settings store.

All runtime parameters live in `app_settings` (global) or `project_settings`
(per-tenant). Application code reads them through `AppSettings.get(...)` or
`ProjectSettings.get(project_id, key, default)` so that nothing is hardcoded.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text

from .connection import session_scope


def _coerce(value: str | None, value_type: str) -> Any:
    if value is None or value == "":
        return None
    vt = value_type.lower()
    if vt == "int":
        return int(value)
    if vt == "float":
        return float(value)
    if vt == "bool":
        return value.strip().lower() in ("1", "true", "yes", "on")
    if vt == "json":
        return json.loads(value)
    return value


def _serialize(value: Any, value_type: str) -> str | None:
    if value is None:
        return None
    if value_type == "json":
        return json.dumps(value)
    if value_type == "bool":
        return "true" if bool(value) else "false"
    return str(value)


# ---------- Secret encryption --------------------------------------------------

def _get_fernet():
    key = os.getenv("SECRET_ENCRYPTION_KEY", "")
    if not key:
        return None
    try:
        from cryptography.fernet import Fernet
        return Fernet(key.encode() if isinstance(key, str) else key)
    except Exception:
        return None


def _encrypt(value: str) -> str:
    if value is None or value == "":
        return value
    fernet = _get_fernet()
    if fernet is None:
        return value  # dev mode - plaintext
    return "enc::" + fernet.encrypt(value.encode()).decode()


def _decrypt(value: str | None) -> str | None:
    if not value:
        return value
    if isinstance(value, str) and value.startswith("enc::"):
        fernet = _get_fernet()
        if fernet is None:
            return ""
        try:
            return fernet.decrypt(value[5:].encode()).decode()
        except Exception:
            # The value was encrypted with a DIFFERENT Fernet key than the
            # one currently in SECRET_ENCRYPTION_KEY. Returning a sentinel
            # so the UI can show "needs reset" rather than 500'ing the
            # whole settings page.
            return "<<decrypt failed — re-enter this value>>"
    return value


# ---------- Global settings ---------------------------------------------------

@dataclass
class SettingRow:
    key: str
    value: Any
    value_type: str
    category: str
    description: str | None
    is_secret: bool


class AppSettings:
    """Global key/value settings."""

    @staticmethod
    def get(key: str, default: Any = None) -> Any:
        with session_scope() as s:
            row = s.execute(
                text("SELECT setting_value, value_type, is_secret FROM app_settings WHERE setting_key = :k"),
                {"k": key},
            ).fetchone()
        if row is None:
            return default
        raw = _decrypt(row[0]) if row[2] else row[0]
        coerced = _coerce(raw, row[1])
        return default if coerced is None else coerced

    @staticmethod
    def set(key: str, value: Any, value_type: str | None = None, category: str = "general",
            description: str | None = None, is_secret: bool = False) -> None:
        serialized = _serialize(value, value_type or "string")
        if is_secret and serialized:
            serialized = _encrypt(serialized)
        with session_scope() as s:
            existing = s.execute(
                text("SELECT setting_key FROM app_settings WHERE setting_key = :k"),
                {"k": key},
            ).fetchone()
            if existing:
                s.execute(
                    text("""UPDATE app_settings
                            SET setting_value = :v,
                                value_type = COALESCE(:vt, value_type),
                                category = COALESCE(:c, category),
                                description = COALESCE(:d, description),
                                is_secret = :s,
                                updated_at = UTC_TIMESTAMP(6)
                            WHERE setting_key = :k"""),
                    {"k": key, "v": serialized, "vt": value_type, "c": category,
                     "d": description, "s": 1 if is_secret else 0},
                )
            else:
                s.execute(
                    text("""INSERT INTO app_settings
                            (setting_key, setting_value, value_type, category, description, is_secret)
                            VALUES (:k, :v, :vt, :c, :d, :s)"""),
                    {"k": key, "v": serialized, "vt": value_type or "string", "c": category,
                     "d": description, "s": 1 if is_secret else 0},
                )
            s.commit()

    @staticmethod
    def list_all() -> list[SettingRow]:
        with session_scope() as s:
            rows = s.execute(
                text("""SELECT setting_key, setting_value, value_type, category, description, is_secret
                        FROM app_settings ORDER BY category, setting_key""")
            ).fetchall()
        out: list[SettingRow] = []
        for r in rows:
            value_raw = _decrypt(r[1]) if r[5] else r[1]
            out.append(SettingRow(
                key=r[0],
                value=_coerce(value_raw, r[2]),
                value_type=r[2],
                category=r[3],
                description=r[4],
                is_secret=bool(r[5]),
            ))
        return out


# ---------- Per-project settings ----------------------------------------------

class ProjectSettings:
    """Per-tenant key/value settings.

    Defaults are owned here instead of inline in agent code, so any tenant can
    override any strategy/risk parameter without editing source.
    """

    DEFAULTS: dict[str, tuple[Any, str, str]] = {
        # key: (default_value, value_type, description)
        "stop_loss_dollars":             (2.00,    "float",  "Absolute dollar drop from entry that triggers stock liquidation"),
        "watchlist":                     ("",      "string", "Comma-separated tickers the scanner & outlook page consider (blank = built-in default universe)"),
        "volume_threshold":              (2_000_000, "int", "Minimum average daily share volume for a candidate"),
        "scanner_top_n":                 (10,      "int",    "How many top movers to consider per cycle"),
        "scanner_min_price":             (5.00,    "float",  "Minimum share price for scan candidates"),
        "scanner_max_price":             (500.00,  "float",  "Maximum share price for scan candidates"),
        "scanner_min_pct_change":        (1.5,     "float",  "Minimum percent change (abs) from previous close"),
        "csp_delta_min":                 (0.15,    "float",  "Lower bound of target delta range for cash-secured puts"),
        "csp_delta_max":                 (0.30,    "float",  "Upper bound of target delta range for cash-secured puts"),
        "csp_min_dte":                   (7,       "int",    "Minimum days-to-expiration for sold puts"),
        "csp_max_dte":                   (45,      "int",    "Maximum days-to-expiration for sold puts"),
        "cc_delta_min":                  (0.20,    "float",  "Lower bound of target delta for covered calls"),
        "cc_delta_max":                  (0.35,    "float",  "Upper bound of target delta for covered calls"),
        "cc_min_premium_ratio":          (0.005,   "float",  "Minimum premium / strike ratio for covered calls"),
        "max_open_positions":            (5,       "int",    "Maximum simultaneous open stock positions"),
        "max_open_contracts":            (10,      "int",    "Maximum simultaneous open option contracts"),
        "max_collateral_pct":            (0.80,    "float",  "Max fraction of buying power consumable by option collateral"),
        "max_concentration_per_ticker":  (0.25,    "float",  "Maximum fraction of buying power any single underlying may consume"),
        "avoid_earnings_within_dte":     (3,       "int",    "Skip tickers with earnings within N days (0 = disabled)"),
        "contracts_per_csp":             (1,       "int",    "How many CSP contracts to sell per ticker per cycle (income multiplier)"),
        "max_contracts_per_ticker":      (5,       "int",    "Cap on contracts of the same option symbol opened at once"),
        "cc_pyramid_levels":             (1,       "int",    "After assignment, split CCs across N staggered strikes (1=no pyramiding)"),
        "cc_pyramid_spacing_pct":        (0.03,    "float",  "Strike spacing between pyramid levels (3% default)"),
        "take_profit_enabled":           (True,    "bool",   "Buy back short options once a target percent of max profit is reached"),
        "close_at_profit_pct":           (0.50,    "float",  "Target fraction of max profit at which to buy-to-close (0.50 = 50%)"),
        "auto_roll_enabled":             (True,    "bool",   "Automatically roll a contract approaching expiration"),
        "auto_roll_dte_threshold":       (2,       "int",    "Days-to-expiration that triggers an auto-roll attempt"),
        "min_iv_rank":                   (0.0,     "float",  "Skip tickers whose 1-year realized-vol rank is below this (0 = disabled)"),
        "news_sentiment_filter":         (True,    "bool",   "Skip tickers with strongly negative recent news sentiment (uses Alpaca News + VADER for instant scoring)"),
        "news_sentiment_min":            (-0.50,   "float",  "Block when the worst headline's VADER compound score is below this (-1..+1). Default -0.5 blocks on moderately-negative-or-worse headlines."),
        "skip_event_days_within":        (1,       "int",    "Block new option positions when a major economic event (FOMC/CPI/NFP) falls within this many calendar days. Default 1 = block only on T-1 and T (i.e. the day before and day of the release). 0 = disabled, 3 = very conservative."),
        "skip_on_fomc_days":             (True,    "bool",   "Apply the economic-event filter for FOMC rate decisions (binary vol)"),
        "skip_on_cpi_days":              (True,    "bool",   "Apply the economic-event filter for CPI release days (8:30am ET surprise)"),
        "skip_on_nfp_days":              (True,    "bool",   "Apply the economic-event filter for NFP / jobs report Fridays"),
        "skip_on_pce_days":              (False,   "bool",   "Apply the economic-event filter for PCE inflation. Off by default — smaller market impact than CPI/FOMC."),
        "recent_failure_skip_minutes":   (60,      "int",    "Skip any ticker whose most-recent order submission was rejected by the broker (insufficient BP, halted symbol, etc.) within this many minutes. 0 = always retry."),
        "quarantined_symbols":           ("",      "string", "Comma-separated tickers the Scanner and Strategist will NEVER consider for this project. Use for delisted/renamed symbols (e.g. SQ → XYZ), companies you don't want exposure to, or to back off a ticker after a bad fill. Case-insensitive."),
        "optimizer_auto_apply":          (False,   "bool",   "When True, the Optimizer Agent auto-applies LLM-recommended parameter changes that fit inside the safety rails (whitelisted keys + value clamps + max-step-per-cycle bounded by the project's trading_plan, max 2 changes per cycle). When False (default), recommendations stay pending for human review on the Intelligence page."),
        "trading_plan":                  ("balanced","string","Risk appetite for this project: 'conservative' (smaller positions, longer DTE, narrower delta band, Optimizer takes tiny steps), 'balanced' (default — middle ground), 'aggressive' (higher delta, shorter DTE, looser risk caps, Optimizer takes larger steps). The Optimizer Agent NEVER changes this value — you set the plan, and every other auto-tune respects it."),
        "optimizer_interval_minutes":    (0,        "int",    "Minutes between Optimizer Agent ticks for THIS project. 0 (default) inherits the global AppSettings value (which defaults to 30). Set per-project when you want one account on a faster/slower cadence than another. The Optimizer still only runs during ET 04:00-20:00 on trading days regardless."),
        "reconcile_interval_min":        (0,        "int",    "Minutes between the light DB-vs-broker reconciler ticks for THIS project. 0 (default) inherits the global AppSettings value (which defaults to 15). Lower this for high-activity projects where divergence costs money."),
        "loop_interval_seconds":         (60,      "int",    "Seconds between scan->execute cycles for this project (overrides global)"),
        "order_time_in_force":           ("day",   "string", "Default time-in-force for submitted orders"),
        "strategy_mode":                 ("wheel", "string", "What this project trades. WHEEL FAMILY: 'wheel' (CSP+CC options income — default), 'wheel_plus_dca' (options + scheduled stock buys), 'dca_only' (long-term stock accumulation, no options). MULTI-LEG SPREADS: 'bull_put_spread' (bullish credit), 'bear_call_spread' (bearish credit), 'bull_call_spread' (bullish debit), 'bear_put_spread' (bearish debit), 'iron_condor' (neutral, 4-leg), 'calendar_spread' (theta). DAY-TRADING: 'intraday_momentum' (0DTE/1DTE long calls or puts driven by intraday RSI/MACD/VWAP signals — requires allow_0dte or allow_1dte to be on). 'paused' = don't trade. All modes work on both Alpaca and ETrade."),
        "use_extended_hours":            (False,   "bool",   "Allow trades during extended hours"),
        "income_cadence":                ("custom","string", "Income cadence preset: weekly | biweekly | monthly | custom. When set to a preset, the strategist overrides csp_min_dte / csp_max_dte / csp_delta_min / csp_delta_max with the preset values, and auto-roll will roll any open contract whose remaining DTE drifts outside the preset band."),
        "dry_run":                       (True,    "bool",   "If true, skip order submission and only log decisions"),
        # --- Multi-leg spread tunables (used when strategy_mode is one of
        # the *_spread modes or iron_condor) --------------------------------
        "spread_target_delta":           (0.25,    "float",  "Target |delta| for the SHORT leg of a credit vertical / iron condor (e.g. 0.25 = ~25-delta short option). For debit spreads (bull_call / bear_put) this is the long-leg delta."),
        "spread_width":                  (5.0,     "float",  "Distance in dollars between the two legs of a vertical, or the wing width of an iron condor."),
        "spread_min_dte":                (21,      "int",    "Minimum days-to-expiration for spread setups."),
        "spread_max_dte":                (45,      "int",    "Maximum days-to-expiration for spread setups."),
        # --- Calendar-spread specific ---------------------------------------
        "calendar_option_type":          ("call",  "string", "Calendar spread leg type: 'call' or 'put'."),
        "calendar_short_dte":            (14,      "int",    "Target DTE for the calendar's short (near-month) leg."),
        "calendar_long_dte":             (45,      "int",    "Target DTE for the calendar's long (back-month) leg."),
        # --- Intraday / day-trading -----------------------------------------
        # Toggles that gate the intraday momentum strategist (only consulted
        # when strategy_mode='intraday_momentum').
        "intraday_scanner_enabled":      (False,   "bool",   "Master switch for the intraday RSI/MACD/VWAP scanner. Must be true (along with allow_0dte and/or allow_1dte) for the intraday_momentum strategy_mode to open any trades. The Optimize-Now button flips this to true automatically when you pick intraday_momentum."),
        "allow_0dte":                    (False,   "bool",   "Allow the day-trading strategist to buy 0DTE (same-day expiration) long calls/puts. HIGH variance — gamma is large, slippage is large, theta is brutal. Default off; opt in explicitly."),
        "allow_1dte":                    (False,   "bool",   "Allow the day-trading strategist to buy 1DTE (expires next trading day) long calls/puts. Lower variance than 0DTE but still day-trade territory. Default off."),
        "intraday_max_trades_per_cycle": (3,       "int",    "Hard cap on how many 0DTE/1DTE trades the strategist will open per cycle. Independent of the PDT 5-day window cap."),
        # --- Dynamic watchlist (market-aware refresh) -------------------
        "dynamic_watchlist_enabled":     (True,    "bool",   "When true, the watchlist auto-refreshes daily at US market open: stable tier-anchor names + today's top % movers + IV-rich names, all BP-fit and earnings-filtered. Set false to lock the watchlist to whatever you set manually."),
        "dynamic_watchlist_max_size":    (30,      "int",    "Cap on dynamic-watchlist size after all augmentation. Smaller = focused universe; larger = more candidates per scan."),
        "dynamic_watchlist_min_iv_rank": (0.30,    "float",  "Minimum IV-rank a momentum candidate must clear to be added to the watchlist's IV-rich augmentation layer. Lower = more inclusion."),
        "news_aware_watchlist":          (True,    "bool",   "When true, the dynamic watchlist scoring biases toward tickers with active news flow (free RSS feeds: MarketWatch, CNBC, Yahoo, Seeking Alpha, Reddit WSB). Each news mention adds 5% to a ticker's momentum score, capped at 50%. Set false to ignore news entirely."),
        "0dte_profit_target_pct":        (0.30,    "float",  "Per-trade profit target for 0DTE/1DTE long-option opens, as a fraction of entry price. 0.30 = exit when the option is up 30% from entry. Take-profit logic uses this to auto-close intraday winners."),
        "0dte_stop_loss_pct":            (0.50,    "float",  "Per-trade stop-loss for 0DTE/1DTE long-option opens, as a fraction of entry price. 0.50 = exit when the option is down 50% from entry."),
        "intraday_rsi_oversold":         (30,      "int",    "RSI threshold below which the intraday scanner flags an oversold (BUY-signal) condition. Lower = stricter, fewer but stronger signals. 14-period RSI; 30 is the textbook default."),
        "intraday_rsi_overbought":       (70,      "int",    "RSI threshold above which the scanner flags an overbought (SELL-signal) condition. Mirrors intraday_rsi_oversold."),
    }

    # Display grouping for the project settings panel. Each group has a
    # title, optional subtitle, and ORDERED list of setting keys it owns.
    # Keys not listed in any group fall through to a "Misc" group at
    # the bottom of the panel, so adding a new setting still renders
    # (just in the catch-all bucket until someone moves it).
    DISPLAY_GROUPS: list[dict[str, Any]] = [
        {
            "id":    "strategy",
            "title": "Strategy & Mode",
            "subtitle": "What this project trades and how it makes decisions.",
            "keys": [
                "strategy_mode", "trading_plan", "income_cadence",
                "dry_run",
            ],
        },
        {
            "id":    "watchlist",
            "title": "Scanner & Watchlist",
            "subtitle": "Universe of tickers + how the scanner picks candidates.",
            "keys": [
                "watchlist", "quarantined_symbols",
                "scanner_top_n", "scanner_min_price",
                "scanner_max_price", "scanner_min_pct_change",
                "volume_threshold",
                "dynamic_watchlist_enabled",
                "dynamic_watchlist_max_size",
                "dynamic_watchlist_min_iv_rank",
                "news_aware_watchlist",
            ],
        },
        {
            "id":    "wheel",
            "title": "Wheel (Cash-Secured Puts + Covered Calls)",
            "subtitle": "Used when strategy_mode is wheel or wheel_plus_dca.",
            "keys": [
                "csp_delta_min", "csp_delta_max",
                "csp_min_dte", "csp_max_dte",
                "cc_delta_min", "cc_delta_max",
                "cc_min_premium_ratio",
                "contracts_per_csp", "max_contracts_per_ticker",
                "cc_pyramid_levels", "cc_pyramid_spacing_pct",
            ],
        },
        {
            "id":    "spreads",
            "title": "Multi-leg Spreads",
            "subtitle": "Verticals, iron condor, calendar — used when strategy_mode is one of the *_spread modes.",
            "keys": [
                "spread_target_delta", "spread_width",
                "spread_min_dte", "spread_max_dte",
                "calendar_option_type",
                "calendar_short_dte", "calendar_long_dte",
            ],
        },
        {
            "id":    "intraday",
            "title": "Day-Trading / Intraday Momentum",
            "subtitle": "Used when strategy_mode is intraday_momentum. PDT rules enforced automatically.",
            "keys": [
                "intraday_scanner_enabled",
                "allow_0dte", "allow_1dte",
                "intraday_max_trades_per_cycle",
                "0dte_profit_target_pct", "0dte_stop_loss_pct",
                "intraday_rsi_oversold", "intraday_rsi_overbought",
            ],
        },
        {
            "id":    "risk",
            "title": "Risk & Position Caps",
            "subtitle": "Hard limits on capital exposure, concentration, and recovery from errors.",
            "keys": [
                "max_open_positions", "max_open_contracts",
                "max_collateral_pct",
                "max_concentration_per_ticker",
                "stop_loss_dollars",
                "min_iv_rank",
                "avoid_earnings_within_dte",
                "recent_failure_skip_minutes",
            ],
        },
        {
            "id":    "exit",
            "title": "Take-Profit & Auto-Roll",
            "subtitle": "How and when open positions close themselves.",
            "keys": [
                "take_profit_enabled", "close_at_profit_pct",
                "auto_roll_enabled", "auto_roll_dte_threshold",
            ],
        },
        {
            "id":    "news",
            "title": "News & Sentiment Filters",
            "subtitle": "Per-ticker filters using Alpaca News + VADER scoring.",
            "keys": [
                "news_sentiment_filter", "news_sentiment_min",
            ],
        },
        {
            "id":    "macro",
            "title": "Economic Event Gates",
            "subtitle": "Block new positions around binary-vol events like FOMC, CPI, NFP.",
            "keys": [
                "skip_event_days_within",
                "skip_on_fomc_days", "skip_on_cpi_days",
                "skip_on_nfp_days", "skip_on_pce_days",
            ],
        },
        {
            "id":    "optimizer",
            "title": "Optimizer Agent",
            "subtitle": "LLM-driven setting tuner that runs on its own cadence.",
            "keys": [
                "optimizer_auto_apply", "optimizer_interval_minutes",
            ],
        },
        {
            "id":    "execution",
            "title": "Cycle & Execution",
            "subtitle": "Scheduling, order routing, and broker behavior.",
            "keys": [
                "loop_interval_seconds", "reconcile_interval_min",
                "order_time_in_force", "use_extended_hours",
            ],
        },
    ]

    @classmethod
    def group_for_display(
        cls,
        settings: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Bucket a flat list of project settings into display groups
        (in order). Settings whose key isn't in any group land in a
        catch-all "Misc" group at the bottom — surfaces new settings
        the moment they exist rather than silently hiding them."""
        by_key = {row["key"]: row for row in settings}
        out: list[dict[str, Any]] = []
        used: set[str] = set()
        for group in cls.DISPLAY_GROUPS:
            items = []
            for key in group["keys"]:
                row = by_key.get(key)
                if row is None:
                    continue
                items.append(row)
                used.add(key)
            if items:
                out.append({
                    "id":       group["id"],
                    "title":    group["title"],
                    "subtitle": group.get("subtitle", ""),
                    "items":    items,
                })
        leftover = [r for r in settings if r["key"] not in used]
        if leftover:
            out.append({
                "id":       "misc",
                "title":    "Other Settings",
                "subtitle": ("Newer or less-categorized settings — "
                             "these still work, they just don't have "
                             "a home group yet."),
                "items":    leftover,
            })
        return out

    @classmethod
    def get(cls, project_id: str, key: str, default: Any = None) -> Any:
        with session_scope() as s:
            row = s.execute(
                text("""SELECT setting_value, value_type
                        FROM project_settings
                        WHERE project_id = :p AND setting_key = :k"""),
                {"p": project_id, "k": key},
            ).fetchone()
        if row is not None:
            coerced = _coerce(row[0], row[1])
            if coerced is not None:
                return coerced
        if key in cls.DEFAULTS:
            return cls.DEFAULTS[key][0]
        return default

    @classmethod
    def set(cls, project_id: str, key: str, value: Any, value_type: str | None = None) -> None:
        vt = value_type or (cls.DEFAULTS[key][1] if key in cls.DEFAULTS else "string")
        serialized = _serialize(value, vt)
        with session_scope() as s:
            existing = s.execute(
                text("""SELECT 1 FROM project_settings
                        WHERE project_id = :p AND setting_key = :k"""),
                {"p": project_id, "k": key},
            ).fetchone()
            if existing:
                s.execute(
                    text("""UPDATE project_settings
                            SET setting_value = :v, value_type = :vt, updated_at = UTC_TIMESTAMP(6)
                            WHERE project_id = :p AND setting_key = :k"""),
                    {"p": project_id, "k": key, "v": serialized, "vt": vt},
                )
            else:
                s.execute(
                    text("""INSERT INTO project_settings
                            (project_id, setting_key, setting_value, value_type)
                            VALUES (:p, :k, :v, :vt)"""),
                    {"p": project_id, "k": key, "v": serialized, "vt": vt},
                )
            s.commit()

    @classmethod
    def export_all(cls, project_id: str) -> dict[str, Any]:
        """Snapshot every setting for a project (overrides + defaults) into
        a JSON-serializable dict.

        Returned shape:
            {"schema_version": 1,
             "project_id": "<pid>",
             "exported_at": "<UTC ISO timestamp>",
             "settings": {key: {"value": v, "value_type": vt}, ...}}

        Includes BOTH explicitly-overridden settings and DEFAULTS so that
        importing into a fresh project produces an identical configuration
        even if the destination's DEFAULTS table drifts.
        """
        from datetime import datetime, timezone
        rows = cls.list_for_project(project_id)
        return {
            "schema_version": 1,
            "project_id": project_id,
            "exported_at": datetime.now(tz=timezone.utc).isoformat(),
            "settings": {
                r.key: {"value": r.value, "value_type": r.value_type}
                for r in rows
            },
        }

    @classmethod
    def import_bulk(cls, project_id: str,
                    payload: dict[str, Any], *,
                    overwrite: bool = True) -> dict[str, Any]:
        """Apply a previously-exported settings dict to ``project_id``.

        Accepts either the full export envelope (with ``settings`` key) or
        a bare ``{key: value | {value, value_type}}`` mapping for
        convenience. Unknown keys are skipped (logged) — typo-protection
        against hand-edited files.

        Returns ``{"applied": [...], "skipped": [...], "errors": [...]}``.
        """
        settings_in = payload.get("settings") if isinstance(payload, dict) and "settings" in payload else payload
        if not isinstance(settings_in, dict):
            return {"applied": [], "skipped": [],
                    "errors": ["payload missing 'settings' object"]}
        applied: list[str] = []
        skipped: list[str] = []
        errors: list[str] = []
        for key, raw in settings_in.items():
            if key not in cls.DEFAULTS:
                skipped.append(key)
                continue
            try:
                if isinstance(raw, dict) and "value" in raw:
                    value = raw["value"]
                    vt = raw.get("value_type") or cls.DEFAULTS[key][1]
                else:
                    value = raw
                    vt = cls.DEFAULTS[key][1]
                if not overwrite:
                    existing = cls.get(project_id, key, default=None)
                    if existing is not None:
                        skipped.append(key)
                        continue
                cls.set(project_id, key, value, value_type=vt)
                applied.append(key)
            except Exception as e:
                errors.append(f"{key}: {e}")
        return {"applied": applied, "skipped": skipped, "errors": errors}

    @classmethod
    def clone_from(cls, source_project_id: str,
                   dest_project_id: str, *,
                   overwrite: bool = True) -> dict[str, Any]:
        """Copy every setting from one project to another. Server-only —
        no file leaves the box."""
        if source_project_id == dest_project_id:
            return {"applied": [], "skipped": [],
                    "errors": ["source and destination are the same"]}
        snapshot = cls.export_all(source_project_id)
        return cls.import_bulk(dest_project_id, snapshot,
                               overwrite=overwrite)

    @classmethod
    def list_for_project(cls, project_id: str) -> list[SettingRow]:
        with session_scope() as s:
            rows = s.execute(
                text("""SELECT setting_key, setting_value, value_type
                        FROM project_settings WHERE project_id = :p"""),
                {"p": project_id},
            ).fetchall()
        overrides = {r[0]: (r[1], r[2]) for r in rows}
        out: list[SettingRow] = []
        for key, (default_val, vt, desc) in cls.DEFAULTS.items():
            if key in overrides:
                val = _coerce(overrides[key][0], overrides[key][1])
            else:
                val = default_val
            out.append(SettingRow(
                key=key, value=val, value_type=vt, category="strategy",
                description=desc, is_secret=False,
            ))
        return out



# ----------------- Cadence presets -----------------------------------------
# When `income_cadence` is set to a preset (not 'custom'), the strategist
# uses these values instead of the stored csp_min_dte / csp_max_dte /
# csp_delta_min / csp_delta_max.
#
# delta ranges trade off "how aggressive" vs "how often assigned":
#   - higher delta => fatter premium but higher assignment probability
#   - lower delta  => safer but smaller premium per cycle
CADENCE_PRESETS: dict[str, dict[str, Any]] = {
    "weekly":   {"min_dte": 5,  "max_dte": 10, "delta_min": 0.20, "delta_max": 0.30},
    "biweekly": {"min_dte": 12, "max_dte": 18, "delta_min": 0.25, "delta_max": 0.35},
    "monthly":  {"min_dte": 25, "max_dte": 35, "delta_min": 0.25, "delta_max": 0.40},
}


def effective_csp_band(project_id: str) -> dict[str, Any]:
    """Return {min_dte, max_dte, delta_min, delta_max, cadence} for CSPs.

    If `income_cadence` is set to a known preset, the preset wins.
    Otherwise the stored csp_* settings (or their defaults) are returned.
    """
    cadence = str(ProjectSettings.get(project_id, "income_cadence",
                                      default="custom") or "custom").lower()
    if cadence in CADENCE_PRESETS:
        p = CADENCE_PRESETS[cadence]
        return {
            "min_dte":   int(p["min_dte"]),
            "max_dte":   int(p["max_dte"]),
            "delta_min": float(p["delta_min"]),
            "delta_max": float(p["delta_max"]),
            "cadence":   cadence,
        }
    return {
        "min_dte":   int(ProjectSettings.get(project_id, "csp_min_dte")),
        "max_dte":   int(ProjectSettings.get(project_id, "csp_max_dte")),
        "delta_min": float(ProjectSettings.get(project_id, "csp_delta_min")),
        "delta_max": float(ProjectSettings.get(project_id, "csp_delta_max")),
        "cadence":   "custom",
    }
