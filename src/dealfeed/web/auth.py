"""Web sign-in. Everything auth-related for the web app goes through this module,
so swapping in Clerk or Supabase Auth later means replacing these functions
and the /login routes, nothing else.

Current mode, "dev": type an email and you're signed in as that user (created
on first use). There's no password and no email check, so it is ONLY for
local development. `dev_login_allowed()` refuses it when DEALFEED_ENV=production.
"""
from __future__ import annotations

import secrets

import psycopg
from fastapi import Request

from ..config import get_settings
from ..services import accounts
from ..services.accounts import User

SESSION_KEY = "uid"
_EPHEMERAL_SECRET = secrets.token_urlsafe(32)


def session_secret() -> str:
    # Without a configured secret, sessions only last until the process restarts.
    return get_settings().session_secret or _EPHEMERAL_SECRET


def dev_login_allowed() -> bool:
    s = get_settings()
    return s.web_auth == "dev" and s.env != "production"


def current_user(request: Request, conn: psycopg.Connection) -> User | None:
    uid = request.session.get(SESSION_KEY)
    if not uid:
        return None
    user = accounts.get_user(conn, uid)
    if not user:
        request.session.clear()
    return user


def dev_sign_in(request: Request, conn: psycopg.Connection, email: str) -> User:
    if not dev_login_allowed():
        raise PermissionError("Dev sign-in is disabled in production. Configure a real auth provider.")
    email = email.strip().lower()
    if "@" not in email or len(email) > 254:
        raise ValueError("Enter a valid email address.")
    user = accounts.get_user_by_email(conn, email) or accounts.create_user(conn, email, get_settings().dev_signup_plan)
    request.session.clear()
    request.session[SESSION_KEY] = str(user.id)
    return user


def sign_out(request: Request) -> None:
    request.session.clear()
