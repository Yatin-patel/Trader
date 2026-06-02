"""SQL Server BACKUP DATABASE + retention pruning.

Uses BACKUP DATABASE T-SQL which writes to a path readable by the SQL Server
service account. The configured `backup_dir` must be writable by that
account (typically a path like C:\\trader_backups).
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text

from db.connection import get_engine, session_scope
from db.settings_store import AppSettings

logger = logging.getLogger(__name__)


def _backup_dir() -> str:
    raw = str(AppSettings.get("backup_dir", "C:\\trader_backups")
              or "C:\\trader_backups")
    # Normalize away double backslashes from SQL-escaped seed values.
    return os.path.normpath(raw)


def _retention_days() -> int:
    try:
        return int(AppSettings.get("backup_retention_days", 14) or 14)
    except Exception:
        return 14


def _ensure_dir(path: str) -> None:
    try:
        os.makedirs(path, exist_ok=True)
    except Exception as e:
        logger.warning("could not create backup dir %s: %s", path, e)


def run_backup() -> dict[str, Any]:
    bdir = _backup_dir()
    _ensure_dir(bdir)
    db_name = os.getenv("DB_NAME", "TraderDB")
    ts = datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S")
    filename = f"{db_name}_{ts}.bak"
    full_path = os.path.join(bdir, filename)

    with session_scope() as s:
        row = s.execute(text("""
            INSERT INTO dbo.backup_log (started_at, status, path)
            OUTPUT INSERTED.backup_id
            VALUES (SYSUTCDATETIME(), 'RUNNING', :p)
        """), {"p": full_path}).fetchone()
        backup_id = int(row[0])
        s.commit()

    # Run the actual BACKUP. SQL Server refuses to BACKUP inside a
    # transaction. SQLAlchemy's pool starts an implicit one on checkout
    # that we can't unwind, so we open a FRESH pyodbc connection with
    # autocommit=True at construction time (bypassing the pool entirely).
    error_msg: str | None = None
    try:
        import pyodbc

        driver = os.getenv("DB_DRIVER", "ODBC Driver 17 for SQL Server")
        server = os.getenv("DB_SERVER", r"localhost\SQLEXPRESS")
        trusted = os.getenv("DB_TRUSTED_CONNECTION", "yes").lower() in (
            "yes", "true", "1")
        parts = [f"DRIVER={{{driver}}}", f"SERVER={server}",
                 f"DATABASE={db_name}", "TrustServerCertificate=yes"]
        if trusted:
            parts.append("Trusted_Connection=yes")
        else:
            parts.append(f"UID={os.getenv('DB_USER', '')}")
            parts.append(f"PWD={os.getenv('DB_PASSWORD', '')}")
        conn_str = ";".join(parts) + ";"

        conn = pyodbc.connect(conn_str, autocommit=True)
        try:
            cur = conn.cursor()
            cur.execute(
                f"BACKUP DATABASE [{db_name}] TO DISK = ? "
                f"WITH FORMAT, INIT, NAME = ?",
                full_path, f"trader-{ts}",
            )
            while True:
                try:
                    cur.fetchall()
                except Exception:
                    pass
                if not cur.nextset():
                    break
        finally:
            try:
                conn.close()
            except Exception:
                pass
    except Exception as e:
        error_msg = str(e)
        logger.exception("backup failed: %s", e)

    # Get size if it exists
    size = None
    try:
        if os.path.exists(full_path):
            size = os.path.getsize(full_path)
    except Exception:
        pass

    with session_scope() as s:
        s.execute(text("""
            UPDATE dbo.backup_log
            SET completed_at = SYSUTCDATETIME(),
                status = :st,
                size_bytes = :sz,
                error_message = :err
            WHERE backup_id = :bid
        """), {"bid": backup_id,
               "st": "FAILED" if error_msg else "COMPLETE",
               "sz": size, "err": (error_msg[:500] if error_msg else None)})
        s.commit()
    return {"backup_id": backup_id, "path": full_path,
            "size_bytes": size, "error": error_msg}


def prune_old_backups() -> dict[str, Any]:
    bdir = _backup_dir()
    days = _retention_days()
    if not os.path.isdir(bdir):
        return {"removed": 0, "reason": "backup_dir does not exist"}
    cutoff = datetime.now() - timedelta(days=days)
    removed = []
    for fn in os.listdir(bdir):
        if not fn.endswith(".bak"):
            continue
        full = os.path.join(bdir, fn)
        try:
            mtime = datetime.fromtimestamp(os.path.getmtime(full))
            if mtime < cutoff:
                os.remove(full)
                removed.append(fn)
        except Exception as e:
            logger.warning("could not prune %s: %s", fn, e)
    return {"removed": len(removed), "files": removed}


def list_backups(limit: int = 20) -> list[dict[str, Any]]:
    with session_scope() as s:
        rows = s.execute(text("""
            SELECT TOP (:lim) backup_id, started_at, completed_at, status,
                              path, size_bytes, error_message
            FROM dbo.backup_log
            ORDER BY backup_id DESC
        """), {"lim": int(limit)}).fetchall()
    return [{
        "backup_id": int(r[0]),
        "started_at": r[1].isoformat() if r[1] else None,
        "completed_at": r[2].isoformat() if r[2] else None,
        "status": r[3],
        "path": r[4],
        "size_bytes": int(r[5]) if r[5] is not None else None,
        "error_message": r[6],
    } for r in rows]
