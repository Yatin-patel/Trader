"""SQL Server Express connection layer.

Bootstrap connection comes from environment so the database itself can host
all other configuration. Nothing operational is hardcoded.
"""
from __future__ import annotations

import os
import urllib.parse
from pathlib import Path
from typing import Iterator

from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

load_dotenv()

_engine: Engine | None = None
_SessionLocal: sessionmaker | None = None


def _build_odbc_url() -> str:
    driver = os.getenv("DB_DRIVER", "ODBC Driver 17 for SQL Server")
    server = os.getenv("DB_SERVER", r"localhost\SQLEXPRESS")
    database = os.getenv("DB_NAME", "TraderDB")
    trusted = os.getenv("DB_TRUSTED_CONNECTION", "yes").lower() in ("yes", "true", "1")

    parts = [
        f"DRIVER={{{driver}}}",
        f"SERVER={server}",
        f"DATABASE={database}",
        "TrustServerCertificate=yes",
    ]
    if trusted:
        parts.append("Trusted_Connection=yes")
    else:
        user = os.getenv("DB_USER", "")
        password = os.getenv("DB_PASSWORD", "")
        parts.append(f"UID={user}")
        parts.append(f"PWD={password}")
    odbc = ";".join(parts) + ";"
    return "mssql+pyodbc:///?odbc_connect=" + urllib.parse.quote_plus(odbc)


def _build_master_url() -> str:
    """Same connection but pointed at master so we can CREATE DATABASE."""
    driver = os.getenv("DB_DRIVER", "ODBC Driver 17 for SQL Server")
    server = os.getenv("DB_SERVER", r"localhost\SQLEXPRESS")
    trusted = os.getenv("DB_TRUSTED_CONNECTION", "yes").lower() in ("yes", "true", "1")

    parts = [
        f"DRIVER={{{driver}}}",
        f"SERVER={server}",
        "DATABASE=master",
        "TrustServerCertificate=yes",
    ]
    if trusted:
        parts.append("Trusted_Connection=yes")
    else:
        parts.append(f"UID={os.getenv('DB_USER', '')}")
        parts.append(f"PWD={os.getenv('DB_PASSWORD', '')}")
    odbc = ";".join(parts) + ";"
    return "mssql+pyodbc:///?odbc_connect=" + urllib.parse.quote_plus(odbc)


def get_engine() -> Engine:
    global _engine, _SessionLocal
    if _engine is None:
        _engine = create_engine(
            _build_odbc_url(),
            pool_pre_ping=True,
            pool_size=10,
            max_overflow=20,
            future=True,
        )
        _SessionLocal = sessionmaker(bind=_engine, autoflush=False, expire_on_commit=False)
    return _engine


def get_session() -> Iterator[Session]:
    if _SessionLocal is None:
        get_engine()
    assert _SessionLocal is not None
    session = _SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def session_scope() -> Session:
    if _SessionLocal is None:
        get_engine()
    assert _SessionLocal is not None
    return _SessionLocal()


def init_database() -> None:
    """Create the database (if missing) and apply schema.sql."""
    db_name = os.getenv("DB_NAME", "TraderDB")

    master_engine = create_engine(_build_master_url(), isolation_level="AUTOCOMMIT", future=True)
    with master_engine.connect() as conn:
        exists = conn.execute(
            text("SELECT DB_ID(:n)"), {"n": db_name}
        ).scalar()
        if exists is None:
            conn.execute(text(f"CREATE DATABASE [{db_name}]"))
    master_engine.dispose()

    schema_path = Path(__file__).parent / "schema.sql"
    sql = schema_path.read_text(encoding="utf-8")

    engine = get_engine()
    with engine.connect() as conn:
        for batch in _split_go_batches(sql):
            batch = batch.strip()
            if not batch:
                continue
            conn.execute(text(batch))
        conn.commit()


def _split_go_batches(sql: str) -> list[str]:
    """SQL Server batch separator is GO on its own line."""
    out: list[str] = []
    buf: list[str] = []
    for line in sql.splitlines():
        if line.strip().upper() == "GO":
            out.append("\n".join(buf))
            buf = []
        else:
            buf.append(line)
    if buf:
        out.append("\n".join(buf))
    return out
