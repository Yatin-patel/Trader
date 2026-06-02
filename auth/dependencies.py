"""FastAPI dependencies for auth.

current_user_optional — returns AuthUser or None (use for pages that work both
                       logged-in and logged-out, e.g. landing page)
require_user          — raises 401 / redirects to /login if not authenticated
"""
from __future__ import annotations

from fastapi import Cookie, HTTPException, Request
from fastapi.responses import RedirectResponse

from .repositories import AuthUser, UsersRepo
from .sessions import SESSION_COOKIE, get_session


async def current_user_optional(request: Request) -> AuthUser | None:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    sess = get_session(token)
    if not sess:
        return None
    user = UsersRepo.get_by_id(sess["user_id"])
    if user is None or not user.is_active:
        return None
    return user


class RedirectToLogin(HTTPException):
    """Special exception caught by middleware to issue a redirect for HTML
    routes, but a normal 401 JSON for /api/ routes."""
    def __init__(self):
        super().__init__(status_code=401, detail="login required")


async def require_user(request: Request) -> AuthUser:
    user = await current_user_optional(request)
    if user is None:
        raise RedirectToLogin()
    return user
