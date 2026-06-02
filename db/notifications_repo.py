"""Repos for notification_channels and notifications."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text

from .connection import session_scope


class ChannelsRepo:
    CHANNEL_TYPES = ("discord", "email", "slack", "in_app")

    @staticmethod
    def list(project_id: str, *, enabled_only: bool = False) -> list[dict[str, Any]]:
        where = ["project_id = :p"]
        if enabled_only:
            where.append("enabled = 1")
        sql = (
            "SELECT channel_id, channel_type, name, target, config, events_filter,"
            " enabled, created_at, last_sent_at, last_error, send_count "
            "FROM dbo.notification_channels "
            f"WHERE {' AND '.join(where)} "
            "ORDER BY created_at DESC"
        )
        with session_scope() as s:
            rows = s.execute(text(sql), {"p": project_id}).fetchall()
        out = []
        for r in rows:
            try:
                cfg = json.loads(r[4]) if r[4] else None
            except Exception:
                cfg = None
            try:
                evf = json.loads(r[5]) if r[5] else None
            except Exception:
                evf = None
            out.append({
                "channel_id": int(r[0]), "channel_type": r[1],
                "name": r[2], "target": r[3], "config": cfg,
                "events_filter": evf, "enabled": bool(r[6]),
                "created_at": r[7].isoformat() if r[7] else None,
                "last_sent_at": r[8].isoformat() if r[8] else None,
                "last_error": r[9],
                "send_count": int(r[10]),
            })
        return out

    @staticmethod
    def upsert(*, project_id: str, channel_type: str, name: str, target: str,
               config: dict[str, Any] | None = None,
               events_filter: list[str] | None = None,
               enabled: bool = True,
               channel_id: int | None = None) -> int:
        if channel_type not in ChannelsRepo.CHANNEL_TYPES:
            raise ValueError(f"unknown channel_type {channel_type}")
        cfg_text = json.dumps(config) if config else None
        evf_text = json.dumps(events_filter) if events_filter else None
        with session_scope() as s:
            if channel_id:
                s.execute(text("""
                    UPDATE dbo.notification_channels
                    SET channel_type = :ct, name = :nm, target = :tg,
                        config = :cf, events_filter = :ef, enabled = :en
                    WHERE channel_id = :cid AND project_id = :p
                """), {"ct": channel_type, "nm": name, "tg": target,
                       "cf": cfg_text, "ef": evf_text,
                       "en": 1 if enabled else 0,
                       "cid": channel_id, "p": project_id})
                s.commit()
                return channel_id
            row = s.execute(text("""
                INSERT INTO dbo.notification_channels
                    (project_id, channel_type, name, target, config,
                     events_filter, enabled)
                OUTPUT INSERTED.channel_id
                VALUES (:p, :ct, :nm, :tg, :cf, :ef, :en)
            """), {"p": project_id, "ct": channel_type, "nm": name,
                   "tg": target, "cf": cfg_text, "ef": evf_text,
                   "en": 1 if enabled else 0}).fetchone()
            s.commit()
            return int(row[0])

    @staticmethod
    def delete(project_id: str, channel_id: int) -> None:
        with session_scope() as s:
            s.execute(text("""
                DELETE FROM dbo.notification_channels
                WHERE channel_id = :cid AND project_id = :p
            """), {"cid": channel_id, "p": project_id})
            s.commit()

    @staticmethod
    def record_send(channel_id: int, success: bool,
                    error: str | None = None) -> None:
        with session_scope() as s:
            s.execute(text("""
                UPDATE dbo.notification_channels
                SET last_sent_at = SYSUTCDATETIME(),
                    last_error = :err,
                    send_count = send_count + 1
                WHERE channel_id = :cid
            """), {"cid": channel_id, "err": (error or None) if not success else None})
            s.commit()


class NotificationsRepo:
    SEVERITIES = ("info", "warn", "error", "critical")

    @staticmethod
    def create(*, project_id: str, title: str, body: str | None = None,
               severity: str = "info", event_type: str | None = None,
               payload: dict[str, Any] | None = None,
               channel_id: int | None = None,
               status: str = "queued") -> int:
        if severity not in NotificationsRepo.SEVERITIES:
            severity = "info"
        payload_text = json.dumps(payload, default=str) if payload else None
        with session_scope() as s:
            row = s.execute(text("""
                INSERT INTO dbo.notifications
                    (project_id, channel_id, title, body, severity,
                     event_type, payload, status)
                OUTPUT INSERTED.notification_id
                VALUES (:p, :cid, :t, :b, :sv, :et, :pl, :st)
            """), {"p": project_id, "cid": channel_id, "t": title[:256],
                   "b": body, "sv": severity, "et": event_type,
                   "pl": payload_text, "st": status}).fetchone()
            s.commit()
            return int(row[0])

    @staticmethod
    def mark_sent(notification_id: int, ok: bool = True,
                  error: str | None = None) -> None:
        with session_scope() as s:
            s.execute(text("""
                UPDATE dbo.notifications
                SET status = :st, sent_at = SYSUTCDATETIME(),
                    error_message = :err
                WHERE notification_id = :nid
            """), {"st": "sent" if ok else "failed",
                   "err": error if not ok else None,
                   "nid": notification_id})
            s.commit()

    @staticmethod
    def list(project_id: str, *, limit: int = 50,
             unread_only: bool = False) -> list[dict[str, Any]]:
        where = ["project_id = :p"]
        params: dict[str, Any] = {"p": project_id, "lim": int(limit)}
        if unread_only:
            where.append("read_at IS NULL")
        sql = (
            "SELECT TOP (:lim) notification_id, channel_id, title, body,"
            " severity, event_type, payload, status, sent_at, read_at,"
            " created_at, error_message "
            "FROM dbo.notifications "
            f"WHERE {' AND '.join(where)} "
            "ORDER BY notification_id DESC"
        )
        with session_scope() as s:
            rows = s.execute(text(sql), params).fetchall()
        out = []
        for r in rows:
            try:
                pl = json.loads(r[6]) if r[6] else None
            except Exception:
                pl = None
            out.append({
                "notification_id": int(r[0]),
                "channel_id": int(r[1]) if r[1] else None,
                "title": r[2], "body": r[3], "severity": r[4],
                "event_type": r[5], "payload": pl, "status": r[7],
                "sent_at": r[8].isoformat() if r[8] else None,
                "read_at": r[9].isoformat() if r[9] else None,
                "created_at": r[10].isoformat() if r[10] else None,
                "error_message": r[11],
            })
        return out

    @staticmethod
    def unread_count(project_id: str) -> int:
        with session_scope() as s:
            row = s.execute(text("""
                SELECT COUNT(*) FROM dbo.notifications
                WHERE project_id = :p AND read_at IS NULL
            """), {"p": project_id}).fetchone()
        return int(row[0] or 0)

    @staticmethod
    def mark_read(project_id: str, ids: list[int] | None = None,
                  all_unread: bool = False) -> int:
        with session_scope() as s:
            if all_unread:
                row = s.execute(text("""
                    UPDATE dbo.notifications
                    SET read_at = SYSUTCDATETIME()
                    WHERE project_id = :p AND read_at IS NULL
                """), {"p": project_id})
                s.commit()
                return row.rowcount or 0
            if not ids:
                return 0
            n = 0
            for nid in ids:
                row = s.execute(text("""
                    UPDATE dbo.notifications
                    SET read_at = SYSUTCDATETIME()
                    WHERE notification_id = :nid AND project_id = :p
                      AND read_at IS NULL
                """), {"nid": int(nid), "p": project_id})
                n += row.rowcount or 0
            s.commit()
            return n
