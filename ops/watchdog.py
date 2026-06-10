"""External health watchdog for the Trader app.

Runs OUTSIDE the trader process (as a systemd timer, typically every ~3 min)
so it can detect — and recover from — failures the in-process monitors can't,
e.g. a wedged asyncio event loop that takes the web UI down while the
scheduler keeps running, or a worker/runner that has stopped cycling.

Why this exists: for weeks the only "monitor" was the operator checking the
dashboard each morning. These were silent failures (web-UI wedge, worker
stuck) with no external check and no alerting. This closes that gap.

Checks (auto-restart on failure 1 or 2; rate-limited):
  1. API up      — HTTP to the local app; a timeout / connection refused /
                   5xx means the server isn't serving (the wedge class).
  2. Heartbeat   — the worker logs a Worker.LOOP event every cycle (~2 min
                   active, 5 min when sleeping). No LOOP in >12 min => the
                   runner/loop is stuck or dead.

On any failure it restarts `trader`, re-checks, logs a Watchdog event to the
DB, and emails the operator (if SMTP is configured in AppSettings). With
`--daily` it instead emails a one-line health summary (green or red) so the
operator gets a proactive morning report instead of having to look.

SMTP config (AppSettings keys; email silently skipped if incomplete):
  alert_smtp_host, alert_smtp_port, alert_smtp_user, alert_smtp_password,
  alert_smtp_from, alert_smtp_to
"""
from __future__ import annotations

import json
import smtplib
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path

sys.path.insert(0, "/var/www/trader")

# Load the app's .env so DB credentials resolve when run under systemd
# (the trader service relies on load_dotenv(); we're a separate process).
try:
    from dotenv import load_dotenv
    load_dotenv("/var/www/trader/.env")
except Exception:
    pass

APP_URL = "http://127.0.0.1:8005/login"   # public route; any HTTP reply = up
HEARTBEAT_MAX_AGE_MIN = 12
API_TIMEOUT_SEC = 10
API_RETRIES = 2
STATE_FILE = Path("/var/tmp/trader_watchdog_state.json")
MIN_RESTART_INTERVAL_MIN = 15   # don't restart more than once per this window
RESTART_PASSWORD_HINT = "systemd runs this as root; no password needed"


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    try:
        STATE_FILE.write_text(json.dumps(state, default=str))
    except Exception:
        pass


def check_api() -> tuple[bool, str]:
    """True if the app answers HTTP. Any status code (even 401/403) means the
    server is serving; only a timeout / refused / 5xx is a real outage."""
    last = ""
    for _ in range(API_RETRIES):
        try:
            req = urllib.request.Request(APP_URL, method="GET")
            with urllib.request.urlopen(req, timeout=API_TIMEOUT_SEC) as r:
                code = r.status
                if code < 500:
                    return (True, f"HTTP {code}")
                last = f"HTTP {code}"
        except urllib.error.HTTPError as e:
            if e.code < 500:
                return (True, f"HTTP {e.code}")
            last = f"HTTP {e.code}"
        except Exception as e:
            last = f"{type(e).__name__}: {e}"
        time.sleep(2)
    return (False, last or "no response")


def check_heartbeat() -> tuple[bool, str]:
    """True if any active project logged a Worker.LOOP within the window."""
    try:
        from sqlalchemy import text
        from db.connection import session_scope
        with session_scope() as s:
            row = s.execute(text(
                "SELECT MAX(created_at) FROM agent_events "
                "WHERE node_name='Worker' AND event_type='LOOP'"
            )).fetchone()
    except Exception as e:
        # Can't reach the DB — report unknown, don't trigger a restart on it.
        return (True, f"heartbeat check skipped (db error: {e})")
    last = row[0] if row else None
    if last is None:
        return (False, "no Worker.LOOP events ever")
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    age_min = (_now() - last).total_seconds() / 60.0
    if age_min > HEARTBEAT_MAX_AGE_MIN:
        return (False, f"last LOOP {age_min:.0f} min ago")
    return (True, f"last LOOP {age_min:.1f} min ago")


def restart_trader() -> tuple[bool, str]:
    try:
        r = subprocess.run(
            ["systemctl", "restart", "trader"],
            capture_output=True, text=True, timeout=60,
        )
        if r.returncode == 0:
            return (True, "restarted")
        return (False, (r.stderr or r.stdout or "nonzero exit").strip())
    except Exception as e:
        return (False, str(e))


def _smtp_cfg() -> dict | None:
    try:
        from db.settings_store import AppSettings
        cfg = {
            "host": AppSettings.get("alert_smtp_host", None),
            "port": int(AppSettings.get("alert_smtp_port", 587) or 587),
            "user": AppSettings.get("alert_smtp_user", None),
            "password": AppSettings.get("alert_smtp_password", None),
            "from": AppSettings.get("alert_smtp_from", None),
            "to": AppSettings.get("alert_smtp_to", None),
        }
    except Exception:
        return None
    if not (cfg["host"] and cfg["from"] and cfg["to"]):
        return None
    return cfg


def send_email(subject: str, body: str) -> tuple[bool, str]:
    cfg = _smtp_cfg()
    if cfg is None:
        return (False, "smtp not configured")
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = cfg["from"]
    msg["To"] = cfg["to"]
    msg.set_content(body)
    try:
        if cfg["port"] == 465:
            with smtplib.SMTP_SSL(cfg["host"], cfg["port"], timeout=15) as s:
                if cfg["user"]:
                    s.login(cfg["user"], cfg["password"])
                s.send_message(msg)
        else:
            with smtplib.SMTP(cfg["host"], cfg["port"], timeout=15) as s:
                s.starttls()
                if cfg["user"]:
                    s.login(cfg["user"], cfg["password"])
                s.send_message(msg)
        return (True, "sent")
    except Exception as e:
        return (False, f"{type(e).__name__}: {e}")


def _log_event(node: str, etype: str, payload: dict) -> None:
    try:
        from db.repositories import EventsRepo, ProjectsRepo
        pid = None
        for p in ProjectsRepo.list_active():
            pid = p.project_id
            break
        if pid:
            EventsRepo.log(pid, node, etype, payload)
    except Exception:
        pass


def run_health() -> int:
    api_ok, api_msg = check_api()
    hb_ok, hb_msg = check_heartbeat()
    healthy = api_ok and hb_ok
    state = _load_state()

    if healthy:
        state["last_ok"] = _now().isoformat()
        _save_state(state)
        print(f"OK · api={api_msg} · heartbeat={hb_msg}")
        return 0

    problems = []
    if not api_ok:
        problems.append(f"API DOWN ({api_msg})")
    if not hb_ok:
        problems.append(f"WORKER STALE ({hb_msg})")
    problem_str = "; ".join(problems)
    print(f"UNHEALTHY · {problem_str}")

    # Rate-limit restarts.
    last_restart = state.get("last_restart")
    can_restart = True
    if last_restart:
        try:
            lr = datetime.fromisoformat(last_restart)
            if (_now() - lr) < timedelta(minutes=MIN_RESTART_INTERVAL_MIN):
                can_restart = False
        except Exception:
            pass

    action = "no restart (rate-limited)"
    if can_restart:
        ok, msg = restart_trader()
        action = f"restart: {msg}"
        state["last_restart"] = _now().isoformat()
        _save_state(state)
        time.sleep(12)
        api_ok2, api_msg2 = check_api()
        action += f" · post-restart api={api_msg2}"

    _log_event("Watchdog", "HEALTH_FAIL", {
        "problems": problems, "action": action,
        "narrative": [f"External watchdog detected: {problem_str}. {action}."],
    })
    ok, em = send_email(
        f"[Trader] ALERT: {problem_str}",
        f"The external watchdog detected a problem at {_now().isoformat()}.\n\n"
        f"Problems: {problem_str}\n"
        f"Action taken: {action}\n\n"
        f"This is an automated message from the Trader health watchdog.",
    )
    print(f"alert email: {em}")
    return 1


def run_daily() -> int:
    api_ok, api_msg = check_api()
    hb_ok, hb_msg = check_heartbeat()
    # Pull a quick net-P&L line per active project for the morning report.
    lines = []
    try:
        from db.repositories import ProjectsRepo
        from analytics.pnl_calculator import metrics_summary
        for p in ProjectsRepo.list_active():
            try:
                m = metrics_summary(p.project_id, period="all")
                lines.append(
                    f"  {p.project_id}: Net P&L "
                    f"${m.get('account_net_pnl')} "
                    f"({m.get('account_net_pnl_pct')}%)"
                )
            except Exception as e:
                lines.append(f"  {p.project_id}: (metrics error: {e})")
    except Exception:
        pass
    status = "GREEN" if (api_ok and hb_ok) else "RED"
    body = (
        f"Trader daily health — {status}\n\n"
        f"API: {api_msg}\nWorker heartbeat: {hb_msg}\n\n"
        f"Net P&L (broker-reconciled):\n" + "\n".join(lines) + "\n\n"
        f"Generated {_now().isoformat()} by the health watchdog."
    )
    print(body)
    ok, em = send_email(f"[Trader] Daily health: {status}", body)
    print(f"daily email: {em}")
    return 0


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "--daily":
        return run_daily()
    return run_health()


if __name__ == "__main__":
    sys.exit(main())
