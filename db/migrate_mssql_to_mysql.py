"""Migrate data from SQL Server to MySQL.

One-shot table-by-table copy designed to run unattended on either the local
dev box (default args) or production (override via CLI flags). Foreign-key
checks are disabled during the copy so order doesn't matter. Each target
table is TRUNCATEd before insert so the run is idempotent — re-running
produces the same result.

Usage examples
--------------
Local default (current .env points to local MSSQL, MySQL is on localhost):

    python -m db.migrate_mssql_to_mysql \\
        --target-password baPa8usa

Production (run on the server itself):

    python -m db.migrate_mssql_to_mysql \\
        --source-server "localhost" \\
        --source-user "sa" --source-password "AuthPass2024" \\
        --target-host  "localhost" --target-port 3306 \\
        --target-user  "root"      --target-password "<prod-mysql-pass>"

Or set everything via environment variables:

    SRC_SERVER=...  SRC_DATABASE=TraderDB  SRC_USER=sa  SRC_PASSWORD=...
    TGT_HOST=...    TGT_PORT=3306          TGT_USER=root TGT_PASSWORD=...
    TGT_DATABASE=TraderDB
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
import urllib.parse
from decimal import Decimal
from pathlib import Path
from typing import Any

import pymysql
from sqlalchemy import create_engine, text


# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------
BATCH_SIZE = 500            # rows per INSERT batch
DEFAULT_TARGET_DB = "TraderDB"


# ---------------------------------------------------------------------------
# CLI / config
# ---------------------------------------------------------------------------
def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)

    # Source (SQL Server)
    sg = p.add_argument_group("source (SQL Server)")
    sg.add_argument("--source-driver",
                    default=os.getenv("SRC_DRIVER", "ODBC Driver 17 for SQL Server"))
    sg.add_argument("--source-server",
                    default=os.getenv("SRC_SERVER", r"localhost\SQLEXPRESS"),
                    help=r"e.g. localhost\SQLEXPRESS or 127.0.0.1,1433")
    sg.add_argument("--source-database",
                    default=os.getenv("SRC_DATABASE", "TraderDB"))
    sg.add_argument("--source-trusted",
                    default=os.getenv("SRC_TRUSTED", "yes"),
                    help="yes|no — Windows integrated auth")
    sg.add_argument("--source-user", default=os.getenv("SRC_USER", ""))
    sg.add_argument("--source-password",
                    default=os.getenv("SRC_PASSWORD", ""))

    # Target (MySQL)
    tg = p.add_argument_group("target (MySQL)")
    tg.add_argument("--target-host",
                    default=os.getenv("TGT_HOST", "localhost"))
    tg.add_argument("--target-port", type=int,
                    default=int(os.getenv("TGT_PORT", "3306")))
    tg.add_argument("--target-user",
                    default=os.getenv("TGT_USER", "root"))
    tg.add_argument("--target-password",
                    default=os.getenv("TGT_PASSWORD", ""))
    tg.add_argument("--target-database",
                    default=os.getenv("TGT_DATABASE", DEFAULT_TARGET_DB))

    # Behaviour
    p.add_argument("--apply-schema", action="store_true",
                   help="Run schema_mysql.sql before copying data (creates the database too)")
    p.add_argument("--only", default="",
                   help="Comma-separated list of tables to migrate. Default = all")
    p.add_argument("--skip", default="",
                   help="Comma-separated tables to skip")
    p.add_argument("--dry-run", action="store_true",
                   help="Print plan and row counts but don't write to target")
    return p.parse_args()


def _src_engine(args):
    """Build SQLAlchemy engine for the source SQL Server."""
    parts = [
        f"DRIVER={{{args.source_driver}}}",
        f"SERVER={args.source_server}",
        f"DATABASE={args.source_database}",
        "TrustServerCertificate=yes",
    ]
    if args.source_trusted.lower() in ("yes", "true", "1"):
        parts.append("Trusted_Connection=yes")
    else:
        parts.append(f"UID={args.source_user}")
        parts.append(f"PWD={args.source_password}")
    odbc = ";".join(parts) + ";"
    url = "mssql+pyodbc:///?odbc_connect=" + urllib.parse.quote_plus(odbc)
    return create_engine(url, future=True)


def _connect_mysql(args, *, with_db: bool):
    """Open a raw pymysql connection. Use with_db=False to issue
    CREATE DATABASE before the target DB exists."""
    return pymysql.connect(
        host=args.target_host,
        port=args.target_port,
        user=args.target_user,
        password=args.target_password,
        database=args.target_database if with_db else None,
        charset="utf8mb4",
        autocommit=False,
    )


# ---------------------------------------------------------------------------
# Schema bootstrap
# ---------------------------------------------------------------------------
def _ensure_database_and_schema(args) -> None:
    """Create the target database if missing, then apply schema_mysql.sql."""
    # 1. CREATE DATABASE IF NOT EXISTS
    conn = _connect_mysql(args, with_db=False)
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"CREATE DATABASE IF NOT EXISTS `{args.target_database}` "
                "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            )
        conn.commit()
    finally:
        conn.close()
    print(f"[ok] database ready: {args.target_database}")

    # 2. Apply schema_mysql.sql
    schema_path = Path(__file__).parent / "schema_mysql.sql"
    if not schema_path.exists():
        raise SystemExit(f"missing schema file: {schema_path}")
    sql = schema_path.read_text(encoding="utf-8")
    statements = _split_statements(sql)

    conn = _connect_mysql(args, with_db=True)
    applied = 0
    try:
        with conn.cursor() as cur:
            cur.execute("SET FOREIGN_KEY_CHECKS = 0")
            for stmt in statements:
                if not stmt.strip():
                    continue
                try:
                    cur.execute(stmt)
                    applied += 1
                except pymysql.Error as e:
                    msg = str(e).lower()
                    # Idempotent apply: tolerate "already exists" (tables/
                    # indexes) and "duplicate column" (ALTER ADD COLUMN
                    # rerun against an already-migrated table).
                    if "already exists" in msg or "duplicate column" in msg:
                        continue
                    raise SystemExit(
                        f"schema apply failed:\n  stmt={stmt[:120]}...\n  err={e}"
                    )
            cur.execute("SET FOREIGN_KEY_CHECKS = 1")
        conn.commit()
    finally:
        conn.close()
    print(f"[ok] schema applied ({applied} statements)")


def _split_statements(sql: str) -> list[str]:
    """Split on `;` outside string literals AND outside SQL comments.
    Handles `--` line comments and `/* */` block comments."""
    out: list[str] = []
    buf: list[str] = []
    in_str = False
    quote: str | None = None
    in_line_comment = False
    in_block_comment = False
    i = 0
    n = len(sql)
    while i < n:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""
        if in_line_comment:
            buf.append(ch)
            if ch == "\n":
                in_line_comment = False
            i += 1
            continue
        if in_block_comment:
            buf.append(ch)
            if ch == "*" and nxt == "/":
                buf.append(nxt)
                i += 2
                in_block_comment = False
                continue
            i += 1
            continue
        if in_str:
            buf.append(ch)
            if ch == quote:
                in_str = False
            i += 1
            continue
        if ch == "-" and nxt == "-":
            in_line_comment = True
            buf.append(ch)
            i += 1
            continue
        if ch == "/" and nxt == "*":
            in_block_comment = True
            buf.append(ch)
            i += 1
            continue
        if ch in ('"', "'"):
            in_str = True
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if ch == ";":
            out.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    if buf:
        out.append("".join(buf))
    return out


# ---------------------------------------------------------------------------
# Source introspection
# ---------------------------------------------------------------------------
def _list_source_tables(src_eng, database: str) -> list[str]:
    sql = (
        "SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES "
        "WHERE TABLE_CATALOG = :db AND TABLE_TYPE = 'BASE TABLE' "
        "  AND TABLE_SCHEMA = 'dbo' "
        "ORDER BY TABLE_NAME"
    )
    with src_eng.connect() as conn:
        rows = conn.execute(text(sql), {"db": database}).fetchall()
    return [r[0] for r in rows]


def _column_meta(src_eng, table: str) -> list[tuple[str, str]]:
    """Return [(column_name, data_type), ...] in ordinal position."""
    sql = (
        "SELECT COLUMN_NAME, DATA_TYPE "
        "FROM INFORMATION_SCHEMA.COLUMNS "
        "WHERE TABLE_NAME = :t AND TABLE_SCHEMA = 'dbo' "
        "ORDER BY ORDINAL_POSITION"
    )
    with src_eng.connect() as conn:
        rows = conn.execute(text(sql), {"t": table}).fetchall()
    return [(r[0], r[1].lower()) for r in rows]


# ---------------------------------------------------------------------------
# Value coercion
# ---------------------------------------------------------------------------
def _coerce(val: Any, sqltype: str) -> Any:
    """Coerce a single value from SQL Server -> MySQL-friendly form."""
    if val is None:
        return None

    # SQL Server BIT -> TINYINT(1). pyodbc returns True/False already.
    if sqltype == "bit":
        return 1 if val else 0

    # UNIQUEIDENTIFIER -> 36-char string (already a UUID string in pyodbc)
    if sqltype == "uniqueidentifier":
        return str(val)

    # datetimes: passthrough; pymysql handles datetime/date objects
    if isinstance(val, (dt.datetime, dt.date)):
        return val

    # Decimal: passthrough
    if isinstance(val, Decimal):
        return val

    # bytes/varbinary: passthrough
    if isinstance(val, (bytes, bytearray, memoryview)):
        return bytes(val)

    # bool sneaking through
    if isinstance(val, bool):
        return 1 if val else 0

    return val


# ---------------------------------------------------------------------------
# Per-table copy
# ---------------------------------------------------------------------------
def _target_columns(tgt_conn, database: str, table: str) -> list[str]:
    """Return the list of columns that exist on the MySQL target table.
    Empty list means the table itself is missing."""
    sql = (
        "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
        "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s "
        "ORDER BY ORDINAL_POSITION"
    )
    with tgt_conn.cursor() as cur:
        cur.execute(sql, (database, table))
        return [r[0] for r in cur.fetchall()]


def _copy_table(src_eng, tgt_conn, table: str, *, dry_run: bool,
                target_db: str) -> tuple[int, int, str]:
    """Copy a single table. Returns (src_rows, written, status_note)."""
    cols = _column_meta(src_eng, table)
    if not cols:
        return (0, 0, "no source columns")

    # Intersect source columns with target columns so we only INSERT what
    # actually exists on both sides. Lets us survive minor schema drift
    # between SQL Server and the MySQL port.
    target_cols = set()
    if tgt_conn is not None:
        target_cols = {c.lower() for c in _target_columns(tgt_conn, target_db, table)}
        if not target_cols:
            return (0, 0, "TARGET TABLE MISSING — skipped")

    paired = [(name, typ) for (name, typ) in cols
              if name.lower() in target_cols or not target_cols]
    skipped_cols = [n for (n, _) in cols if n.lower() not in target_cols and target_cols]

    if not paired:
        return (0, 0, "no overlapping columns")

    col_names = [c[0] for c in paired]
    col_types = [c[1] for c in paired]
    quoted_target_cols = ", ".join(f"`{c}`" for c in col_names)
    placeholders = ", ".join(["%s"] * len(col_names))
    insert_sql = (
        f"INSERT INTO `{table}` ({quoted_target_cols}) VALUES ({placeholders})"
    )

    # 1. Count source rows
    with src_eng.connect() as sconn:
        src_rows = sconn.execute(text(f"SELECT COUNT(*) FROM [dbo].[{table}]")).scalar()

    skip_note = ""
    if skipped_cols:
        skip_note = f" (skipping cols absent in target: {', '.join(skipped_cols)})"

    print(f"  {table:30s} source={src_rows:>7} rows{skip_note}", end="", flush=True)
    if dry_run:
        print("  (dry-run, no writes)")
        return (src_rows, 0, "dry-run")
    if src_rows == 0:
        # Still TRUNCATE the target so a re-run leaves zero rows there too.
        with tgt_conn.cursor() as cur:
            cur.execute(f"TRUNCATE TABLE `{table}`")
        tgt_conn.commit()
        print("  -> truncated target, nothing to insert")
        return (0, 0, "empty source")

    # 2. TRUNCATE target table
    with tgt_conn.cursor() as cur:
        cur.execute(f"TRUNCATE TABLE `{table}`")
    tgt_conn.commit()

    # 3. Stream source rows in batches and INSERT into target
    written = 0
    quoted_src_cols = ", ".join(f"[{c}]" for c in col_names)
    select_sql = f"SELECT {quoted_src_cols} FROM [dbo].[{table}]"
    with src_eng.connect() as sconn:
        result = sconn.execution_options(stream_results=True).execute(text(select_sql))
        while True:
            batch = result.fetchmany(BATCH_SIZE)
            if not batch:
                break
            payload = [
                tuple(_coerce(v, col_types[i]) for i, v in enumerate(row))
                for row in batch
            ]
            with tgt_conn.cursor() as cur:
                cur.executemany(insert_sql, payload)
            tgt_conn.commit()
            written += len(payload)
            print(".", end="", flush=True)
    print(f"  -> wrote {written}")
    return (src_rows, written, "ok")


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------
def main() -> int:
    args = _parse_args()
    print(f"source: SQL Server  {args.source_server} / {args.source_database}")
    print(f"target: MySQL       {args.target_user}@{args.target_host}:{args.target_port} / {args.target_database}")
    print(f"mode:   {'DRY-RUN' if args.dry_run else 'WRITE'}")
    if args.apply_schema:
        print("schema: will be applied")
    print()

    # 1. Bootstrap target if requested
    if args.apply_schema and not args.dry_run:
        _ensure_database_and_schema(args)

    # 2. List source tables
    src_eng = _src_engine(args)
    tables = _list_source_tables(src_eng, args.source_database)

    only = {t.strip() for t in args.only.split(",") if t.strip()}
    skip = {t.strip() for t in args.skip.split(",") if t.strip()}
    if only:
        tables = [t for t in tables if t in only]
    tables = [t for t in tables if t not in skip]

    print(f"[plan] {len(tables)} tables to migrate")
    for t in tables:
        print(f"   - {t}")
    print()

    # 3. Connect to target with FK checks disabled
    if not args.dry_run:
        tgt_conn = _connect_mysql(args, with_db=True)
    else:
        tgt_conn = None
    try:
        if tgt_conn is not None:
            with tgt_conn.cursor() as cur:
                cur.execute("SET FOREIGN_KEY_CHECKS = 0")
            tgt_conn.commit()

        totals = {"tables": 0, "src_rows": 0, "written": 0,
                  "errors": 0, "skipped": 0}
        skipped_tables: list[str] = []
        for t in tables:
            try:
                src, wrote, note = _copy_table(
                    src_eng, tgt_conn, t,
                    dry_run=args.dry_run,
                    target_db=args.target_database,
                )
                totals["tables"] += 1
                totals["src_rows"] += src
                totals["written"] += wrote
                if "MISSING" in note:
                    print(f"  {t:30s} [skip] {note}")
                    totals["skipped"] += 1
                    skipped_tables.append(t)
            except Exception as e:
                totals["errors"] += 1
                print(f"  [FAIL] {t}: {e}")

        if tgt_conn is not None:
            with tgt_conn.cursor() as cur:
                cur.execute("SET FOREIGN_KEY_CHECKS = 1")
            tgt_conn.commit()
    finally:
        if tgt_conn is not None:
            tgt_conn.close()

    print()
    print("=" * 64)
    print(f"tables={totals['tables']}  src_rows={totals['src_rows']}  "
          f"written={totals['written']}  skipped={totals['skipped']}  "
          f"errors={totals['errors']}")
    if skipped_tables:
        print("missing in target schema:")
        for t in skipped_tables:
            print(f"  - {t}")
    return 0 if totals["errors"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
