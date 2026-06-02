"""Application entry point.

Modes:
    python main.py            -> Starts API + background runner together
    python main.py api        -> Starts only the FastAPI server (no trading loop)
    python main.py runner     -> Starts only the multi-tenant runner (no HTTP UI)
    python main.py initdb     -> Bootstraps the SQL Server database and schema
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

from dotenv import load_dotenv

load_dotenv()


def _setup_logging() -> None:
    from db.settings_store import AppSettings
    try:
        level_name = AppSettings.get("log_level", "INFO") or "INFO"
    except Exception:
        level_name = os.getenv("LOG_LEVEL", "INFO")
    logging.basicConfig(
        level=getattr(logging, str(level_name).upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


def cmd_initdb() -> int:
    from db import init_database
    init_database()
    print("Database initialized.")
    return 0


def cmd_runner() -> int:
    from db import init_database
    from workers import MultiTenantRunner

    init_database()
    _setup_logging()
    runner = MultiTenantRunner()
    try:
        asyncio.run(runner.run_forever())
    except KeyboardInterrupt:
        runner.stop()
    return 0


def cmd_api(autorun: bool) -> int:
    import uvicorn
    from api.main import app

    app.state.autorun = autorun
    host = os.getenv("API_HOST", "127.0.0.1")
    port = int(os.getenv("API_PORT", "8000"))
    uvicorn.run(app, host=host, port=port, log_level="info")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", nargs="?", default="all",
                        choices=["all", "api", "runner", "initdb"])
    args = parser.parse_args()

    if args.mode == "initdb":
        return cmd_initdb()
    if args.mode == "runner":
        return cmd_runner()
    if args.mode == "api":
        return cmd_api(autorun=False)
    return cmd_api(autorun=True)


if __name__ == "__main__":
    sys.exit(main())
