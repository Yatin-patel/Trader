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
        "loop_interval_seconds":         (60,      "int",    "Seconds between scan->execute cycles for this project (overrides global)"),
        "order_time_in_force":           ("day",   "string", "Default time-in-force for submitted orders"),
        "strategy_mode":                 ("wheel", "string", "What this project trades: 'wheel' (CSP+CC options income — default), 'wheel_plus_dca' (options + scheduled stock buys), 'dca_only' (long-term stock accumulation, no options), 'paused' (don't trade — for manual review). Day-trading and multi-leg spreads are not yet implemented."),
        "use_extended_hours":            (False,   "bool",   "Allow trades during extended hours"),
        "income_cadence":                ("custom","string", "Income cadence preset: weekly | biweekly | monthly | custom. When set to a preset, the strategist overrides csp_min_dte / csp_max_dte / csp_delta_min / csp_delta_max with the preset values, and auto-roll will roll any open contract whose remaining DTE drifts outside the preset band."),
        "dry_run":                       (True,    "bool",   "If true, skip order submission and only log decisions"),
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
