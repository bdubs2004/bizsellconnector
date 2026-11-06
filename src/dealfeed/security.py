"""Security controls shared by the web app, REST API and MCP server.

  * rate limiting (in-process sliding window; swap for Redis when running more than one instance)
  * security headers + Content-Security-Policy on every response
  * CSRF tokens for cookie-authenticated web forms (the API and MCP use bearer keys, not cookies)
  * generic errors with a reference id, so nothing internal (stack traces, SQL, connection strings,
    file paths, hostnames) ever reaches a client or an AI; the detail goes to the server log only
  * text sanitizing for anything that flows into an AI's context (hidden-character prompt-injection tricks)
  * production startup checks that refuse to boot with insecure settings
"""
from __future__ import annotations

import hmac
import logging
import re
import secrets
import threading
import time
from collections import defaultdict, deque

from .config import get_settings

log = logging.getLogger("dealfeed.security")


# --- rate limiting -----------------------------------------------------------------------------

# name: (max requests, window seconds)
LIMITS = {
    "api": (60, 60),            # per API key's user, REST
    "mcp": (60, 60),            # per user, AI tool calls
    "web": (180, 60),           # per signed-in user, page/HTMX requests
    "login": (10, 60),          # per client IP
    "auth_fail": (20, 300),     # bad API keys per client IP
    "live_user": (20, 60),      # live state-registry checks per user
    "live_global": (300, 60),   # all users together: keeps us polite to state APIs
}


class RateLimited(Exception):
    def __init__(self, retry_after: int, what: str):
        super().__init__(f"Too many requests ({what}). Try again in {retry_after}s.")
        self.retry_after = retry_after


class RateLimiter:
    def __init__(self):
        self._hits: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def hit(self, bucket: str, key: str, limits: dict | None = None) -> None:
        limit, window = (limits or LIMITS)[bucket]
        now = time.monotonic()
        k = f"{bucket}:{key}"
        with self._lock:
            q = self._hits[k]
            while q and q[0] <= now - window:
                q.popleft()
            if len(q) >= limit:
                raise RateLimited(max(1, int(q[0] + window - now) + 1), bucket)
            q.append(now)
            if len(self._hits) > 50_000:          # bound memory: drop idle buckets
                for kk in [kk for kk, qq in self._hits.items() if not qq][:10_000]:
                    self._hits.pop(kk, None)

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


limiter = RateLimiter()


def client_ip(request) -> str:
    # Behind a proxy, run uvicorn with --proxy-headers (see `dealfeed serve`) so this is the real client.
    return request.client.host if request.client else "unknown"


# --- errors --------------------------------------------------------------------------------------

def error_reference() -> str:
    return secrets.token_hex(4)


def log_internal_error(where: str, exc: BaseException) -> str:
    """Log the full exception server-side and return a short reference id for the client."""
    ref = error_reference()
    log.error("internal error ref=%s in %s", ref, where, exc_info=exc)
    return ref


# --- text sanitizing (prompt-injection hygiene) -------------------------------------------------

# Control chars, zero-width chars, bidi overrides/isolates, BOM: invisible text that can hide
# instructions from a human reviewer while an AI still reads them.
_HIDDEN = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f​-‏‪-‮⁠-⁤⁦-⁩﻿]")


def strip_hidden(s: str | None) -> str | None:
    return _HIDDEN.sub("", s) if s else s


def clean_user_text(s: str | None, max_len: int) -> str | None:
    if s is None:
        return None
    s = strip_hidden(s).strip()
    return s[:max_len] or None


def escape_like(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# --- CSRF ----------------------------------------------------------------------------------------

CSRF_KEY = "csrf"


def csrf_token(request) -> str:
    tok = request.session.get(CSRF_KEY)
    if not tok:
        tok = secrets.token_urlsafe(32)
        request.session[CSRF_KEY] = tok
    return tok


def csrf_ok(request, submitted: str | None) -> bool:
    expected = request.session.get(CSRF_KEY)
    return bool(expected and submitted and hmac.compare_digest(expected, submitted))


# --- headers -------------------------------------------------------------------------------------

def csp() -> str:
    return "; ".join([
        "default-src 'self'",
        "script-src 'self'",                       # no inline scripts anywhere in the app
        "style-src 'self' 'unsafe-inline'",        # inline style attributes (meters, htmx indicators)
        "img-src 'self' data: https://tile.openstreetmap.org",
        "connect-src 'self'",
        "font-src 'self'",
        "object-src 'none'",
        "base-uri 'self'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    ])


_DOCS_PATHS = ("/docs", "/redoc", "/openapi.json")


class SecurityHeadersMiddleware:
    """Pure ASGI middleware (works with streaming responses)."""

    def __init__(self, app, api_docs_csp_exempt: bool = True):
        self.app = app
        self.docs_exempt = api_docs_csp_exempt

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        prod = get_settings().env == "production"

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                headers = [(k, v) for k, v in message.get("headers", []) if k.lower() != b"server"]
                add = {
                    b"x-content-type-options": b"nosniff",
                    b"x-frame-options": b"DENY",
                    b"referrer-policy": b"same-origin",
                    b"permissions-policy": b"geolocation=(), camera=(), microphone=(), payment=(), usb=()",
                    b"cross-origin-opener-policy": b"same-origin",
                }
                if not (self.docs_exempt and path.startswith(_DOCS_PATHS)):
                    add[b"content-security-policy"] = csp().encode()
                if not path.startswith("/static/"):
                    add[b"cache-control"] = b"no-store"
                if prod:
                    add[b"strict-transport-security"] = b"max-age=31536000; includeSubDomains"
                existing = {k.lower() for k, _ in headers}
                headers += [(k, v) for k, v in add.items() if k not in existing]
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_wrapper)


# --- production checks ---------------------------------------------------------------------------

class InsecureConfig(RuntimeError):
    pass


def check_production() -> list[str]:
    """Return problems that make a production deployment unsafe. Callers refuse to start if any."""
    s = get_settings()
    if s.env != "production":
        return []
    problems = []
    if not s.session_secret or len(s.session_secret) < 32:
        problems.append("DEALFEED_SESSION_SECRET must be set to a random value of 32+ characters.")
    if s.web_auth == "dev":
        problems.append("DEALFEED_WEB_AUTH=dev (email-only sign-in) can't run in production; set up Supabase auth.")
    if "postgres:postgres@" in s.database_url or s.database_url.startswith("postgresql://postgres@"):
        problems.append("DATABASE_URL uses the default/blank postgres password.")
    if not s.public_url.startswith("https://"):
        problems.append("DEALFEED_PUBLIC_URL must be https:// in production.")
    return problems


def enforce_production() -> None:
    problems = check_production()
    if problems:
        raise InsecureConfig("Refusing to start in production:\n  - " + "\n  - ".join(problems))
