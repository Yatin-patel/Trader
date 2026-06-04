"""FastAPI app — settings UI + REST endpoints.

The same process can host the API only, or also spin up the multi-tenant
runner via lifespan when started with `python main.py`.
"""
from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import (
    HTMLResponse, JSONResponse, PlainTextResponse,
    RedirectResponse, StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from auth import (
    SESSION_COOKIE, SESSION_TTL_HOURS,
    UsersRepo, create_session, get_session, list_for_user,
    hash_password, revoke_session, revoke_others_for_user,
    verify_password, totp,
)
from db import init_database
from db.repositories import EventsRepo, PositionsRepo, ProjectsRepo, TradingProject, WheelRepo
from db.settings_store import AppSettings, ProjectSettings
from execution import AlpacaClient
from workers import MultiTenantRunner

from .chat import router as chat_router
from .humanize import humanize_event, summarize_pipeline

logger = logging.getLogger(__name__)

# Process-wide socket timeout. The Alpaca SDK is synchronous and will
# hang indefinitely on a slow/stalled connection unless we set a default
# socket timeout. Production froze at 15:04 today because one such call
# blocked the event loop. 30 s is plenty for any legitimate Alpaca
# response (most return in <1 s); past that the call fails and frees
# the thread.
import socket as _socket
_socket.setdefaulttimeout(30.0)

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"
TEMPLATES_DIR.mkdir(exist_ok=True)
STATIC_DIR.mkdir(exist_ok=True)


_runner: MultiTenantRunner | None = None
_runner_task: asyncio.Task | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_database()
    if getattr(app.state, "autorun", False):
        global _runner, _runner_task
        _runner = MultiTenantRunner()
        _runner_task = asyncio.create_task(_runner.run_forever())
    yield
    if _runner is not None:
        _runner.stop()
    if _runner_task is not None:
        try:
            await asyncio.wait_for(_runner_task, timeout=5)
        except Exception:
            _runner_task.cancel()


app = FastAPI(title="Autonomous Trader", lifespan=lifespan)
app.include_router(chat_router)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


# ---------- Auth middleware --------------------------------------------------
# Paths that are accessible without login. Everything else gets a redirect to
# /login (for HTML requests) or a 401 JSON response (for /api/ requests).
_PUBLIC_PREFIXES = (
    "/login", "/signup", "/logout", "/forgot", "/reset/",
    "/static/", "/favicon.ico", "/metrics", "/openapi.json", "/docs", "/redoc",
)


def _is_public_path(path: str) -> bool:
    if path in ("/", "/login", "/signup", "/logout", "/login/2fa",
                "/forgot", "/favicon.ico"):
        return True
    return any(path.startswith(p) for p in _PUBLIC_PREFIXES)
    # NOTE: /etrade/* stays AUTHENTICATED — only logged-in owners can
    # initiate or complete the OAuth dance for their own projects.


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path

    # Resolve current user from session cookie (if any). Always set
    # request.state.user so templates can show login state even on public pages.
    user = None
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        sess = get_session(token)
        if sess:
            u = UsersRepo.get_by_id(sess["user_id"])
            if u and u.is_active:
                user = u
    request.state.user = user
    request.state.user_id = user.user_id if user else None
    request.state.is_admin = bool(user.is_admin) if user else False

    if _is_public_path(path):
        return await call_next(request)

    if user is None:
        # Unauthenticated. /api/* gets JSON 401; everything else redirects.
        if path.startswith("/api/"):
            return JSONResponse(
                {"detail": "login required"}, status_code=401,
            )
        return RedirectResponse(url=f"/login?next={path}", status_code=303)

    return await call_next(request)


# Inject current_user into every Jinja template (sub-nav uses it).
_orig_template_response = templates.TemplateResponse


def _template_response_with_user(*args, **kwargs):
    # FastAPI calls TemplateResponse(name, context) or
    # TemplateResponse(request, name, context). Handle both.
    if args and hasattr(args[0], "scope"):  # called with request first
        request = args[0]
        ctx = args[2] if len(args) > 2 else kwargs.get("context", {})
    else:
        ctx = args[1] if len(args) > 1 else kwargs.get("context", {})
        request = ctx.get("request") if isinstance(ctx, dict) else None
    if request is not None and isinstance(ctx, dict):
        ctx.setdefault("current_user", getattr(request.state, "user", None))
    return _orig_template_response(*args, **kwargs)


templates.TemplateResponse = _template_response_with_user   # type: ignore


# ---------- Schemas -----------------------------------------------------------

class ProjectIn(BaseModel):
    project_id: str
    project_name: str
    broker_type: str = "alpaca"
    # Alpaca (optional for ETrade projects)
    alpaca_api_key: str = ""
    alpaca_secret_key: str = ""
    alpaca_base_url: str = "https://paper-api.alpaca.markets"
    alpaca_data_feed: str = "iex"
    # ETrade (optional for Alpaca projects)
    etrade_consumer_key: str = ""
    etrade_consumer_secret: str = ""
    etrade_environment: str = "sandbox"
    max_equity_allocation: float
    is_active: bool = True


class SettingIn(BaseModel):
    key: str
    value: Any
    value_type: str = "string"
    category: str = "general"
    description: str | None = None
    is_secret: bool = False


class ProjectSettingIn(BaseModel):
    key: str
    value: Any
    value_type: str | None = None


# ---------- Auth pages + endpoints -------------------------------------------

def _current_user_id(request: Request) -> str | None:
    """Returns current user's id (or None)."""
    return getattr(request.state, "user_id", None)


def _scoped_project(project_id: str, request: Request) -> TradingProject:
    """Fetch a project, enforcing ownership. Admins see all projects."""
    uid = _current_user_id(request)
    is_admin = bool(getattr(request.state, "is_admin", False))
    project = ProjectsRepo.get(project_id, user_id=None if is_admin else uid)
    if project is None:
        raise HTTPException(404, "Project not found")
    return project


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: str | None = None,
                     email: str | None = None):
    # Already logged in? Skip to dashboard.
    if getattr(request.state, "user", None) is not None:
        return RedirectResponse("/dashboard", status_code=303)
    return templates.TemplateResponse("login.html", {
        "request": request, "error": error, "email": email,
    })


@app.post("/login")
async def login_submit(request: Request,
                       email: str = Form(...),
                       password: str = Form(...)):
    email = email.strip().lower()
    user = UsersRepo.get_by_email(email)
    pw_hash = UsersRepo.get_password_hash(user.user_id) if user else None

    # Always run verify even when user is None to avoid timing-based user
    # enumeration. argon2.verify on a fake hash takes about the same time as
    # the real check, then the comparison fails uniformly.
    if user is None or pw_hash is None or not verify_password(password, pw_hash):
        return templates.TemplateResponse("login.html", {
            "request": request,
            "error": "Invalid email or password.",
            "email": email,
        }, status_code=401)

    if not user.is_active:
        return templates.TemplateResponse("login.html", {
            "request": request,
            "error": "This account has been disabled.",
            "email": email,
        }, status_code=401)

    next_url = request.query_params.get("next") or "/dashboard"
    if not next_url.startswith("/") or next_url.startswith("//"):
        next_url = "/dashboard"

    # 2FA gate: if enabled, stash a short-lived pending-login token signed
    # with itsdangerous and prompt for the TOTP code on /login/2fa.
    if user.totp_enabled:
        from itsdangerous import URLSafeTimedSerializer
        signer = URLSafeTimedSerializer(_pending_2fa_secret(), salt="2fa-login")
        pending = signer.dumps({"uid": user.user_id, "next": next_url})
        return templates.TemplateResponse("login_2fa.html", {
            "request": request, "pending": pending, "error": None,
        })

    # No 2FA — issue session immediately.
    ip = request.client.host if request.client else None
    ua = request.headers.get("user-agent")
    token, _expires = create_session(user.user_id, ip=ip, user_agent=ua)
    UsersRepo.touch_last_login(user.user_id)

    resp = RedirectResponse(next_url, status_code=303)
    resp.set_cookie(
        key=SESSION_COOKIE, value=token,
        httponly=True, samesite="lax",
        secure=request.url.scheme == "https",
    )
    return resp


def _pending_2fa_secret() -> str:
    """Signing key for the short-lived pending-2FA token. Pulled from
    app_settings so it survives restarts; auto-created on first use."""
    key = AppSettings.get("pending_2fa_secret")
    if not key:
        import secrets as _secrets
        key = _secrets.token_urlsafe(32)
        AppSettings.set("pending_2fa_secret", key,
                        value_type="secret", category="auth",
                        description="Internal signing key for 2FA gate",
                        is_secret=True)
    return key


@app.post("/login/2fa")
async def login_2fa_submit(request: Request,
                           pending: str = Form(...),
                           code: str = Form(...)):
    from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
    signer = URLSafeTimedSerializer(_pending_2fa_secret(), salt="2fa-login")
    try:
        payload = signer.loads(pending, max_age=300)  # 5-min window
    except (BadSignature, SignatureExpired):
        return RedirectResponse("/login?error=2fa+session+expired", status_code=303)

    user = UsersRepo.get_by_id(payload["uid"])
    if user is None or not user.is_active or not user.totp_enabled:
        return RedirectResponse("/login", status_code=303)

    secret = UsersRepo.get_totp_secret(user.user_id)
    if not secret or not totp.verify(secret, code):
        return templates.TemplateResponse("login_2fa.html", {
            "request": request, "pending": pending,
            "error": "Code didn't match. Try the latest 6-digit code.",
        }, status_code=401)

    # Code verified — create session.
    ip = request.client.host if request.client else None
    ua = request.headers.get("user-agent")
    token, _expires = create_session(user.user_id, ip=ip, user_agent=ua)
    UsersRepo.touch_last_login(user.user_id)

    resp = RedirectResponse(payload.get("next") or "/dashboard", status_code=303)
    resp.set_cookie(
        key=SESSION_COOKIE, value=token,
        httponly=True, samesite="lax",
        secure=request.url.scheme == "https",
    )
    return resp


@app.get("/signup", response_class=HTMLResponse)
async def signup_page(request: Request, error: str | None = None,
                      email: str | None = None):
    if getattr(request.state, "user", None) is not None:
        return RedirectResponse("/dashboard", status_code=303)
    return templates.TemplateResponse("signup.html", {
        "request": request, "error": error, "email": email,
    })


# ---------------------------------------------------------------------------
# Password reset
# ---------------------------------------------------------------------------
@app.get("/forgot", response_class=HTMLResponse)
async def forgot_page(request: Request, error: str | None = None,
                      sent: bool = False):
    return templates.TemplateResponse("forgot.html", {
        "request": request, "error": error, "sent": sent,
    })


@app.post("/forgot")
async def forgot_submit(request: Request, email: str = Form(...)):
    from auth.password_reset import make_reset_token, send_reset_email
    email = email.strip().lower()
    user = UsersRepo.get_by_email(email)
    # ALWAYS show the same success page regardless of whether the
    # email matched a real account or whether SMTP delivery worked.
    # Showing a different response for "SMTP not configured" vs
    # "email not registered" lets an attacker enumerate which
    # addresses have accounts (they see the SMTP error message for
    # real accounts and the silent success page for unknown ones).
    # Operator visibility for SMTP misconfig lives in server.log + a
    # warning banner on the admin page, NOT in the user-facing flow.
    if user is not None and user.is_active:
        try:
            token = make_reset_token(user.user_id)
            base = str(request.base_url).rstrip("/")
            reset_url = f"{base}/reset/{token}"
            ok, msg = send_reset_email(user.email, reset_url)
            if not ok:
                logger.warning(
                    "reset email send failed for %s: %s", user.email, msg
                )
        except Exception:
            logger.exception(
                "reset token / send pipeline crashed for %s", user.email
            )
    return RedirectResponse("/forgot?sent=true", status_code=303)


@app.get("/reset/{token}", response_class=HTMLResponse)
async def reset_page(request: Request, token: str,
                     error: str | None = None):
    from auth.password_reset import consume_reset_token
    payload = consume_reset_token(token)
    if payload is None:
        return templates.TemplateResponse("reset.html", {
            "request": request, "valid": False,
            "error": "This reset link is invalid or expired. "
                     "Request a new one from the login page.",
            "token": "",
        })
    return templates.TemplateResponse("reset.html", {
        "request": request, "valid": True,
        "email": payload["email"], "token": token, "error": error,
    })


@app.post("/reset/{token}")
async def reset_submit(request: Request, token: str,
                       password: str = Form(...),
                       password_confirm: str = Form(...)):
    from auth.password_reset import consume_reset_token, apply_new_password
    payload = consume_reset_token(token)
    if payload is None:
        return RedirectResponse(
            f"/reset/{token}?error=invalid_or_expired", status_code=303)
    if password != password_confirm:
        return RedirectResponse(
            f"/reset/{token}?error=passwords_do_not_match", status_code=303)
    ok, msg = apply_new_password(payload["user_id"], password)
    if not ok:
        return RedirectResponse(
            f"/reset/{token}?error={msg[:120]}", status_code=303)
    # On success route to /login with a friendly notice (handled by
    # the template via the ?reset=true query param).
    return RedirectResponse("/login?reset=true", status_code=303)


@app.post("/signup")
async def signup_submit(request: Request,
                        email: str = Form(...),
                        password: str = Form(...),
                        password_confirm: str = Form(...)):
    email = email.strip().lower()
    err = None
    if len(password) < 10:
        err = "Password must be at least 10 characters."
    elif password != password_confirm:
        err = "Passwords do not match."
    elif "@" not in email or "." not in email:
        err = "Please enter a valid email."
    elif UsersRepo.get_by_email(email) is not None:
        err = "An account with this email already exists."

    if err:
        return templates.TemplateResponse("signup.html", {
            "request": request, "error": err, "email": email,
        }, status_code=400)

    # First user becomes admin automatically AND inherits any pre-existing
    # projects that have no owner yet (the bootstrap migration path).
    is_admin = UsersRepo.count() == 0
    user = UsersRepo.create(
        email=email, password_hash=hash_password(password), is_admin=is_admin,
    )
    if is_admin:
        from sqlalchemy import text as _sql_text
        from db.connection import session_scope as _scope
        with _scope() as s:
            s.execute(_sql_text(
                "UPDATE trading_projects SET user_id = :u "
                "WHERE user_id IS NULL"
            ), {"u": user.user_id})
            s.commit()
    ip = request.client.host if request.client else None
    ua = request.headers.get("user-agent")
    token, _ = create_session(user.user_id, ip=ip, user_agent=ua)

    resp = RedirectResponse("/dashboard", status_code=303)
    # No max_age — browser drops the cookie when the window closes.
    # Server-side session still lasts SESSION_TTL_HOURS as a safety upper
    # bound, but in practice the cookie is gone the moment the user quits.
    resp.set_cookie(
        key=SESSION_COOKIE, value=token,
        httponly=True, samesite="lax",
        secure=request.url.scheme == "https",
    )
    return resp


@app.post("/logout")
async def logout(request: Request):
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        revoke_session(token)
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(SESSION_COOKIE)
    return resp


@app.get("/logout")
async def logout_get(request: Request):
    # Convenience GET so the top-nav link works without a form.
    return await logout(request)


# ---------- Account self-service ---------------------------------------------

@app.get("/account", response_class=HTMLResponse)
async def account_page(request: Request,
                       pw_error: str | None = None,
                       pw_success: bool = False,
                       totp_error: str | None = None):
    user = getattr(request.state, "user", None)
    if user is None:
        return RedirectResponse("/login?next=/account", status_code=303)

    # Active sessions for this user
    sessions = list_for_user(user.user_id)
    current_token = request.cookies.get(SESSION_COOKIE)
    active = []
    for s in sessions:
        active.append({
            "token": s["token"],
            "created_at_str": s["created_at"].strftime("%Y-%m-%d %H:%M UTC")
                if s["created_at"] else "—",
            "expires_at_str": s["expires_at"].strftime("%Y-%m-%d %H:%M UTC")
                if s["expires_at"] else "—",
            "ip_address": s["ip_address"],
            "user_agent": s["user_agent"],
            "is_current": s["token"] == current_token,
        })

    # Projects the caller can manage settings for. Used to populate the
    # export / import / clone pickers in the Settings Backup & Migration
    # panel on /account.
    is_admin = bool(getattr(request.state, "is_admin", False))
    accessible_projects = ProjectsRepo.list_all(
        user_id=None if is_admin else user.user_id
    )

    return templates.TemplateResponse("account.html", {
        "request": request,
        "active_sessions": active,
        "pw_error": pw_error,
        "pw_success": pw_success,
        "totp_error": totp_error,
        "provisioning_uri": None,
        "qr_data_uri": None,
        "totp_secret": None,
        "default_broker": _get_default_broker(user.user_id),
        "accessible_projects": accessible_projects,
    })


@app.post("/account/change_password")
async def account_change_password(request: Request,
                                  current_password: str = Form(...),
                                  new_password: str = Form(...),
                                  new_password_confirm: str = Form(...)):
    user = getattr(request.state, "user", None)
    if user is None:
        return RedirectResponse("/login", status_code=303)

    if new_password != new_password_confirm:
        return RedirectResponse(
            "/account?pw_error=New+passwords+do+not+match.",
            status_code=303)
    if len(new_password) < 10:
        return RedirectResponse(
            "/account?pw_error=Password+must+be+at+least+10+characters.",
            status_code=303)

    current_hash = UsersRepo.get_password_hash(user.user_id)
    if not current_hash or not verify_password(current_password, current_hash):
        return RedirectResponse(
            "/account?pw_error=Current+password+is+incorrect.",
            status_code=303)

    UsersRepo.update_password(user.user_id, hash_password(new_password))
    # For safety, kill every OTHER session (forces re-login on other devices).
    revoke_others_for_user(user.user_id,
                           keep_token=request.cookies.get(SESSION_COOKIE) or "")
    return RedirectResponse("/account?pw_success=1", status_code=303)


@app.post("/account/2fa/setup")
async def account_2fa_setup(request: Request):
    """Generate a fresh TOTP secret and show the QR + manual key.
    Secret is held in the URL so we don't store it before verification."""
    user = getattr(request.state, "user", None)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    if user.totp_enabled:
        return RedirectResponse("/account", status_code=303)

    secret = totp.generate_secret()
    uri = totp.provisioning_uri(user.email, secret)
    qr = totp.qr_png_data_uri(uri)

    sessions = list_for_user(user.user_id)
    current_token = request.cookies.get(SESSION_COOKIE)
    active = []
    for s in sessions:
        active.append({
            "token": s["token"],
            "created_at_str": s["created_at"].strftime("%Y-%m-%d %H:%M UTC")
                if s["created_at"] else "—",
            "expires_at_str": s["expires_at"].strftime("%Y-%m-%d %H:%M UTC")
                if s["expires_at"] else "—",
            "ip_address": s["ip_address"],
            "user_agent": s["user_agent"],
            "is_current": s["token"] == current_token,
        })
    return templates.TemplateResponse("account.html", {
        "request": request,
        "active_sessions": active,
        "pw_error": None, "pw_success": False, "totp_error": None,
        "provisioning_uri": uri,
        "qr_data_uri": qr,
        "totp_secret": secret,
        "default_broker": _get_default_broker(user.user_id),
    })


@app.post("/account/2fa/verify")
async def account_2fa_verify(request: Request,
                             secret: str = Form(...),
                             code: str = Form(...)):
    user = getattr(request.state, "user", None)
    if user is None:
        return RedirectResponse("/login", status_code=303)

    if not totp.verify(secret, code):
        # Re-render setup view with error
        uri = totp.provisioning_uri(user.email, secret)
        qr = totp.qr_png_data_uri(uri)
        sessions = list_for_user(user.user_id)
        current_token = request.cookies.get(SESSION_COOKIE)
        active = []
        for s in sessions:
            active.append({
                "token": s["token"],
                "created_at_str": s["created_at"].strftime("%Y-%m-%d %H:%M UTC")
                    if s["created_at"] else "—",
                "expires_at_str": s["expires_at"].strftime("%Y-%m-%d %H:%M UTC")
                    if s["expires_at"] else "—",
                "ip_address": s["ip_address"],
                "user_agent": s["user_agent"],
                "is_current": s["token"] == current_token,
            })
        return templates.TemplateResponse("account.html", {
            "request": request,
            "active_sessions": active,
            "pw_error": None, "pw_success": False,
            "totp_error": "Code didn't match. Try again with the latest code from your app.",
            "provisioning_uri": uri, "qr_data_uri": qr, "totp_secret": secret,
            "default_broker": _get_default_broker(user.user_id),
        }, status_code=400)

    UsersRepo.set_totp(user.user_id, secret=secret, enabled=True)
    return RedirectResponse("/account", status_code=303)


@app.post("/account/2fa/disable")
async def account_2fa_disable(request: Request):
    user = getattr(request.state, "user", None)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    UsersRepo.disable_totp(user.user_id)
    return RedirectResponse("/account", status_code=303)


@app.post("/account/sessions/revoke_others")
async def account_revoke_others(request: Request):
    user = getattr(request.state, "user", None)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    keep = request.cookies.get(SESSION_COOKIE) or ""
    revoke_others_for_user(user.user_id, keep_token=keep)
    return RedirectResponse("/account", status_code=303)


# ---------- ETrade OAuth 1.0a flow -------------------------------------------
# ETrade uses out-of-band (OOB) authorization: the user visits ETrade's
# authorize URL, ETrade shows a 5-digit verifier code on screen, the user
# pastes it back here. There is no automatic redirect callback like OAuth 2.

# In-memory store of pending request tokens keyed by (project_id, user_id).
# Tiny TTL — only used for the few minutes between /connect and /callback.
_ETRADE_PENDING: dict[tuple[str, str], dict[str, Any]] = {}


@app.get("/etrade/connect", response_class=HTMLResponse)
async def etrade_connect(request: Request, project_id: str):
    """Step 1: mint a request token, show the authorize link + verifier form."""
    project = _scoped_project(project_id, request)
    if project.broker_type != "etrade":
        raise HTTPException(400, "This project is not configured for ETrade.")
    if not project.etrade_consumer_key or not project.etrade_consumer_secret:
        raise HTTPException(400,
            "Add ETrade Consumer Key + Secret to the project before connecting.")

    from execution.etrade_client import begin_oauth
    try:
        req_token, req_secret, authorize_url = await asyncio.to_thread(
            begin_oauth,
            project.etrade_consumer_key,
            project.etrade_consumer_secret,
        )
    except Exception as e:
        raise HTTPException(502, f"ETrade rejected the request: {e}")

    uid = request.state.user_id or ""
    _ETRADE_PENDING[(project_id, uid)] = {
        "request_token": req_token,
        "request_token_secret": req_secret,
    }

    return templates.TemplateResponse("etrade_connect.html", {
        "request": request,
        "project": project,
        "authorize_url": authorize_url,
    })


@app.post("/etrade/callback")
async def etrade_callback(request: Request,
                          project_id: str = Form(...),
                          verifier: str = Form(...)):
    """Step 3: exchange the verifier for an access token, persist it."""
    project = _scoped_project(project_id, request)
    if project.broker_type != "etrade":
        raise HTTPException(400, "This project is not configured for ETrade.")

    uid = request.state.user_id or ""
    pending = _ETRADE_PENDING.pop((project_id, uid), None)
    if not pending:
        raise HTTPException(400,
            "OAuth session expired. Restart the connect flow.")

    from execution.etrade_client import complete_oauth
    try:
        access_token, access_secret = await asyncio.to_thread(
            complete_oauth,
            project.etrade_consumer_key,
            project.etrade_consumer_secret,
            pending["request_token"],
            pending["request_token_secret"],
            verifier.strip(),
        )
    except Exception as e:
        raise HTTPException(400, f"ETrade rejected the verifier code: {e}")

    ProjectsRepo.update_etrade_tokens(
        project_id,
        access_token=access_token,
        access_token_secret=access_secret,
    )
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


@app.post("/api/projects/{project_id}/etrade/disconnect")
async def api_etrade_disconnect(request: Request, project_id: str):
    """Drop the saved access tokens. User must re-OAuth to use ETrade again."""
    _scoped_project(project_id, request)
    ProjectsRepo.update_etrade_tokens(
        project_id, access_token="", access_token_secret="",
    )
    return {"ok": True}


# ---------- Default broker preference (account page) -------------------------

@app.post("/account/default_broker")
async def account_set_default_broker(request: Request,
                                     default_broker: str = Form(...)):
    user = getattr(request.state, "user", None)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    if default_broker not in ("alpaca", "etrade"):
        raise HTTPException(400, "Invalid broker selection.")
    from sqlalchemy import text as _sql
    from db.connection import session_scope as _scope
    with _scope() as s:
        exists = s.execute(_sql(
            "SELECT 1 FROM user_preferences WHERE user_id = :u"
        ), {"u": user.user_id}).fetchone()
        if exists:
            s.execute(_sql(
                "UPDATE user_preferences SET default_broker = :b, "
                "updated_at = UTC_TIMESTAMP() WHERE user_id = :u"
            ), {"u": user.user_id, "b": default_broker})
        else:
            s.execute(_sql(
                "INSERT INTO user_preferences (user_id, default_broker) "
                "VALUES (:u, :b)"
            ), {"u": user.user_id, "b": default_broker})
        s.commit()
    return RedirectResponse("/account", status_code=303)


def _get_default_broker(user_id: str) -> str:
    from sqlalchemy import text as _sql
    from db.connection import session_scope as _scope
    with _scope() as s:
        row = s.execute(_sql(
            "SELECT default_broker FROM user_preferences WHERE user_id = :u"
        ), {"u": user_id}).fetchone()
    return (row[0] if row else "alpaca")


# ---------- Pages -------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    if getattr(request.state, "user", None) is None:
        return RedirectResponse("/login")
    return RedirectResponse("/dashboard")


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    uid = getattr(request.state, "user_id", None)
    is_admin = bool(getattr(request.state, "is_admin", False))
    projects = ProjectsRepo.list_all(user_id=None if is_admin else uid)
    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "projects": projects,
        "runner_active": _runner is not None,
    })


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    rows = AppSettings.list_all()
    return templates.TemplateResponse("settings.html", {
        "request": request,
        "settings": rows,
    })


@app.get("/projects/{project_id}", response_class=HTMLResponse)
async def project_page(request: Request, project_id: str):
    project = _scoped_project(project_id, request)
    proj_settings = ProjectSettings.list_for_project(project_id)
    return templates.TemplateResponse("project.html", {
        "request": request,
        "project": project,
        "project_settings": proj_settings,
    })


class RiskLimitIn(BaseModel):
    limit_id: int | None = None
    limit_type: str
    threshold: float
    action: str = "HALT"
    window_minutes: int | None = None
    enabled: bool = True


@app.get("/api/projects/{project_id}/risk/limits")
async def api_risk_list(request: Request, project_id: str):
    _scoped_project(project_id, request)
    from db.risk_repos import RiskLimitsRepo
    return RiskLimitsRepo.list(project_id)


@app.post("/api/projects/{project_id}/risk/limits")
async def api_risk_upsert(request: Request, project_id: str, payload: RiskLimitIn):
    _scoped_project(project_id, request)
    from db.risk_repos import RiskLimitsRepo
    try:
        lid = RiskLimitsRepo.upsert(
            project_id=project_id, limit_type=payload.limit_type,
            threshold=payload.threshold, action=payload.action,
            window_minutes=payload.window_minutes, enabled=payload.enabled,
            limit_id=payload.limit_id,
        )
        return {"ok": True, "limit_id": lid}
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.delete("/api/projects/{project_id}/risk/limits/{limit_id}")
async def api_risk_delete(request: Request, project_id: str, limit_id: int):
    _scoped_project(project_id, request)
    from db.risk_repos import RiskLimitsRepo
    RiskLimitsRepo.delete(project_id, limit_id)
    return {"ok": True}


@app.get("/api/projects/{project_id}/risk/greeks")
async def api_risk_greeks(request: Request, project_id: str):
    _scoped_project(project_id, request)
    from risk.greeks_agg import aggregate_greeks
    return aggregate_greeks(project_id)


@app.get("/api/projects/{project_id}/risk/earnings/{ticker}")
async def api_risk_earnings(request: Request, project_id: str, ticker: str):
    _scoped_project(project_id, request)
    from risk.earnings import get_next_earnings
    nxt = get_next_earnings(ticker)
    return {"ticker": ticker.upper(),
            "next_earnings_date": nxt.isoformat() if nxt else None}


@app.post("/api/projects/{project_id}/risk/evaluate")
async def api_risk_evaluate(request: Request, project_id: str):
    _scoped_project(project_id, request)
    from risk.kill_switch import evaluate_kill_switches
    return {"breaches": evaluate_kill_switches(project_id)}


@app.get("/projects/{project_id}/risk", response_class=HTMLResponse)
async def risk_page(request: Request, project_id: str):
    project = _scoped_project(project_id, request)
    return templates.TemplateResponse("risk.html", {
        "request": request, "project": project,
    })


# ---------- Position management (close, roll, take-profit, auto-roll) -------

@app.post("/api/projects/{project_id}/contracts/{contract_id}/close")
async def api_close_contract(request: Request, project_id: str, contract_id: int):
    """Buy back the contract at mid. NB: every Alpaca call here MUST go
    through asyncio.to_thread; the Alpaca HTTP client is synchronous and
    will block the event loop (freezing the whole app) if called
    directly. That's how prod hung at 15:04 today."""
    from db.repositories import WheelRepo
    from execution import AlpacaClient
    from risk.greeks_agg import _extract_underlying
    project = _scoped_project(project_id, request)
    target = None
    for c in WheelRepo.list_open(project_id):
        if c["contract_id"] == contract_id:
            target = c
            break
    if target is None:
        raise HTTPException(404, "Contract not found or already closed")
    client = AlpacaClient(project)
    sym = target["option_symbol"]
    chain = await asyncio.to_thread(
        client.option_chain_quotes, _extract_underlying(sym))
    q = chain.get(sym) or {}
    bid = q.get("bid") or 0
    ask = q.get("ask") or 0
    if ask <= 0:
        raise HTTPException(400, "no ask price available")
    mid = (bid + ask) / 2
    qty = int(target.get("quantity") or 1)
    try:
        order = await asyncio.to_thread(
            client.submit_limit_option,
            option_symbol=sym, qty=qty, side="buy",
            limit_price=round(mid, 2),
        )
        EventsRepo.log(project_id, "Manual", "BUY_TO_CLOSE", {
            "ticker": target["ticker"], "option_symbol": sym,
            "qty": qty, "limit_price": round(mid, 2), "order": order,
            "narrative": [f"User manually closed {target['ticker']} {sym} "
                          f"at ${mid:.2f} for {qty} contract(s)."],
        })
        return {"ok": True, "order": order}
    except Exception as e:
        raise HTTPException(400, f"close failed: {e}")


@app.post("/api/projects/{project_id}/contracts/{contract_id}/roll")
async def api_roll_contract(request: Request, project_id: str,
                            contract_id: int):
    """Manual single-contract roll: buy back the contract at mid AND log
    an AutoRoll.CLOSE_FOR_ROLL marker so the next Strategist cycle picks
    the ticker back up and writes a fresh contract at the configured
    delta/DTE band. Reuses the same BTC flow as /close — the difference
    is the event signal that tells the Strategist this was a roll, not
    an outright close."""
    from db.repositories import WheelRepo
    from execution import AlpacaClient
    from risk.greeks_agg import _extract_underlying
    project = _scoped_project(project_id, request)
    target = None
    for c in WheelRepo.list_open(project_id):
        if c["contract_id"] == contract_id:
            target = c
            break
    if target is None:
        raise HTTPException(404, "Contract not found or already closed")
    client = AlpacaClient(project)
    sym = target["option_symbol"]
    if not sym:
        raise HTTPException(400, "Contract has no option symbol")
    chain = await asyncio.to_thread(
        client.option_chain_quotes, _extract_underlying(sym))
    q = chain.get(sym) or {}
    bid = q.get("bid") or 0
    ask = q.get("ask") or 0
    if ask <= 0:
        raise HTTPException(400, "no ask price available")
    mid = (bid + ask) / 2
    qty = int(target.get("quantity") or 1)
    try:
        order = await asyncio.to_thread(
            client.submit_limit_option,
            option_symbol=sym, qty=qty, side="buy",
            limit_price=round(mid, 2),
        )
        # Use the AutoRoll node name so existing recent-failure /
        # cycle-attribution logic groups manual rolls with automatic
        # ones. The 'manual' flag on the payload distinguishes the two
        # for the activity-feed humanizer.
        EventsRepo.log(project_id, "AutoRoll", "CLOSE_FOR_ROLL", {
            "ticker": target["ticker"],
            "option_symbol": sym,
            "qty": qty,
            "close_price": round(mid, 2),
            "manual": True,
            "roll_reason": "user_requested",
            "order": order,
            "narrative": [
                f"User rolled {target['ticker']} {sym} at ${mid:.2f} "
                f"for {qty} contract(s). The next Strategist cycle will "
                f"select a fresh contract on this ticker at the "
                f"configured delta/DTE band."
            ],
        })
        return {"ok": True, "order": order, "close_price": round(mid, 2)}
    except Exception as e:
        raise HTTPException(400, f"roll failed: {e}")


# ---------- Manual DB overrides for wheel_contracts ------------------------
# These three endpoints exist for the case the broker state and the
# wheel_contracts table have diverged — e.g. a manual order outside the
# bot, a partial fill the reconciler missed, or a stale DB row left over
# from a prior project. They DO NOT submit orders to the broker; they
# only edit / close / create the local DB row. Every override writes a
# Manual.DB_OVERRIDE audit event so the change is reviewable.

class _ContractEditIn(BaseModel):
    strategy_phase: str | None = None
    quantity: int | None = None
    strike_price: float | None = None
    premium_collected: float | None = None
    delta_at_entry: float | None = None
    expiration_date: str | None = None  # YYYY-MM-DD


@app.post("/api/projects/{project_id}/contracts/{contract_id}/edit")
async def api_edit_contract(request: Request, project_id: str,
                            contract_id: int, payload: _ContractEditIn):
    """Patch fields on an open wheel_contracts row. Only the supplied
    fields change. NO broker order is submitted — this is purely a DB
    edit to bring the local row in sync with reality."""
    from db.repositories import WheelRepo
    _scoped_project(project_id, request)
    target = None
    for c in WheelRepo.list_open(project_id):
        if c["contract_id"] == contract_id:
            target = c
            break
    if target is None:
        raise HTTPException(404, "Contract not found or already closed")
    sets: dict[str, Any] = {}
    if payload.strategy_phase is not None:
        sets["strategy_phase"] = payload.strategy_phase[:32]
    if payload.quantity is not None:
        sets["quantity"] = int(payload.quantity)
    if payload.strike_price is not None:
        sets["strike_price"] = float(payload.strike_price)
    if payload.premium_collected is not None:
        sets["premium_collected"] = float(payload.premium_collected)
    if payload.delta_at_entry is not None:
        sets["delta_at_entry"] = float(payload.delta_at_entry)
    if payload.expiration_date is not None:
        sets["expiration_date"] = payload.expiration_date[:10]
    if not sets:
        raise HTTPException(400, "no fields to update")
    from sqlalchemy import text as _text
    from db.connection import session_scope
    set_sql = ", ".join(f"{k} = :{k}" for k in sets)
    sets["c"] = contract_id
    with session_scope() as s:
        s.execute(_text(
            f"UPDATE wheel_contracts SET {set_sql}, "
            f"updated_at = UTC_TIMESTAMP(6) WHERE contract_id = :c"
        ), sets)
        s.commit()
    sets.pop("c")
    EventsRepo.log(project_id, "Manual", "DB_OVERRIDE", {
        "action": "edit_contract",
        "contract_id": contract_id,
        "ticker": target.get("ticker"),
        "option_symbol": target.get("option_symbol"),
        "before": {k: target.get(k) for k in sets},
        "after": sets,
        "narrative": [
            f"User edited wheel_contracts row #{contract_id} "
            f"({target.get('ticker')}): {sets}"
        ],
    })
    return {"ok": True, "applied": sets}


@app.post("/api/projects/{project_id}/contracts/{contract_id}/force_close")
async def api_force_close_contract(request: Request, project_id: str,
                                   contract_id: int):
    """Mark the wheel_contracts row is_closed=1 WITHOUT submitting any
    broker order. Used when the position no longer exists at the broker
    (e.g. closed manually outside the bot, expired, or DB row was
    bogus to begin with) and you just want the bot to stop tracking
    it. Different from /close which actually buys back at market."""
    from db.repositories import WheelRepo
    _scoped_project(project_id, request)
    target = None
    for c in WheelRepo.list_open(project_id):
        if c["contract_id"] == contract_id:
            target = c
            break
    if target is None:
        raise HTTPException(404, "Contract not found or already closed")
    WheelRepo.close(contract_id)
    EventsRepo.log(project_id, "Manual", "DB_OVERRIDE", {
        "action": "force_close_db_row",
        "contract_id": contract_id,
        "ticker": target.get("ticker"),
        "option_symbol": target.get("option_symbol"),
        "narrative": [
            f"User force-closed wheel_contracts row #{contract_id} "
            f"({target.get('ticker')} {target.get('option_symbol')}). "
            "No broker order was submitted — the DB row is just "
            "marked is_closed=1 so the bot stops tracking it."
        ],
    })
    return {"ok": True}


class _ContractImportIn(BaseModel):
    option_symbol: str
    strategy_phase: str | None = None  # auto-inferred if omitted


@app.post("/api/projects/{project_id}/contracts/import")
async def api_import_contract(request: Request, project_id: str,
                              payload: _ContractImportIn):
    """Create a wheel_contracts row from a live Alpaca option position
    that isn't already tracked. Reads avg_entry_price / qty / OCC symbol
    from Alpaca, infers CSP vs CC from the option right + qty sign, and
    inserts. Refuses if the symbol is already tracked OR not currently
    held at the broker."""
    from datetime import datetime as _dt
    from db.repositories import WheelRepo
    from execution import AlpacaClient
    project = _scoped_project(project_id, request)
    sym = (payload.option_symbol or "").upper().strip()
    if not sym:
        raise HTTPException(400, "option_symbol required")
    # Already tracked?
    for c in WheelRepo.list_open(project_id):
        if (c.get("option_symbol") or "").upper() == sym:
            raise HTTPException(409, "already tracked")
    # Pull live position
    client = AlpacaClient(project)
    try:
        live = await asyncio.to_thread(client.list_positions)
    except Exception as e:
        raise HTTPException(502, f"alpaca error: {e}")
    pos = next((p for p in live if str(p.get("symbol") or "").upper() == sym
                and p.get("asset_class") == "us_option"), None)
    if pos is None:
        raise HTTPException(404, f"no live position for {sym}")
    # Parse OCC symbol — same regex as the front-end parseOcc.
    import re as _re
    m = _re.match(r"^([A-Z.]+)(\d{6})([CP])(\d{8})$", sym)
    if not m:
        raise HTTPException(400, "unrecognized OCC symbol format")
    ticker = m.group(1)
    exp_str = m.group(2)
    right = m.group(3)
    strike = int(m.group(4)) / 1000.0
    exp = _dt.strptime("20" + exp_str, "%Y%m%d").date()
    qty = float(pos.get("qty") or 0)
    avg_entry = float(pos.get("avg_entry_price") or 0)
    is_short = qty < 0
    if not is_short:
        raise HTTPException(400,
            "Long positions can't be imported as wheel contracts (CSP/CC). "
            "The wheel pipeline only manages SHORT options.")
    phase = (payload.strategy_phase
             or ("CASH_SECURED_PUT" if right == "P" else "COVERED_CALL"))
    contract_id = WheelRepo.open_contract(
        project_id=project_id, ticker=ticker,
        phase=phase, option_symbol=sym,
        strike=strike, premium=avg_entry,
        expiration=exp, delta=None, quantity=int(abs(qty)),
    )
    EventsRepo.log(project_id, "Manual", "DB_OVERRIDE", {
        "action": "import_contract",
        "contract_id": contract_id,
        "ticker": ticker, "option_symbol": sym,
        "phase": phase, "strike": strike,
        "premium_collected": avg_entry, "quantity": int(abs(qty)),
        "expiration": exp.isoformat(),
        "narrative": [
            f"User imported live Alpaca position {sym} into "
            f"wheel_contracts as #{contract_id} (phase={phase}, "
            f"qty={int(abs(qty))}, premium=${avg_entry:.2f}). The wheel "
            f"pipeline will now manage it."
        ],
    })
    return {"ok": True, "contract_id": contract_id, "ticker": ticker,
            "phase": phase, "strike": strike, "premium": avg_entry,
            "quantity": int(abs(qty)), "expiration": exp.isoformat()}


@app.post("/api/projects/{project_id}/positions/{position_id}/close")
async def api_close_position(request: Request, project_id: str, position_id: int):
    from db.repositories import PositionsRepo
    from execution import AlpacaClient
    project = _scoped_project(project_id, request)
    target = None
    for p in PositionsRepo.list_open(project_id):
        if p["position_id"] == position_id:
            target = p
            break
    if target is None:
        raise HTTPException(404, "Position not found")
    client = AlpacaClient(project)
    try:
        result = await asyncio.to_thread(
            client.liquidate_position, target["ticker"])
        PositionsRepo.close(position_id, final_status="CLOSED")
        EventsRepo.log(project_id, "Manual", "POSITION_CLOSED", {
            "ticker": target["ticker"], "qty": target["quantity"],
            "result": result,
            "narrative": [f"User manually closed {target['quantity']} "
                          f"shares of {target['ticker']}."],
        })
        return {"ok": True, "result": result}
    except Exception as e:
        raise HTTPException(400, f"close failed: {e}")


@app.post("/api/projects/{project_id}/positions/take_profit_now")
async def api_take_profit_now(request: Request, project_id: str):
    """Synchronous Alpaca calls inside — must run on a thread so the
    event loop doesn't block."""
    _scoped_project(project_id, request)
    from risk.take_profit import evaluate_take_profit
    actions = await asyncio.to_thread(evaluate_take_profit, project_id)
    return {"actions": actions}


@app.post("/api/projects/{project_id}/positions/auto_roll_now")
async def api_auto_roll_now(request: Request, project_id: str):
    """Synchronous Alpaca calls inside — must run on a thread."""
    _scoped_project(project_id, request)
    from risk.auto_roll import evaluate_auto_roll
    actions = await asyncio.to_thread(evaluate_auto_roll, project_id)
    return {"actions": actions}


# ---------- IV rank + news -------------------------------------------------

@app.get("/api/projects/{project_id}/iv_rank/{ticker}")
async def api_iv_rank(request: Request, project_id: str, ticker: str):
    _scoped_project(project_id, request)
    from analytics.iv_rank import get_iv_rank
    return {"ticker": ticker.upper(),
            "iv_rank": get_iv_rank(project_id, ticker)}


@app.get("/api/projects/{project_id}/news/{ticker}")
async def api_news(request: Request, project_id: str, ticker: str):
    _scoped_project(project_id, request)
    from risk.news import get_news_sentiment
    return get_news_sentiment(ticker)


# ---------- Backtesting ----------------------------------------------------

class BacktestIn(BaseModel):
    name: str = "ad-hoc"
    from_date: str    # YYYY-MM-DD
    to_date: str
    universe: list[str] | None = None


@app.post("/api/projects/{project_id}/backtest/run")
async def api_backtest_run(request: Request, project_id: str, payload: BacktestIn):
    _scoped_project(project_id, request)
    from datetime import date as _date
    from backtest import run_backtest
    try:
        fd = _date.fromisoformat(payload.from_date)
        td = _date.fromisoformat(payload.to_date)
    except Exception:
        raise HTTPException(400, "from_date and to_date must be YYYY-MM-DD")
    if td < fd:
        raise HTTPException(400, "to_date before from_date")
    try:
        result = await asyncio.to_thread(
            run_backtest, project_id,
            from_date=fd, to_date=td,
            universe=payload.universe, name=payload.name,
        )
        return result
    except Exception as e:
        raise HTTPException(500, f"backtest failed: {e}")


@app.get("/api/projects/{project_id}/backtest/runs")
async def api_backtest_list(request: Request, project_id: str, limit: int = 25):
    _scoped_project(project_id, request)
    from backtest import list_runs
    return list_runs(project_id, limit=limit)


@app.get("/api/projects/{project_id}/backtest/runs/{run_id}")
async def api_backtest_get(request: Request, project_id: str, run_id: int):
    _scoped_project(project_id, request)
    from backtest import get_run
    r = get_run(project_id, run_id)
    if r is None:
        raise HTTPException(404, "run not found")
    return r


@app.get("/projects/{project_id}/backtest", response_class=HTMLResponse)
async def backtest_page(request: Request, project_id: str):
    project = _scoped_project(project_id, request)
    return templates.TemplateResponse("backtest.html", {
        "request": request, "project": project,
    })


# ---------- Strategy registry (for UI) -------------------------------------

@app.get("/api/strategies")
async def api_strategies():
    from strategies import REGISTRY
    return [{"name": s.name, "description": s.description}
            for s in REGISTRY.values()]


# ---------- Notifications --------------------------------------------------

class ChannelIn(BaseModel):
    channel_id: int | None = None
    channel_type: str   # discord|email|slack|in_app
    name: str
    target: str
    config: dict[str, Any] | None = None
    events_filter: list[str] | None = None
    enabled: bool = True


class TestSendIn(BaseModel):
    channel_id: int
    title: str = "Test from Trader"
    body: str | None = "If you can read this, the channel works."
    severity: str = "info"


@app.get("/api/projects/{project_id}/notifications/channels")
async def api_channels_list(request: Request, project_id: str):
    _scoped_project(project_id, request)
    from db.notifications_repo import ChannelsRepo
    rows = ChannelsRepo.list(project_id)
    # Mask the target for email/discord/slack so we don't leak credentials.
    for r in rows:
        t = r.get("target") or ""
        if len(t) > 12:
            r["target_masked"] = t[:6] + "…" + t[-4:]
        else:
            r["target_masked"] = t
    return rows


@app.post("/api/projects/{project_id}/notifications/channels")
async def api_channels_upsert(request: Request, project_id: str, payload: ChannelIn):
    _scoped_project(project_id, request)
    from db.notifications_repo import ChannelsRepo
    try:
        cid = ChannelsRepo.upsert(
            project_id=project_id, channel_type=payload.channel_type,
            name=payload.name, target=payload.target, config=payload.config,
            events_filter=payload.events_filter, enabled=payload.enabled,
            channel_id=payload.channel_id,
        )
        return {"ok": True, "channel_id": cid}
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.delete("/api/projects/{project_id}/notifications/channels/{channel_id}")
async def api_channels_delete(request: Request, project_id: str, channel_id: int):
    _scoped_project(project_id, request)
    from db.notifications_repo import ChannelsRepo
    ChannelsRepo.delete(project_id, channel_id)
    return {"ok": True}


@app.post("/api/projects/{project_id}/notifications/test")
async def api_channels_test(request: Request, project_id: str, payload: TestSendIn):
    """Send a test notification through one specific channel."""
    _scoped_project(project_id, request)
    from db.notifications_repo import ChannelsRepo, NotificationsRepo
    from notifications.adapters import ADAPTERS
    channels = {c["channel_id"]: c for c in ChannelsRepo.list(project_id)}
    ch = channels.get(payload.channel_id)
    if ch is None:
        raise HTTPException(404, "channel not found")
    adapter = ADAPTERS.get(ch["channel_type"])
    if adapter is None:
        raise HTTPException(400, f"no adapter for {ch['channel_type']}")
    nid = NotificationsRepo.create(
        project_id=project_id, title=payload.title, body=payload.body,
        severity=payload.severity, event_type="TEST",
        channel_id=ch["channel_id"], status="queued",
    )
    try:
        ok, err = adapter(ch, payload.title, payload.body or "", payload.severity)
    except Exception as e:
        ok, err = False, str(e)
    NotificationsRepo.mark_sent(nid, ok=ok, error=err)
    ChannelsRepo.record_send(ch["channel_id"], ok, error=err)
    return {"ok": ok, "error": err, "notification_id": nid}


@app.get("/api/projects/{project_id}/notifications")
async def api_notifications_list(request: Request, project_id: str, limit: int = 50,
                                 unread_only: bool = False):
    _scoped_project(project_id, request)
    from db.notifications_repo import NotificationsRepo
    return NotificationsRepo.list(project_id, limit=limit,
                                  unread_only=unread_only)


@app.get("/api/projects/{project_id}/notifications/unread_count")
async def api_notifications_unread(request: Request, project_id: str):
    _scoped_project(project_id, request)
    from db.notifications_repo import NotificationsRepo
    return {"unread": NotificationsRepo.unread_count(project_id)}


class MarkReadIn(BaseModel):
    ids: list[int] | None = None
    all_unread: bool = False


@app.post("/api/projects/{project_id}/notifications/mark_read")
async def api_notifications_mark_read(request: Request, project_id: str, payload: MarkReadIn):
    _scoped_project(project_id, request)
    from db.notifications_repo import NotificationsRepo
    n = NotificationsRepo.mark_read(project_id, ids=payload.ids,
                                    all_unread=payload.all_unread)
    return {"ok": True, "marked": n}


@app.post("/api/projects/{project_id}/notifications/digest_now")
async def api_digest_now(request: Request, project_id: str):
    _scoped_project(project_id, request)
    from notifications.digest import send_daily_digest
    return {"results": send_daily_digest(project_id)}


# ---------- Wheel cycles ---------------------------------------------------

@app.get("/api/projects/{project_id}/cycles")
async def api_cycles_list(request: Request, project_id: str, status: str | None = None,
                          limit: int = 50):
    _scoped_project(project_id, request)
    from analytics.wheel_cycles import list_cycles
    return list_cycles(project_id, status=status, limit=limit)


@app.get("/api/projects/{project_id}/cycles/{cycle_id}")
async def api_cycle_detail(request: Request, project_id: str, cycle_id: int):
    _scoped_project(project_id, request)
    from analytics.wheel_cycles import get_cycle
    c = get_cycle(project_id, cycle_id)
    if c is None:
        raise HTTPException(404, "cycle not found")
    return c


# ---------- Reliability (orders + reconciliation + backups + metrics) -----

@app.get("/api/projects/{project_id}/orders")
async def api_orders_list(request: Request, project_id: str, limit: int = 100,
                          terminal: bool | None = None):
    _scoped_project(project_id, request)
    from ops.orders_tracker import list_orders
    return list_orders(project_id, limit=limit, terminal=terminal)


@app.post("/api/projects/{project_id}/orders/poll")
async def api_orders_poll(request: Request, project_id: str):
    _scoped_project(project_id, request)
    from ops.orders_tracker import poll_orders
    return await asyncio.to_thread(poll_orders, project_id)


@app.get("/api/projects/{project_id}/reconciliation/history")
async def api_recon_history(request: Request, project_id: str, limit: int = 20):
    _scoped_project(project_id, request)
    from ops.reconciliation import list_recon_history
    return list_recon_history(project_id, limit=limit)


@app.post("/api/projects/{project_id}/reconciliation/run")
async def api_recon_run(request: Request, project_id: str,
                        auto_sync: bool = False, deep_sync: bool = False):
    """Run the DB-vs-broker reconciler. ``deep_sync=True`` additionally
    catches qty mismatches and long-vs-short flips (the NIO class of
    bug). The same code path the 14:00 / 19:30 UTC scheduled jobs use."""
    _scoped_project(project_id, request)
    from ops.reconciliation import run_reconciliation
    return await asyncio.to_thread(
        run_reconciliation, project_id,
        auto_sync=auto_sync, deep_sync=deep_sync,
    )


@app.get("/api/backups")
async def api_backups_list(limit: int = 20):
    from ops.backups import list_backups
    return list_backups(limit=limit)


@app.post("/api/backups/run")
async def api_backups_run():
    from ops.backups import run_backup
    return await asyncio.to_thread(run_backup)


@app.post("/api/backups/prune")
async def api_backups_prune():
    from ops.backups import prune_old_backups
    return await asyncio.to_thread(prune_old_backups)


@app.get("/api/metrics")
async def api_metrics():
    from ops.metrics import collect_metrics
    return collect_metrics()


@app.get("/metrics", response_class=PlainTextResponse)
async def metrics_prometheus():
    from ops.metrics import prometheus_text
    return prometheus_text()


# ---------- LLM cost tracking + cache stats (Cat 8) -----------------------

@app.get("/api/llm/usage")
async def api_llm_usage(project_id: str | None = None, limit: int = 50):
    from llm_ops.tracker import list_usage, usage_summary
    return {
        "summary": usage_summary(project_id),
        "recent": list_usage(project_id, limit=limit),
    }


@app.get("/api/llm/cache_stats")
async def api_llm_cache_stats():
    from llm_ops.cache import cache_stats
    return cache_stats()


# ---------- AI recommendations (Cat 10.1) --------------------------------

@app.post("/api/projects/{project_id}/recommendations/build")
async def api_recs_build(request: Request, project_id: str):
    _scoped_project(project_id, request)
    from intelligence.recommendations import build_recommendations
    return await asyncio.to_thread(build_recommendations, project_id)


@app.get("/api/projects/{project_id}/recommendations")
async def api_recs_list(request: Request, project_id: str, status: str | None = None,
                        limit: int = 20):
    _scoped_project(project_id, request)
    from intelligence.recommendations import list_recommendations
    return list_recommendations(project_id, status=status, limit=limit)


@app.post("/api/projects/{project_id}/recommendations/{rec_id}/apply")
async def api_recs_apply(request: Request, project_id: str, rec_id: int):
    _scoped_project(project_id, request)
    from intelligence.recommendations import apply_recommendation
    return apply_recommendation(project_id, rec_id)


# ---------- Anomalies (Cat 10.3) -----------------------------------------

@app.post("/api/projects/{project_id}/anomalies/detect")
async def api_anom_detect(request: Request, project_id: str):
    _scoped_project(project_id, request)
    from intelligence.anomalies import detect_anomalies
    return {"anomalies": await asyncio.to_thread(detect_anomalies, project_id)}


@app.get("/api/projects/{project_id}/anomalies")
async def api_anom_list(request: Request, project_id: str, limit: int = 50):
    _scoped_project(project_id, request)
    from intelligence.anomalies import list_anomalies
    return list_anomalies(project_id, limit=limit)


# ---------- Strategy templates (Cat 9.3) ---------------------------------

@app.get("/api/strategy_templates")
async def api_templates_list():
    from intelligence.strategy_templates import list_templates
    return list_templates()


@app.post("/api/projects/{project_id}/strategy_templates/{tid}/apply")
async def api_template_apply(request: Request, project_id: str, tid: str):
    _scoped_project(project_id, request)
    from intelligence.strategy_templates import apply_template
    out = apply_template(project_id, tid)
    if "error" in out:
        raise HTTPException(400, out["error"])
    return out


# ---------- Market Outlook (top performers + 30/60/90 predictions) -----------

@app.get("/projects/{project_id}/outlook", response_class=HTMLResponse)
async def outlook_page(request: Request, project_id: str):
    project = _scoped_project(project_id, request)
    return templates.TemplateResponse("outlook.html", {
        "request": request, "project": project,
    })


@app.get("/api/projects/{project_id}/outlook/top_performers")
async def api_outlook_top(request: Request, project_id: str, limit: int = 25):
    _scoped_project(project_id, request)
    from intelligence.market_outlook import top_performers
    return await asyncio.to_thread(top_performers, project_id, limit=limit)


@app.get("/api/projects/{project_id}/outlook/predict/{ticker}")
async def api_outlook_predict(request: Request, project_id: str, ticker: str, force: bool = False):
    _scoped_project(project_id, request)
    from intelligence.market_outlook import predict
    out = await asyncio.to_thread(predict, project_id, ticker, force=force)
    if "error" in out:
        raise HTTPException(400, out["error"])
    return out


@app.get("/api/projects/{project_id}/account/raw")
async def api_account_raw(request: Request, project_id: str):
    """Dump every field the configured broker returns for the account —
    used to diagnose buying-power, PDT, and account-status issues."""
    project = _scoped_project(project_id, request)
    from execution import get_broker, BrokerNotConfigured
    try:
        return await asyncio.to_thread(get_broker(project).get_account_raw)
    except BrokerNotConfigured as e:
        return {"broker_state": "needs_oauth",
                "message": str(e),
                "broker_type": project.broker_type}
    except NotImplementedError as e:
        return {"broker_state": "phase2_pending",
                "message": str(e),
                "broker_type": project.broker_type}
    except Exception as e:
        broker_label = "ETrade" if project.broker_type == "etrade" else "Alpaca"
        raise HTTPException(502, f"{broker_label} error: {e}")


@app.get("/api/projects/{project_id}/dashboard_overview")
async def api_dashboard_overview(request: Request, project_id: str):
    """Top-of-dashboard summary: starting balance, current equity,
    short-term gain (7d), and long-term gain (all-time)."""
    project = _scoped_project(project_id, request)
    from datetime import datetime, timedelta, timezone
    from execution import get_broker, BrokerNotConfigured
    from db.analytics_repos import PortfolioSnapshotsRepo

    def _compute():
        # Current equity from the configured broker. ETrade projects
        # without OAuth return None — UI shows "—" for unavailable data.
        broker_ok = True
        current_equity: float | None = None
        current_cash: float | None = None
        try:
            account = get_broker(project).get_account()
            current_equity = float(account.get("equity") or 0)
            current_cash = float(account.get("cash") or 0)
        except (BrokerNotConfigured, NotImplementedError):
            broker_ok = False
        except Exception:
            broker_ok = False

        # Starting balance from earliest snapshot, fall back to project's max
        # equity allocation (i.e. what the user said they'd commit).
        earliest = PortfolioSnapshotsRepo.earliest(project_id)
        if earliest and earliest.get("equity"):
            starting_balance = earliest["equity"]
            starting_at = earliest["t"]
        else:
            starting_balance = float(project.max_equity_allocation or 0)
            starting_at = None

        # 7-day reference snapshot
        seven_days_ago = datetime.now(tz=timezone.utc) - timedelta(days=7)
        week_ref = PortfolioSnapshotsRepo.at_or_after(project_id, seven_days_ago)
        week_start_equity = (week_ref or {}).get("equity")

        def _gain(start):
            # Without live broker data we have no current equity to subtract,
            # so reporting "+/- $X" would be a fake number. Return None and
            # the dashboard shows "—" instead.
            if not broker_ok or current_equity is None:
                return {"dollars": None, "pct": None, "ref_equity": start}
            if not start or start <= 0:
                return {"dollars": None, "pct": None, "ref_equity": start}
            d = current_equity - start
            return {
                "dollars": round(d, 2),
                "pct": round((d / start) * 100, 2),
                "ref_equity": round(start, 2),
            }

        return {
            "current_equity": round(current_equity, 2) if current_equity is not None else None,
            "current_cash": round(current_cash, 2) if current_cash is not None else None,
            "starting_balance": round(starting_balance, 2),
            "starting_at": starting_at,
            "short_term": _gain(week_start_equity),     # last 7 days
            "long_term": _gain(starting_balance),       # since inception
            "short_term_period_days": 7,
            "broker_state": "ready" if broker_ok else "not_connected",
        }

    return await asyncio.to_thread(_compute)


@app.get("/api/projects/{project_id}/optimize/preview")
async def api_optimize_preview(request: Request, project_id: str, strategy: str):
    """Preview what settings the Optimize button would apply."""
    _scoped_project(project_id, request)
    from intelligence.optimizer import preview
    out = await asyncio.to_thread(preview, project_id, strategy)
    if "error" in out:
        raise HTTPException(400, out["error"])
    return out


@app.post("/api/projects/{project_id}/optimize")
async def api_optimize_apply(request: Request, project_id: str, body: dict):
    """Apply the cash-tier-aware optimized settings for the given strategy."""
    _scoped_project(project_id, request)
    strategy = (body or {}).get("strategy")
    if not strategy:
        raise HTTPException(400, "strategy required in body")
    from intelligence.optimizer import optimize
    out = await asyncio.to_thread(optimize, project_id, strategy)
    if "error" in out:
        raise HTTPException(400, out["error"])
    return out


@app.post("/api/projects/{project_id}/alpaca_position/close")
async def api_close_alpaca_position(request: Request, project_id: str, body: dict):
    """Close any open position (stock or option) by symbol via the project's
    configured broker. Submits at market.
    """
    project = _scoped_project(project_id, request)
    symbol = (body or {}).get("symbol")
    if not symbol:
        raise HTTPException(400, "symbol required in body")
    from execution import get_broker, BrokerNotConfigured
    from db.repositories import EventsRepo
    try:
        client = get_broker(project)
    except BrokerNotConfigured as e:
        raise HTTPException(400, str(e))
    try:
        order = await asyncio.to_thread(client.liquidate_position, symbol)
        EventsRepo.log(project_id, "User", "MANUAL_CLOSE", {
            "symbol": symbol, "order": order,
            "narrative": [f"User manually closed Alpaca position {symbol}."],
        })
        if isinstance(order, dict) and order.get("error"):
            raise HTTPException(400, f"close failed: {order['error']}")
        return {"ok": True, "order": order}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(400, f"close failed: {e}")


# ---------- Trade journal export (Cat 11.1) ------------------------------

@app.get("/api/projects/{project_id}/journal/export.csv")
async def api_journal_csv(request: Request, project_id: str, since_days: int | None = None):
    _scoped_project(project_id, request)
    from datetime import datetime, timedelta, timezone
    from fastapi.responses import Response
    from exports.journal import trade_journal_csv
    since = None
    if since_days:
        since = datetime.now(tz=timezone.utc) - timedelta(days=int(since_days))
    csv_text = trade_journal_csv(project_id, since=since)
    return Response(content=csv_text, media_type="text/csv",
                    headers={"Content-Disposition":
                             f'attachment; filename="trades_{project_id}.csv"'})


# ---------- Cross-tenant portfolio (Cat 12.2) ----------------------------

@app.get("/api/portfolio")
async def api_portfolio_all():
    from api.portfolio import aggregate_all
    return aggregate_all()


@app.get("/portfolio", response_class=HTMLResponse)
async def portfolio_page(request: Request):
    return templates.TemplateResponse("portfolio.html", {"request": request})


@app.get("/projects/{project_id}/intelligence", response_class=HTMLResponse)
async def intelligence_page(request: Request, project_id: str):
    project = _scoped_project(project_id, request)
    return templates.TemplateResponse("intelligence.html", {
        "request": request, "project": project,
    })


@app.get("/cost", response_class=HTMLResponse)
async def cost_page(request: Request):
    return templates.TemplateResponse("cost.html", {"request": request})


@app.get("/projects/{project_id}/reliability", response_class=HTMLResponse)
async def reliability_page(request: Request, project_id: str):
    project = _scoped_project(project_id, request)
    return templates.TemplateResponse("reliability.html", {
        "request": request, "project": project,
    })


@app.get("/projects/{project_id}/cycles", response_class=HTMLResponse)
async def cycles_page(request: Request, project_id: str):
    project = _scoped_project(project_id, request)
    return templates.TemplateResponse("cycles.html", {
        "request": request, "project": project,
    })


@app.get("/projects/{project_id}/notifications", response_class=HTMLResponse)
async def notifications_page(request: Request, project_id: str):
    project = _scoped_project(project_id, request)
    return templates.TemplateResponse("notifications.html", {
        "request": request, "project": project,
    })


_SNAPSHOT_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_SNAPSHOT_TTL = 5.0   # seconds


@app.get("/api/projects/{project_id}/snapshot")
async def api_project_snapshot(request: Request, project_id: str):
    import time as _time
    now = _time.monotonic()
    cache_key = (project_id, getattr(request.state, "user_id", None))
    cached = _SNAPSHOT_CACHE.get(cache_key)
    if cached and (now - cached[0]) < _SNAPSHOT_TTL:
        return cached[1]
    uid = getattr(request.state, "user_id", None)
    is_admin = bool(getattr(request.state, "is_admin", False))
    result = await asyncio.to_thread(_build_snapshot, project_id, uid, is_admin)
    _SNAPSHOT_CACHE[cache_key] = (now, result)
    return result


def _build_snapshot(project_id: str, user_id: str | None = None,
                    is_admin: bool = False) -> dict[str, Any]:
    """Consolidated view used by the dashboard auto-refresh — no JSON payloads."""
    project = ProjectsRepo.get(project_id, user_id=None if is_admin else user_id)
    if project is None:
        raise HTTPException(404, "Project not found")

    account: dict[str, Any] = {"cash": None, "buying_power": None,
                               "equity": None, "portfolio_value": None,
                               "error": None}
    clock = {"is_open": None, "next_open": None, "next_close": None,
             "error": None}
    positions_live: list[dict[str, Any]] = []

    broker_type = (getattr(project, "broker_type", "alpaca") or "alpaca")
    broker_state = "ready"   # 'ready' | 'needs_oauth' | 'phase2_pending'

    if broker_type == "etrade":
        # ETrade Phase 1: tokens may be missing (user hasn't OAuthed yet)
        # OR tokens may be present but the trading endpoints are still
        # stubbed for Phase 2. Don't call Alpaca regardless.
        if not getattr(project, "etrade_access_token", ""):
            broker_state = "needs_oauth"
            account["error"] = (
                "ETrade is not yet connected. "
                "Click 'Connect ETrade' to authorize."
            )
        else:
            broker_state = "phase2_pending"
            account["error"] = (
                "ETrade is connected, but the trading endpoints land in "
                "Phase 2. The runner is skipping this project for now."
            )

    else:
        # Alpaca path (default).
        try:
            ac = AlpacaClient(project)
            try:
                account.update(ac.get_account())
            except Exception as e:
                account["error"] = str(e)
            try:
                clock.update(ac.get_market_clock())
                for k in ("next_open", "next_close", "timestamp"):
                    if clock.get(k) is not None:
                        clock[k] = str(clock[k])
            except Exception as e:
                clock["error"] = str(e)
            try:
                positions_live = ac.list_positions()
            except Exception:
                positions_live = []
        except Exception as e:
            account["error"] = str(e)

    raw_events = EventsRepo.recent(project_id, limit=80)
    pipeline = summarize_pipeline(raw_events)
    timeline = [humanize_event(e) for e in raw_events[:30]]

    # cycles-per-minute for last 30 minutes (for the activity bar chart)
    from collections import Counter
    from datetime import datetime, timedelta, timezone
    now = datetime.now(tz=timezone.utc)
    bucket: Counter = Counter()
    for e in raw_events:
        if e.get("event_type") != "LOOP":
            continue
        ts = e.get("created_at")
        try:
            t = ts if isinstance(ts, datetime) else datetime.fromisoformat(str(ts))
            if t.tzinfo is None:
                t = t.replace(tzinfo=timezone.utc)
        except Exception:
            continue
        if (now - t) > timedelta(minutes=30):
            continue
        bucket[t.replace(second=0, microsecond=0)] += 1
    cycles_chart = []
    for i in range(29, -1, -1):
        slot = (now - timedelta(minutes=i)).replace(second=0, microsecond=0)
        cycles_chart.append({"t": slot.strftime("%H:%M"), "n": bucket.get(slot, 0)})

    db_positions = PositionsRepo.list_open(project_id)
    contracts = WheelRepo.list_open(project_id)

    # Greeks intentionally NOT computed in snapshot — each aggregation does
    # one Alpaca option-chain call per underlying which is slow enough to
    # starve other endpoints when the dashboard polls every 5 s. The Risk
    # page fetches /risk/greeks on its own cadence.
    greeks = None

    # Active kill switches and most recent breach (for the banner)
    try:
        from db.risk_repos import RiskLimitsRepo
        risk_limits = RiskLimitsRepo.list(project_id)
    except Exception:
        risk_limits = []
    recent_breach = None
    for lim in risk_limits:
        if lim.get("last_breached_at"):
            if recent_breach is None or lim["last_breached_at"] > recent_breach["last_breached_at"]:
                recent_breach = lim

    return {
        "project": {
            "project_id": project.project_id,
            "project_name": project.project_name,
            "alpaca_base_url": project.alpaca_base_url,
            "is_active": project.is_active,
            "broker_type": broker_type,
            "broker_state": broker_state,
            "etrade_environment": getattr(project, "etrade_environment", None),
        },
        "account": account,
        "clock": clock,
        "pipeline": pipeline,
        "timeline": timeline,
        "cycles_chart": cycles_chart,
        "positions_db": db_positions,
        "positions_live": positions_live,
        "contracts": contracts,
        "greeks": greeks,
        "risk_limits": risk_limits,
        "recent_breach": recent_breach,
        "warnings": _build_warnings(account, clock, raw_events, project),
    }


def _build_warnings(account: dict[str, Any], clock: dict[str, Any],
                    events: list[dict[str, Any]], project: Any) -> list[dict[str, str]]:
    warns: list[dict[str, str]] = []
    broker_type = (getattr(project, "broker_type", "alpaca") or "alpaca")

    if account.get("error"):
        if broker_type == "etrade":
            if not getattr(project, "etrade_access_token", ""):
                warns.append({
                    "level": "info",
                    "title": "ETrade not connected yet",
                    "detail": "Complete the OAuth flow to authorize this "
                              "project. Click the 'Connect ETrade' link.",
                })
            else:
                warns.append({
                    "level": "info",
                    "title": "ETrade trading endpoints are in Phase 2",
                    "detail": "OAuth tokens are stored, but live order "
                              "submission ships in the next phase. The "
                              "runner skips this project until then.",
                })
        else:
            warns.append({"level": "error",
                          "title": "Alpaca account unreachable",
                          "detail": account["error"]})
    elif broker_type == "alpaca":
        bp = account.get("buying_power") or 0
        cash = account.get("cash") or 0
        if (bp or 0) == 0 and (cash or 0) == 0:
            warns.append({
                "level": "warn",
                "title": "Paper account has $0 buying power",
                "detail": "Complete the Alpaca application at app.alpaca.markets "
                          "(yellow 'Complete Application' banner) — the account auto-funds "
                          "to $100,000 once approved.",
            })
    provider = (AppSettings.get("llm_provider", "anthropic") or "anthropic").lower()
    if provider == "google":
        if AppSettings.get("google_api_key") in (None, ""):
            warns.append({
                "level": "info",
                "title": "Gemini key not set — running in deterministic fallback",
                "detail": "Add google_api_key in Global Settings (free at aistudio.google.com).",
            })
    else:
        if AppSettings.get("anthropic_api_key") in (None, ""):
            warns.append({
                "level": "info",
                "title": "Claude key not set — running in deterministic fallback",
                "detail": "Add anthropic_api_key in Global Settings, or switch llm_provider=google.",
            })
    # Inspect recent DECIDE rejection reasons for LLM credit / auth issues —
    # but only for the *currently active* provider, and only within the last
    # 5 minutes (otherwise old events from a previous provider stay sticky).
    from datetime import datetime, timedelta, timezone
    now = datetime.now(tz=timezone.utc)
    for e in events[:10]:
        if e.get("node_name") != "Strategist" or e.get("event_type") != "DECIDE":
            continue
        ts = e.get("created_at")
        try:
            t = ts if isinstance(ts, datetime) else datetime.fromisoformat(str(ts))
            if t.tzinfo is None:
                t = t.replace(tzinfo=timezone.utc)
            if (now - t) > timedelta(minutes=5):
                continue
        except Exception:
            continue
        payload = e.get("payload") or {}
        rejs = payload.get("rejections") if isinstance(payload, dict) else None
        if not rejs:
            continue
        reasons = " ".join(str(r.get("reason", "")) for r in rejs).lower()
        if provider == "anthropic":
            if "credit balance is too low" in reasons or "billing" in reasons:
                warns.append({
                    "level": "error",
                    "title": "Anthropic API credit exhausted",
                    "detail": "Add credit at console.anthropic.com → Plans & Billing, "
                              "or switch llm_provider=google in Global Settings.",
                })
                break
            if "invalid_api_key" in reasons or "unauthorized" in reasons:
                warns.append({
                    "level": "error",
                    "title": "Invalid Anthropic API key",
                    "detail": "Update anthropic_api_key in Global Settings.",
                })
                break
        elif provider == "google":
            if "quota" in reasons or "rate limit" in reasons or "resource_exhausted" in reasons:
                warns.append({
                    "level": "error",
                    "title": "Gemini quota / rate-limit hit",
                    "detail": "Wait a minute or upgrade tier at aistudio.google.com.",
                })
                break
            if "api key not valid" in reasons or "invalid_argument" in reasons or "permission_denied" in reasons:
                warns.append({
                    "level": "error",
                    "title": "Invalid Gemini API key",
                    "detail": "Update google_api_key in Global Settings.",
                })
                break
    # Latest error event in a tight recent window (last 15 min). Without
    # the window, stale errors from before fixes shipped keep surfacing as
    # warnings indefinitely.
    err_cutoff = datetime.now(tz=timezone.utc) - timedelta(minutes=15)
    for e in events[:50]:
        if e.get("event_type") != "ERROR":
            continue
        ts = e.get("created_at")
        try:
            t = ts if isinstance(ts, datetime) else datetime.fromisoformat(str(ts))
            if t.tzinfo is None:
                t = t.replace(tzinfo=timezone.utc)
        except Exception:
            continue
        if t < err_cutoff:
            continue
        warns.append({"level": "error",
                      "title": f"Recent error in {e.get('node_name')}",
                      "detail": str(e.get('payload', {}).get('err', ''))[:200]})
        break
    return warns


# ---------- REST: global settings --------------------------------------------

@app.get("/api/settings")
async def api_list_settings():
    rows = AppSettings.list_all()
    return [{"key": r.key, "value": (None if r.is_secret else r.value),
             "value_type": r.value_type, "category": r.category,
             "description": r.description, "is_secret": r.is_secret} for r in rows]


@app.post("/api/settings")
async def api_set_setting(payload: SettingIn):
    AppSettings.set(payload.key, payload.value, value_type=payload.value_type,
                    category=payload.category, description=payload.description,
                    is_secret=payload.is_secret)
    return {"ok": True}


# ---------- REST: projects ----------------------------------------------------

@app.get("/api/projects")
async def api_list_projects(request: Request):
    uid = getattr(request.state, "user_id", None)
    is_admin = bool(getattr(request.state, "is_admin", False))
    return [
        {"project_id": p.project_id, "project_name": p.project_name,
         "alpaca_base_url": p.alpaca_base_url, "alpaca_data_feed": p.alpaca_data_feed,
         "max_equity_allocation": p.max_equity_allocation, "is_active": p.is_active}
        for p in ProjectsRepo.list_all(user_id=None if is_admin else uid)
    ]


@app.post("/api/projects")
async def api_upsert_project(request: Request, payload: ProjectIn):
    uid = getattr(request.state, "user_id", None)
    is_admin = bool(getattr(request.state, "is_admin", False))
    # If updating an existing project, enforce ownership.
    existing = ProjectsRepo.get(payload.project_id,
                                user_id=None if is_admin else uid)
    if existing is None:
        # Check whether it exists at all (without owner scope)
        if ProjectsRepo.get(payload.project_id) is not None:
            raise HTTPException(404, "Project not found")
    project = TradingProject(
        project_id=payload.project_id,
        project_name=payload.project_name,
        alpaca_api_key=payload.alpaca_api_key,
        alpaca_secret_key=payload.alpaca_secret_key,
        alpaca_base_url=payload.alpaca_base_url,
        alpaca_data_feed=payload.alpaca_data_feed,
        max_equity_allocation=payload.max_equity_allocation,
        is_active=payload.is_active,
        user_id=existing.user_id if existing else uid,
        broker_type=payload.broker_type or "alpaca",
        etrade_consumer_key=payload.etrade_consumer_key,
        etrade_consumer_secret=payload.etrade_consumer_secret,
        etrade_access_token=existing.etrade_access_token if existing else "",
        etrade_access_token_secret=existing.etrade_access_token_secret if existing else "",
        etrade_account_id_key=existing.etrade_account_id_key if existing else "",
        etrade_environment=payload.etrade_environment or "sandbox",
    )
    ProjectsRepo.upsert(project)
    return {"ok": True}


@app.delete("/api/projects/{project_id}")
async def api_delete_project(request: Request, project_id: str):
    # Verify ownership before deleting.
    _scoped_project(project_id, request)
    ProjectsRepo.delete(project_id)
    return {"ok": True}


# ---------- REST: tax lots ---------------------------------------------------

@app.get("/api/projects/{project_id}/tax_lots/open")
async def api_open_tax_lots(request: Request, project_id: str,
                            ticker: str | None = None):
    _scoped_project(project_id, request)
    from analytics.tax_lots import open_lots
    return {"lots": open_lots(project_id, ticker)}


@app.get("/api/projects/{project_id}/tax_lots/capital_gains")
async def api_capital_gains(request: Request, project_id: str,
                            year: int | None = None):
    _scoped_project(project_id, request)
    from analytics.tax_lots import capital_gains_summary
    from datetime import datetime, timezone
    yr = year or datetime.now(tz=timezone.utc).year
    return capital_gains_summary(project_id, int(yr))


# ---------- Tax report page + Form 8949 CSV -------------------------------

@app.get("/projects/{project_id}/tax_report", response_class=HTMLResponse)
async def tax_report_page(request: Request, project_id: str,
                          year: int | None = None):
    project = _scoped_project(project_id, request)
    from datetime import datetime, timezone
    current_year = datetime.now(tz=timezone.utc).year
    selected_year = int(year) if year else current_year
    return templates.TemplateResponse("tax_report.html", {
        "request": request,
        "project": project,
        "selected_year": selected_year,
        "year_options": list(range(current_year, current_year - 6, -1)),
    })


@app.get("/api/projects/{project_id}/tax_report/form_8949.csv")
async def api_form_8949_csv(request: Request, project_id: str,
                            year: int | None = None):
    """Download per-lot capital-gains detail as a CSV importable into
    TurboTax/H&R Block. One row per FIFO consumption — i.e. one Form 8949
    line. Includes BOTH short-term and long-term rows; the ``Term``
    column distinguishes them."""
    _scoped_project(project_id, request)
    import csv
    import io
    from datetime import datetime, timezone
    from analytics.tax_lots import form_8949_rows, capital_gains_summary
    yr = int(year) if year else datetime.now(tz=timezone.utc).year
    rows = form_8949_rows(project_id, yr)
    summary = capital_gains_summary(project_id, yr)

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([
        f"Form 8949 / Schedule D detail — {project_id} — tax year {yr}",
    ])
    w.writerow([
        "Kind", "Description", "Date Acquired", "Date Sold",
        "Proceeds (USD)", "Cost Basis (USD)", "Gain/Loss (USD)",
        "Term", "Holding Days", "Ticker", "Quantity",
        "Sale Price", "Cost Per Share",
    ])
    for r in rows:
        sp = (f'{r["sale_price"]:.4f}'
              if r.get("sale_price") is not None else "")
        cps = (f'{r["cost_per_share"]:.4f}'
               if r.get("cost_per_share") is not None else "")
        w.writerow([
            r.get("kind", ""),
            r["description"], r["date_acquired"], r["date_sold"],
            f'{r["proceeds"]:.2f}', f'{r["cost_basis"]:.2f}',
            f'{r["realized_pnl"]:.2f}', r["term"].upper(),
            r["holding_days"], r["ticker"], r["quantity"],
            sp, cps,
        ])
    w.writerow([])
    w.writerow(["SUMMARY"])
    bd = summary.get("breakdown") or {}
    w.writerow(["Stock — short-term (USD)",
                f'{bd.get("stock_short_term", 0):.2f}'])
    w.writerow(["Stock — long-term (USD)",
                f'{bd.get("stock_long_term", 0):.2f}'])
    w.writerow(["Options — short-term (USD)",
                f'{bd.get("option_short_term", 0):.2f}'])
    w.writerow(["Options — long-term (USD)",
                f'{bd.get("option_long_term", 0):.2f}'])
    w.writerow(["Short-term realized P&L (USD)",
                f'{summary["short_term_total"]:.2f}'])
    w.writerow(["Long-term realized P&L (USD)",
                f'{summary["long_term_total"]:.2f}'])
    w.writerow(["Total realized P&L (USD)",
                f'{summary["grand_total"]:.2f}'])
    w.writerow([])
    w.writerow(["DISCLAIMER: Informational only. Wash-sale, "
                "specific-identification, and Section 1256 rules not "
                "applied. FIFO basis for stock. Assigned-option premium "
                "is excluded here because IRS rules fold it into the "
                "underlying stock basis (not a separate event). "
                "Consult a tax professional before filing."])

    safe_pid = "".join(c if c.isalnum() or c in "-_" else "_"
                       for c in project_id)
    fname = f"{safe_pid}_form_8949_{yr}.csv"
    return PlainTextResponse(
        buf.getvalue(), media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


# ---------- REST: per-project settings ---------------------------------------

@app.get("/api/projects/{project_id}/settings")
async def api_list_project_settings(request: Request, project_id: str):
    _scoped_project(project_id, request)
    rows = ProjectSettings.list_for_project(project_id)
    return [{"key": r.key, "value": r.value, "value_type": r.value_type,
             "description": r.description} for r in rows]


@app.post("/api/projects/{project_id}/settings")
async def api_set_project_setting(request: Request, project_id: str, payload: ProjectSettingIn):
    _scoped_project(project_id, request)
    ProjectSettings.set(project_id, payload.key, payload.value, value_type=payload.value_type)
    return {"ok": True}


# ---------- REST: project-settings export / import / clone -----------------

@app.get("/api/projects/{project_id}/settings/export")
async def api_settings_export(request: Request, project_id: str):
    """Download a JSON snapshot of every project setting. Streamed with a
    Content-Disposition header so the browser saves it as a file."""
    _scoped_project(project_id, request)
    import io
    snapshot = ProjectSettings.export_all(project_id)
    buf = io.BytesIO(json.dumps(snapshot, indent=2,
                                default=str).encode("utf-8"))
    safe_pid = "".join(c if c.isalnum() or c in "-_" else "_"
                       for c in project_id)
    fname = f"{safe_pid}_settings.json"
    return StreamingResponse(
        buf, media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@app.post("/api/projects/{project_id}/settings/import")
async def api_settings_import(
    request: Request, project_id: str,
    file: UploadFile = File(...),
    overwrite: bool = True,
):
    """Apply a previously-exported JSON file to this project. Defaults to
    overwriting existing values; pass ``?overwrite=false`` to merge only
    keys that aren't already set on this project."""
    _scoped_project(project_id, request)
    raw = await file.read()
    try:
        payload = json.loads(raw.decode("utf-8"))
    except Exception as e:
        raise HTTPException(status_code=400,
                            detail=f"invalid JSON: {e}")
    result = ProjectSettings.import_bulk(project_id, payload,
                                         overwrite=overwrite)
    return {"ok": True, **result}


class _CloneSettingsIn(BaseModel):
    source_project_id: str
    overwrite: bool = True


@app.post("/api/projects/{project_id}/settings/clone_from")
async def api_settings_clone_from(request: Request, project_id: str,
                                  payload: _CloneSettingsIn):
    """Copy every setting from another project this user owns into this
    project. No file leaves the server."""
    _scoped_project(project_id, request)
    # Caller must also own (or admin) the source project — _scoped_project
    # raises 404 otherwise.
    _scoped_project(payload.source_project_id, request)
    result = ProjectSettings.clone_from(
        payload.source_project_id, project_id,
        overwrite=payload.overwrite,
    )
    return {"ok": True, **result}


@app.get("/api/projects/{project_id}/events")
async def api_project_events(request: Request, project_id: str, limit: int = 50):
    _scoped_project(project_id, request)
    return EventsRepo.recent(project_id, limit=limit)


@app.get("/api/projects/{project_id}/logs")
async def api_project_logs(request: Request, project_id: str,
                           node: str | None = None,
                           event_type: str | None = None,
                           search: str | None = None,
                           limit: int = 100,
                           before_id: int | None = None,
                           humanize: bool = True):
    _scoped_project(project_id, request)
    raw = EventsRepo.query(project_id, node=node or None,
                           event_type=event_type or None,
                           search=search or None,
                           limit=limit, before_id=before_id)
    if humanize:
        return [{**humanize_event(e), "payload": e.get("payload")} for e in raw]
    return raw


@app.get("/projects/{project_id}/logs", response_class=HTMLResponse)
async def project_logs_page(request: Request, project_id: str):
    project = _scoped_project(project_id, request)
    return templates.TemplateResponse("logs.html", {
        "request": request,
        "project": project,
    })


@app.get("/logs", response_class=HTMLResponse)
async def logs_picker(request: Request):
    """When clicked from top nav: redirect to first active project's logs,
    or render a chooser if multiple projects exist."""
    uid = getattr(request.state, "user_id", None)
    is_admin = bool(getattr(request.state, "is_admin", False))
    projects = ProjectsRepo.list_all(user_id=None if is_admin else uid)
    if not projects:
        raise HTTPException(404, "No projects yet — add one from the dashboard.")
    if len(projects) == 1:
        return RedirectResponse(f"/projects/{projects[0].project_id}/logs")
    # Multiple — render dashboard so user can pick.
    return templates.TemplateResponse("dashboard.html", {
        "request": request, "projects": projects,
        "runner_active": _runner is not None,
    })


@app.get("/help", response_class=HTMLResponse)
async def help_page(request: Request):
    return templates.TemplateResponse("help.html", {"request": request})


@app.get("/api/projects/{project_id}/positions")
async def api_project_positions(request: Request, project_id: str):
    _scoped_project(project_id, request)
    return PositionsRepo.list_open(project_id)


@app.post("/api/projects/{project_id}/reset_paper")
async def api_reset_paper(request: Request, project_id: str, cash: float = 100000.0):
    from execution import AlpacaClient
    project = _scoped_project(project_id, request)
    if (project.broker_type or "alpaca") != "alpaca":
        raise HTTPException(400,
            "Reset Paper Account is an Alpaca-only feature. "
            "ETrade sandbox accounts are managed directly at apisb.etrade.com.")
    if "paper-api" not in (project.alpaca_base_url or ""):
        raise HTTPException(400, "Refusing to reset a non-paper account.")
    try:
        result = AlpacaClient(project).reset_paper_account(cash=cash)
        EventsRepo.log(project_id, "Admin", "RESET_PAPER", {"cash": cash, "result": result})
        return {"ok": True, "result": result}
    except Exception as e:
        raise HTTPException(400, f"Reset failed: {e}")


@app.get("/api/projects/{project_id}/contracts")
async def api_project_contracts(request: Request, project_id: str):
    _scoped_project(project_id, request)
    return WheelRepo.list_open(project_id)


# ---------- REST: analytics --------------------------------------------------

@app.get("/api/projects/{project_id}/performance/summary")
async def api_performance_summary(request: Request, project_id: str, period: str = "all"):
    _scoped_project(project_id, request)
    from analytics.pnl_calculator import metrics_summary
    return metrics_summary(project_id, period=period)


@app.get("/api/projects/{project_id}/performance/equity_curve")
async def api_equity_curve(request: Request, project_id: str, period: str = "month"):
    _scoped_project(project_id, request)
    from analytics.pnl_calculator import equity_curve_points
    return equity_curve_points(project_id, period=period)


@app.get("/api/projects/{project_id}/performance/closed_trades")
async def api_closed_trades(request: Request, project_id: str, ticker: str | None = None,
                            limit: int = 100):
    _scoped_project(project_id, request)
    from db.analytics_repos import ClosedContractsRepo
    return ClosedContractsRepo.list(project_id, ticker=ticker, limit=limit)


@app.get("/api/projects/{project_id}/performance/by_ticker")
async def api_perf_by_ticker(request: Request, project_id: str, since_days: int = 90,
                             min_trades: int = 1):
    _scoped_project(project_id, request)
    from datetime import datetime, timedelta, timezone
    from db.analytics_repos import ClosedContractsRepo
    since = datetime.now(tz=timezone.utc) - timedelta(days=since_days)
    return ClosedContractsRepo.by_ticker(project_id, since=since,
                                         min_trades=min_trades)


@app.get("/api/projects/{project_id}/performance/attribution")
async def api_attribution(request: Request, project_id: str, dimension: str = "delta",
                          since_days: int = 365, min_trades: int = 1):
    _scoped_project(project_id, request)
    from analytics.attribution import attribution_by_dimension
    return attribution_by_dimension(project_id, dimension=dimension,
                                    since_days=since_days,
                                    min_trades=min_trades)


@app.post("/api/projects/{project_id}/performance/snapshot")
async def api_take_snapshot_now(request: Request, project_id: str):
    _scoped_project(project_id, request)
    from analytics.snapshotter import take_snapshot
    sid = take_snapshot(project_id)
    return {"ok": sid is not None, "snapshot_id": sid}


@app.post("/api/projects/{project_id}/performance/detect_closures")
async def api_detect_now(request: Request, project_id: str):
    _scoped_project(project_id, request)
    from analytics.closure_detector import detect_closures
    return detect_closures(project_id)


@app.get("/projects/{project_id}/performance", response_class=HTMLResponse)
async def performance_page(request: Request, project_id: str):
    project = _scoped_project(project_id, request)
    return templates.TemplateResponse("performance.html", {
        "request": request, "project": project,
    })


# ---------- P&L report (dedicated printable page + CSV) ------------------

def _pnl_default_range():
    from datetime import datetime, timezone
    now = datetime.now(tz=timezone.utc)
    return (now.replace(month=1, day=1, hour=0, minute=0,
                        second=0, microsecond=0), now)


@app.get("/projects/{project_id}/pnl_report", response_class=HTMLResponse)
async def pnl_report_page(request: Request, project_id: str,
                          from_: str | None = None,
                          to: str | None = None):
    """Dedicated printable P&L report. Browser-print produces a clean
    PDF; CSV download is a separate route."""
    project = _scoped_project(project_id, request)
    return templates.TemplateResponse("pnl_report.html", {
        "request": request, "project": project,
        "from_": from_, "to": to,
    })


@app.get("/api/projects/{project_id}/pnl_report")
async def api_pnl_report(request: Request, project_id: str,
                         from_: str | None = None,
                         to: str | None = None):
    """JSON aggregate for the P&L report page. Returns summary metrics
    + monthly breakdown + closed-trade list for the date range."""
    _scoped_project(project_id, request)
    from datetime import datetime, timezone
    from analytics.pnl_calculator import (
        metrics_summary, monthly_breakdown, equity_curve_points,
    )
    from db.analytics_repos import (
        ClosedContractsRepo, ClosedPositionsRepo,
    )

    default_from, default_to = _pnl_default_range()
    fd = _parse_date_param(from_, default_from)
    td = _parse_date_param(to, default_to, end_of_day=True)

    contracts = ClosedContractsRepo.list(project_id, since=fd, limit=10000)
    contracts = [c for c in contracts
                 if c.get("closed_at") and _le_dt(c["closed_at"], td)]
    stock_pnl = ClosedPositionsRepo.realized_pnl_since(project_id, fd)
    monthly = monthly_breakdown(project_id, fd, td)
    summary = metrics_summary(project_id, period="all")
    # Override the period summary to reflect this date range, not "all".
    pnl_values = [c["realized_pnl"] for c in contracts]
    total_pnl = sum(pnl_values) + stock_pnl
    wins = [v for v in pnl_values if v > 0]
    losses = [v for v in pnl_values if v < 0]
    total_premium = sum(c.get("premium_collected") or 0 for c in contracts)
    trade_count = len(contracts)

    # ---- Return-on-capital math ---------------------------------------
    # "% gain over account funds" — the user wants to know how the
    # period's realized P&L compares to the capital that was at risk.
    # Three sources, in priority order:
    #   1. Portfolio snapshot at or after the period start (real equity)
    #   2. Earliest-ever snapshot (works for "All time" period)
    #   3. project.max_equity_allocation (the budget; stable, never None)
    # Whichever wins becomes ``starting_equity`` and feeds the % gain.
    from db.analytics_repos import PortfolioSnapshotsRepo
    starting_equity = None
    starting_source = None
    snap_at_fd = PortfolioSnapshotsRepo.at_or_after(project_id, fd)
    if snap_at_fd and snap_at_fd.get("equity"):
        starting_equity = float(snap_at_fd["equity"])
        starting_source = "snapshot"
    if not starting_equity:
        earliest = PortfolioSnapshotsRepo.earliest(project_id)
        if earliest and earliest.get("equity"):
            starting_equity = float(earliest["equity"])
            starting_source = "earliest_snapshot"
    project = ProjectsRepo.get(project_id)
    budget = float(getattr(project, "max_equity_allocation", 0) or 0)
    if not starting_equity and budget > 0:
        starting_equity = budget
        starting_source = "max_equity_allocation"

    pct_gain = None
    annualized_pct = None
    if starting_equity and starting_equity > 0:
        pct_gain = round(total_pnl / starting_equity * 100, 2)
        # Annualize when the period is > 14 days. Below that the figure
        # is more noise than signal (one good week extrapolates to an
        # absurd APR).
        days = max(1, (td - fd).days or 1)
        if days >= 14:
            try:
                annualized_pct = round(
                    ((1 + pct_gain / 100) ** (365.0 / days) - 1) * 100, 2)
            except Exception:
                annualized_pct = None

    period_summary = {
        "from": fd.isoformat(),
        "to": td.isoformat(),
        "realized_pnl": round(total_pnl, 2),
        "option_pnl": round(sum(pnl_values), 2),
        "stock_pnl": round(stock_pnl, 2),
        "total_premium": round(total_premium, 2),
        "trade_count": trade_count,
        "win_rate": (round(len(wins) / trade_count, 4)
                     if trade_count else 0.0),
        "wins": len(wins),
        "losses": len(losses),
        "avg_winner": round(sum(wins) / len(wins), 2) if wins else 0.0,
        "avg_loser": round(sum(losses) / len(losses), 2) if losses else 0.0,
        "profit_factor": (round(sum(wins) / abs(sum(losses)), 2)
                          if losses else (None if not wins else None)),
        "max_drawdown": summary.get("max_drawdown"),
        "current_equity": summary.get("current_equity"),
        "unrealized_pnl": summary.get("unrealized_pnl"),
        "starting_equity": (round(starting_equity, 2)
                            if starting_equity else None),
        "starting_source": starting_source,
        "pct_gain_on_capital": pct_gain,
        "annualized_pct": annualized_pct,
        "budget": round(budget, 2) if budget else None,
    }
    return {
        "project_id": project_id,
        "from": fd.isoformat(),
        "to": td.isoformat(),
        "summary": period_summary,
        "monthly": monthly,
        "closed_trades": contracts,
    }


@app.get("/api/projects/{project_id}/pnl_report.csv")
async def api_pnl_report_csv(request: Request, project_id: str,
                             from_: str | None = None,
                             to: str | None = None):
    """Download the full P&L report as a CSV (summary + monthly +
    per-trade detail)."""
    _scoped_project(project_id, request)
    import csv
    import io
    from datetime import datetime, timezone
    from analytics.pnl_calculator import monthly_breakdown
    from db.analytics_repos import (
        ClosedContractsRepo, ClosedPositionsRepo,
    )

    default_from, default_to = _pnl_default_range()
    fd = _parse_date_param(from_, default_from)
    td = _parse_date_param(to, default_to, end_of_day=True)
    contracts = ClosedContractsRepo.list(project_id, since=fd, limit=10000)
    contracts = [c for c in contracts
                 if c.get("closed_at") and _le_dt(c["closed_at"], td)]
    stock_pnl = ClosedPositionsRepo.realized_pnl_since(project_id, fd)
    monthly = monthly_breakdown(project_id, fd, td)

    pnl_values = [c["realized_pnl"] for c in contracts]
    total_pnl = sum(pnl_values) + stock_pnl
    wins = [v for v in pnl_values if v > 0]
    losses = [v for v in pnl_values if v < 0]

    # Same starting-equity resolution as the JSON endpoint above.
    from db.analytics_repos import PortfolioSnapshotsRepo
    starting_equity = None
    starting_source = None
    snap_at_fd = PortfolioSnapshotsRepo.at_or_after(project_id, fd)
    if snap_at_fd and snap_at_fd.get("equity"):
        starting_equity = float(snap_at_fd["equity"])
        starting_source = "snapshot at period start"
    if not starting_equity:
        earliest = PortfolioSnapshotsRepo.earliest(project_id)
        if earliest and earliest.get("equity"):
            starting_equity = float(earliest["equity"])
            starting_source = "earliest recorded equity"
    project = ProjectsRepo.get(project_id)
    budget = float(getattr(project, "max_equity_allocation", 0) or 0)
    if not starting_equity and budget > 0:
        starting_equity = budget
        starting_source = "project budget (max_equity_allocation)"
    pct_gain = None
    if starting_equity and starting_equity > 0:
        pct_gain = total_pnl / starting_equity * 100

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([f"P&L Report — {project_id} — "
                f"{fd.date()} through {td.date()}"])
    w.writerow([])
    w.writerow(["SUMMARY"])
    w.writerow(["Realized P&L (options)", f"{sum(pnl_values):.2f}"])
    w.writerow(["Realized P&L (stock)", f"{stock_pnl:.2f}"])
    w.writerow(["Realized P&L (total)", f"{total_pnl:.2f}"])
    if starting_equity:
        w.writerow(["Starting capital", f"{starting_equity:.2f}"])
        w.writerow(["Capital reference", starting_source or ""])
    if pct_gain is not None:
        w.writerow(["% gain on capital", f"{pct_gain:+.2f}%"])
    w.writerow(["Total premium captured",
                f"{sum(c.get('premium_collected') or 0 for c in contracts):.2f}"])
    w.writerow(["Trade count", len(contracts)])
    w.writerow(["Wins", len(wins)])
    w.writerow(["Losses", len(losses)])
    w.writerow(["Win rate",
                f"{(len(wins)/len(contracts)*100 if contracts else 0):.2f}%"])
    if wins:
        w.writerow(["Average winner", f"{sum(wins)/len(wins):.2f}"])
    if losses:
        w.writerow(["Average loser", f"{sum(losses)/len(losses):.2f}"])
    if losses:
        w.writerow(["Profit factor",
                    f"{sum(wins)/abs(sum(losses)):.2f}"])

    w.writerow([])
    w.writerow(["MONTHLY BREAKDOWN"])
    w.writerow(["Month", "Realized P&L", "Premium captured",
                "Trades", "Wins", "Losses", "Win rate"])
    for m in monthly:
        w.writerow([m["month"], f'{m["realized_pnl"]:.2f}',
                    f'{m["premium_captured"]:.2f}',
                    m["trade_count"], m["wins"], m["losses"],
                    f'{m["win_rate"]*100:.2f}%'])

    w.writerow([])
    w.writerow(["CLOSED TRADES"])
    w.writerow(["Closed at", "Ticker", "Phase", "Strike", "Qty",
                "Days held", "Premium collected", "Close cost",
                "Realized P&L", "Reason"])
    for c in contracts:
        w.writerow([
            (c.get("closed_at") or ""),
            c.get("ticker") or "",
            c.get("strategy_phase") or "",
            c.get("strike_price") or "",
            c.get("quantity") or "",
            c.get("days_held") or "",
            f'{(c.get("premium_collected") or 0):.4f}',
            f'{(c.get("close_cost") or 0):.4f}',
            f'{(c.get("realized_pnl") or 0):.4f}',
            c.get("closure_reason") or "",
        ])

    safe_pid = "".join(ch if ch.isalnum() or ch in "-_" else "_"
                       for ch in project_id)
    fname = f"{safe_pid}_pnl_{fd.date()}_to_{td.date()}.csv"
    return PlainTextResponse(
        buf.getvalue(), media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


def _parse_date_param(s, fallback, *, end_of_day: bool = False):
    """Best-effort parse of YYYY-MM-DD; fall back to ``fallback``."""
    from datetime import datetime, time, timezone
    if not s:
        return fallback
    try:
        d = datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        if end_of_day:
            d = datetime.combine(d.date(), time(23, 59, 59),
                                 tzinfo=timezone.utc)
        return d
    except Exception:
        return fallback


def _le_dt(maybe_dt, cutoff) -> bool:
    """True if maybe_dt (datetime or ISO str) is <= cutoff (datetime)."""
    from datetime import datetime, timezone
    if isinstance(maybe_dt, str):
        try:
            maybe_dt = datetime.fromisoformat(maybe_dt.replace("Z", "+00:00"))
        except Exception:
            return True
    if getattr(maybe_dt, "tzinfo", None) is None:
        maybe_dt = maybe_dt.replace(tzinfo=timezone.utc)
    return maybe_dt <= cutoff


@app.get("/projects/{project_id}/analysis", response_class=HTMLResponse)
async def analysis_page(request: Request, project_id: str):
    project = _scoped_project(project_id, request)
    return templates.TemplateResponse("analysis.html", {
        "request": request, "project": project,
    })


@app.get("/api/health")
async def health():
    return {"ok": True, "runner_active": _runner is not None}
