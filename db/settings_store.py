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
        return fernet.decrypt(value[5:].encode()).decode()
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
                                updated_at = UTC_TIMESTAMP()
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
        "news_sentiment_filter":         (False,   "bool",   "Skip tickers with strongly negative recent news sentiment"),
        "news_sentiment_min":            (-0.30,   "float",  "Skip if recent sentiment score is below this (-1..+1)"),
        "loop_interval_seconds":         (60,      "int",    "Seconds between scan->execute cycles for this project (overrides global)"),
        "order_time_in_force":           ("day",   "string", "Default time-in-force for submitted orders"),
        "use_extended_hours":            (False,   "bool",   "Allow trades during extended hours"),
        "dry_run":                       (True,    "bool",   "If true, skip order submission and only log decisions"),
        # Day Trading / 0DTE Settings
        "allow_0dte":                    (False,   "bool",   "Allow 0DTE (same-day expiration) options trades"),
        "allow_1dte":                    (False,   "bool",   "Allow 1DTE options trades"),
        "max_0dte_contracts":            (2,       "int",    "Maximum number of 0DTE contracts allowed open at once"),
        "0dte_profit_target_pct":        (0.30,    "float",  "Profit target for 0DTE trades (30% default - close early)"),
        "0dte_stop_loss_pct":            (0.50,    "float",  "Stop loss for 0DTE trades (50% of premium)"),
        "0dte_exit_time_minutes":        (30,      "int",    "Close 0DTE positions this many minutes before market close"),
        "intraday_scanner_enabled":      (False,   "bool",   "Enable intraday RSI/MACD scanner for day trading signals"),
        "intraday_rsi_oversold":         (30,      "int",    "RSI level considered oversold for buy signals"),
        "intraday_rsi_overbought":       (70,      "int",    "RSI level considered overbought for sell signals"),
        "trailing_stop_enabled":         (False,   "bool",   "Use trailing stop orders for profit protection"),
        "trailing_stop_pct":             (0.05,    "float",  "Trailing stop percentage from high water mark"),
        "bracket_orders_enabled":        (False,   "bool",   "Use bracket (OCO) orders with take-profit and stop-loss"),
        "bracket_take_profit_pct":       (0.50,    "float",  "Take profit percentage for bracket orders"),
        "bracket_stop_loss_pct":         (0.25,    "float",  "Stop loss percentage for bracket orders"),
    }

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
                            SET setting_value = :v, value_type = :vt, updated_at = UTC_TIMESTAMP()
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
