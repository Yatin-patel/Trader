"""Pytest fixtures for integration tests.

Strategy:
  * Each test run creates a fresh MySQL database named ``TraderDB_test_<pid>``
    using the local root credentials, applies the production schema, and
    drops the DB at teardown. That way we exercise the SAME SQL the
    production app runs (MySQL-specific syntax included), not a
    SQLite-with-fingers-crossed approximation.
  * The Alpaca client is monkey-patched at the import path the runner
    uses, so the strategist/executor get our FakeAlpacaClient instead.
  * A fixture creates a known project + settings so each test starts
    from a deterministic baseline.

If MySQL isn't available locally these tests are skipped (with a clear
message), not silently passed.
"""
from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture(scope="session")
def test_db_name() -> str:
    return f"TraderDB_test_{os.getpid()}_{uuid.uuid4().hex[:6]}"


@pytest.fixture(scope="session")
def mysql_admin_password() -> str:
    """MySQL root password used to create/drop the throwaway test DB.

    Looked up from env (TRADER_TEST_MYSQL_PASSWORD) so CI can inject it.
    Falls back to the local dev value as a convenience.
    """
    return os.getenv("TRADER_TEST_MYSQL_PASSWORD", "baPa8usa")


@pytest.fixture(scope="session", autouse=True)
def _bootstrap_test_db(test_db_name, mysql_admin_password):
    """Create the throwaway DB + apply schema. Tear down after the session.

    Sets the env vars the app's connection layer reads BEFORE any app
    module is imported. Critical: db.connection caches its engine, so
    importing the app first then changing env breaks the test.
    """
    try:
        import pymysql
    except ImportError:
        pytest.skip("pymysql not installed; cannot run integration tests")

    # Connect as root, create DB
    try:
        admin = pymysql.connect(
            host="localhost", port=3306,
            user="root", password=mysql_admin_password,
            charset="utf8mb4", autocommit=True,
        )
    except Exception as e:
        pytest.skip(f"Local MySQL not reachable as root: {e}")

    with admin.cursor() as cur:
        cur.execute(f"DROP DATABASE IF EXISTS `{test_db_name}`")
        cur.execute(
            f"CREATE DATABASE `{test_db_name}` "
            "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
        )

    # Point the app at this test DB BEFORE anything app-related imports
    os.environ["DB_TYPE"] = "mysql"
    os.environ["DB_HOST"] = "localhost"
    os.environ["DB_PORT"] = "3306"
    os.environ["DB_NAME"] = test_db_name
    os.environ["DB_USER"] = "root"
    os.environ["DB_PASSWORD"] = mysql_admin_password
    # Stable Fernet key for the test session.
    os.environ.setdefault(
        "SECRET_ENCRYPTION_KEY",
        "izQtPKfSfHgfLDGByBTJd7-FHfnRbij7Xou9roa0l-M=",
    )

    # Apply the production MySQL schema
    schema_path = REPO_ROOT / "db" / "schema_mysql.sql"
    schema_sql = schema_path.read_text(encoding="utf-8")
    with pymysql.connect(
        host="localhost", port=3306, user="root",
        password=mysql_admin_password, database=test_db_name,
        charset="utf8mb4", autocommit=False,
    ) as conn:
        with conn.cursor() as cur:
            cur.execute("SET FOREIGN_KEY_CHECKS = 0")
            from db.connection import _split_mysql_statements
            for stmt in _split_mysql_statements(schema_sql):
                stmt = stmt.strip()
                if not stmt:
                    continue
                try:
                    cur.execute(stmt)
                except pymysql.Error as e:
                    msg = str(e).lower()
                    if "already exists" in msg or "duplicate column" in msg \
                       or "duplicate key name" in msg:
                        continue
                    raise
            cur.execute("SET FOREIGN_KEY_CHECKS = 1")
        conn.commit()

    yield test_db_name

    # Teardown: drop the test DB
    try:
        with admin.cursor() as cur:
            cur.execute(f"DROP DATABASE IF EXISTS `{test_db_name}`")
    finally:
        admin.close()


@pytest.fixture(autouse=True)
def _isolate_per_test(_bootstrap_test_db):
    """Wipe per-cycle state between tests.

    Without this, the second test's call to ``EventsRepo.recent(...)``
    happily returns Scanner/Strategist/Executor rows from the FIRST
    test, breaking the "Scanner should not have run" / "no SUBMITTED
    status" assertions in the paused-mode and dry-run tests.

    Truncates only the per-cycle / per-trade tables. Project rows and
    settings are left in place because each test fixture re-upserts
    its own project and settings as needed.
    """
    yield
    # Teardown after each test
    # Clear process-wide caches that leak state across tests.
    try:
        from workers import tenant_worker as _tw
        _tw._CLOCK_CACHE["ts"] = 0.0
        _tw._CLOCK_CACHE["value"] = None
        _tw._CAL_CACHE.clear()
    except Exception:
        pass
    try:
        from db.connection import session_scope
        from sqlalchemy import text as _text
    except Exception:
        return
    tables = [
        "agent_events",
        "wheel_contracts",
        "stock_positions",
        "closed_contracts",
        "closed_positions",
        "orders",
        "wheel_cycles",
        "ai_recommendations",
        "anomalies",
        "portfolio_snapshots",
        "intraday_signals",
        "bracket_orders",
        "day_trade_log",
        "multi_leg_orders",
        "trade_journal",
    ]
    with session_scope() as s:
        s.execute(_text("SET FOREIGN_KEY_CHECKS = 0"))
        for t in tables:
            try:
                s.execute(_text(f"TRUNCATE TABLE {t}"))
            except Exception:
                pass
        s.execute(_text("SET FOREIGN_KEY_CHECKS = 1"))
        s.commit()


@pytest.fixture
def project_id() -> str:
    return "test-wheel-project"


@pytest.fixture
def sample_project(project_id, _bootstrap_test_db):
    """Insert a known project + sensible-default settings."""
    from db.repositories import ProjectsRepo, TradingProject
    from db.settings_store import ProjectSettings

    proj = TradingProject(
        project_id=project_id,
        project_name="Integration Test Wheel",
        alpaca_api_key="TEST_KEY",
        alpaca_secret_key="TEST_SECRET",
        alpaca_base_url="https://paper-api.alpaca.markets",
        alpaca_data_feed="iex",
        max_equity_allocation=25_000.0,
        is_active=True,
        user_id=None,
        broker_type="alpaca",
    )
    ProjectsRepo.upsert(proj)

    # Settings tuned so a simple cycle should produce a trade.
    settings = {
        # Universe + scanner — small synthetic watchlist
        "watchlist": "F,SOFI,HOOD,NIO,PLTR,COIN",
        "volume_threshold": 1_000_000,
        "scanner_min_price": 2.0,
        "scanner_max_price": 100.0,
        "scanner_min_pct_change": 0.5,
        "scanner_top_n": 10,
        # CSP envelope wide enough for the fake-alpaca strikes
        "csp_delta_min": 0.10,
        "csp_delta_max": 0.50,
        "csp_min_dte": 10,
        "csp_max_dte": 35,
        "income_cadence": "custom",
        # Loose risk so it actually fires
        "max_open_contracts": 10,
        "max_collateral_pct": 0.90,
        "max_concentration_per_ticker": 0.50,
        "max_concentration_per_sector": 0.80,
        "contracts_per_csp": 1,
        # Disable filters that block in CI
        "min_iv_rank": 0.0,
        "news_sentiment_filter": False,
        "skip_event_days_within": 0,
        "avoid_earnings_within_dte": 0,
        "use_extended_hours": True,  # so the worker's market gate passes
        "dry_run": False,
        "strategy_mode": "wheel",
        "recent_failure_skip_minutes": 0,
    }
    for k, v in settings.items():
        if isinstance(v, bool):
            ProjectSettings.set(project_id, k, v, value_type="bool")
        elif isinstance(v, int):
            ProjectSettings.set(project_id, k, v, value_type="int")
        elif isinstance(v, float):
            ProjectSettings.set(project_id, k, v, value_type="float")
        else:
            ProjectSettings.set(project_id, k, v, value_type="string")

    return proj


@pytest.fixture
def patched_alpaca(monkeypatch):
    """Patch the AlpacaClient import wherever it's used in the pipeline.

    Returns a holder dict whose ``client`` field is the singleton
    FakeAlpacaClient bound to the test's project_id.

    Singleton semantics matter: the production codebase instantiates
    AlpacaClient lazily in many places (per-node in the graph, plus
    closure_detector, snapshotter, take_profit, auto_roll all run AFTER
    the executor). If the factory returned a fresh instance each time,
    ``holder["client"]`` would end up pointing at one of the post-cycle
    callers — never the Executor's — and tests would falsely report
    "no orders submitted" even when the pipeline succeeded. A real
    Alpaca account is a single source of truth shared by all callers;
    mirror that here.
    """
    from tests.fake_alpaca import FakeAlpacaClient

    holder: dict = {"client": None, "by_project": {}}

    def factory(project):
        pid = getattr(project, "project_id", None) or id(project)
        cached = holder["by_project"].get(pid)
        if cached is None:
            cached = FakeAlpacaClient(project)
            holder["by_project"][pid] = cached
        holder["client"] = cached
        return cached

    # Patch every module path through which AlpacaClient OR get_broker
    # is imported. After the Phase 2 refactor agents call get_broker()
    # to stay broker-agnostic, so the new symbol name is what matters.
    # We continue to patch AlpacaClient too for modules in analytics/
    # and risk/ that haven't been migrated yet.
    targets = [
        "execution",
        "agents.scanner", "agents.strategist",
        "agents.guardrail", "agents.executor",
        "agents.intraday_scanner",
        "workers.tenant_worker", "workers.runner",
        "analytics.closure_detector", "analytics.snapshotter",
        "analytics.iv_rank", "analytics.dividends",
        "analytics.intraday_signals",
        "risk.take_profit", "risk.auto_roll",
        "backtest.runner",
    ]
    import importlib
    for mod_path in targets:
        try:
            mod = importlib.import_module(mod_path)
        except ImportError:
            continue
        for sym in ("AlpacaClient", "get_broker"):
            if hasattr(mod, sym):
                monkeypatch.setattr(mod, sym, factory)

    return holder
