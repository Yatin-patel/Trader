"""Database connection layer.

Supports both MySQL and SQL Server. Set DB_TYPE=mysql or DB_TYPE=mssql in .env.
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


def _get_db_type() -> str:
    """Return database type: 'mysql' or 'mssql'."""
    return os.getenv("DB_TYPE", "mysql").lower()


def _build_mysql_url() -> str:
    """Build MySQL connection URL."""
    host = os.getenv("DB_HOST", "localhost")
    port = os.getenv("DB_PORT", "3306")
    database = os.getenv("DB_NAME", "TraderDB")
    user = os.getenv("DB_USER", "trader")
    password = os.getenv("DB_PASSWORD", "")
    encoded_password = urllib.parse.quote_plus(password)
    return f"mysql+pymysql://{user}:{encoded_password}@{host}:{port}/{database}?charset=utf8mb4"


def _build_mysql_admin_url() -> str:
    """Build MySQL admin connection URL (no database selected)."""
    host = os.getenv("DB_HOST", "localhost")
    port = os.getenv("DB_PORT", "3306")
    user = os.getenv("DB_USER", "trader")
    password = os.getenv("DB_PASSWORD", "")
    encoded_password = urllib.parse.quote_plus(password)
    return f"mysql+pymysql://{user}:{encoded_password}@{host}:{port}/?charset=utf8mb4"


def _build_odbc_url() -> str:
    """Build SQL Server ODBC connection URL."""
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
        db_type = _get_db_type()
        if db_type == "mysql":
            url = _build_mysql_url()
        else:
            url = _build_odbc_url()
        _engine = create_engine(
            url,
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
    """Create the database (if missing) and apply schema."""
    db_name = os.getenv("DB_NAME", "TraderDB")
    db_type = _get_db_type()

    if db_type == "mysql":
        _init_mysql_database(db_name)
    else:
        _init_mssql_database(db_name)


def _init_mysql_database(db_name: str) -> None:
    """Initialize MySQL database."""
    # For MySQL, database should already exist (created manually or by deploy script)
    # Just apply the schema
    schema_path = Path(__file__).parent / "schema_mysql.sql"
    if not schema_path.exists():
        # Fall back to converting SQL Server schema
        schema_path = Path(__file__).parent / "schema.sql"
        sql = _convert_mssql_to_mysql(schema_path.read_text(encoding="utf-8"))
    else:
        sql = schema_path.read_text(encoding="utf-8")

    engine = get_engine()
    with engine.connect() as conn:
        for statement in _split_mysql_statements(sql):
            statement = statement.strip()
            if not statement:
                continue
            try:
                conn.execute(text(statement))
            except Exception as e:
                # Ignore "table already exists" errors
                if "already exists" not in str(e).lower():
                    raise
        conn.commit()


def _init_mssql_database(db_name: str) -> None:
    """Initialize SQL Server database."""
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


def _split_mysql_statements(sql: str) -> list[str]:
    """Split MySQL statements by semicolon (respecting string literals)."""
    statements = []
    current = []
    in_string = False
    string_char = None

    for char in sql:
        if char in ('"', "'") and not in_string:
            in_string = True
            string_char = char
        elif char == string_char and in_string:
            in_string = False
            string_char = None
        elif char == ';' and not in_string:
            statements.append(''.join(current))
            current = []
            continue
        current.append(char)

    if current:
        statements.append(''.join(current))
    return statements


def _convert_mssql_to_mysql(sql: str) -> str:
    """Convert SQL Server schema to MySQL (basic conversion)."""
    import re
    # Remove GO statements
    sql = re.sub(r'\bGO\b', '', sql, flags=re.IGNORECASE)
    # Convert UNIQUEIDENTIFIER to CHAR(36)
    sql = re.sub(r'\bUNIQUEIDENTIFIER\b', 'CHAR(36)', sql, flags=re.IGNORECASE)
    # Convert NVARCHAR to VARCHAR
    sql = re.sub(r'\bNVARCHAR\b', 'VARCHAR', sql, flags=re.IGNORECASE)
    # Convert NTEXT to TEXT
    sql = re.sub(r'\bNTEXT\b', 'TEXT', sql, flags=re.IGNORECASE)
    # Convert DATETIME(6) to DATETIME(6)
    sql = re.sub(r'\bDATETIME(6)\b', 'DATETIME(6)', sql, flags=re.IGNORECASE)
    # Convert BIT to TINYINT(1)
    sql = re.sub(r'\bBIT\b', 'TINYINT(1)', sql, flags=re.IGNORECASE)
    # Convert UTC_TIMESTAMP() to UTC_TIMESTAMP()
    sql = re.sub(r'\bGETUTCDATE\(\)', 'UTC_TIMESTAMP()', sql, flags=re.IGNORECASE)
    # Convert NEWID() to UUID()
    sql = re.sub(r'\bNEWID\(\)', 'UUID()', sql, flags=re.IGNORECASE)
    # Remove  prefix
    sql = re.sub(r'\bdbo\.', '', sql)
    # Convert [name] to `name`
    sql = re.sub(r'\[([^\]]+)\]', r'`\1`', sql)
    # Remove IF NOT EXISTS for CREATE TABLE (MySQL uses CREATE TABLE IF NOT EXISTS)
    sql = re.sub(r"IF NOT EXISTS\s*\(SELECT.*?CREATE TABLE", 'CREATE TABLE IF NOT EXISTS', sql, flags=re.IGNORECASE | re.DOTALL)
    # Simple IF NOT EXISTS removal for objects
    sql = re.sub(r"IF NOT EXISTS\s*\([^)]+\)\s*BEGIN\s*", '', sql, flags=re.IGNORECASE)
    sql = re.sub(r'\bEND\s*;?\s*$', '', sql, flags=re.IGNORECASE | re.MULTILINE)
    return sql


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
