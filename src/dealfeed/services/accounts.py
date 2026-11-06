"""Users, API keys, plans and usage metering (scope 6.1 guardrails, Section 9 pricing)."""
from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass

import psycopg


@dataclass(frozen=True)
class Plan:
    """Metering follows the industry pattern (ZoomInfo, Apollo): searching and viewing inside Deal Feed (web
    and AI chat) is free under a daily fair-use cap; credits are spent only when data leaves the platform,
    i.e. CSV exports and REST API results. One credit per business per month."""
    name: str
    views_per_day: int        # fair use: new businesses viewed per day (anti-scraping)
    credits_per_month: int    # export / API credits included each month
    max_per_call: int
    mcp: bool
    export: bool
    max_export_rows: int


PLANS = {
    "free": Plan("free", views_per_day=50, credits_per_month=25, max_per_call=10, mcp=False, export=False,
                 max_export_rows=0),
    "searcher": Plan("searcher", views_per_day=1_000, credits_per_month=500, max_per_call=50, mcp=True,
                     export=True, max_export_rows=500),
    "fund": Plan("fund", views_per_day=3_000, credits_per_month=2_500, max_per_call=50, mcp=True, export=True,
                 max_export_rows=2_500),
    # Usage-billed: included credits, then top-ups / metered billing (Stripe, later).
    "api": Plan("api", views_per_day=20_000, credits_per_month=10_000, max_per_call=50, mcp=True, export=True,
                max_export_rows=5_000),
}
MAX_ACTIVE_KEYS = 5


class AuthError(Exception):
    pass


class PlanError(Exception):
    """The caller's plan doesn't allow this, the fair-use limit is reached, or credits ran out."""


class InsufficientCredits(PlanError):
    def __init__(self, needed: int, left: int):
        super().__init__(f"Out of credits: this needs {needed} (one per business not already exported or pulled "
                         f"this month) and you have {left} left. Searching and viewing in Deal Feed stay free; "
                         "add credits or wait for next month's allowance to export.")
        self.needed, self.left = needed, left


@dataclass(frozen=True)
class User:
    id: uuid.UUID
    email: str
    plan: Plan


def _hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def create_user(conn: psycopg.Connection, email: str, plan: str = "free") -> User:
    if plan not in PLANS:
        raise ValueError(f"unknown plan {plan!r}; choose from {list(PLANS)}")
    row = conn.execute(
        "INSERT INTO users (id, email, plan) VALUES (%s, %s, %s) "
        "ON CONFLICT (email) DO UPDATE SET plan = EXCLUDED.plan RETURNING id, email, plan",
        (uuid.uuid4(), email.lower().strip(), plan),
    ).fetchone()
    conn.commit()
    return User(row["id"], row["email"], PLANS[row["plan"]])


def get_user_by_email(conn: psycopg.Connection, email: str) -> User | None:
    row = conn.execute("SELECT id, email, plan FROM users WHERE email = %s", (email.lower().strip(),)).fetchone()
    return User(row["id"], row["email"], PLANS[row["plan"]]) if row else None


def get_user(conn: psycopg.Connection, user_id: uuid.UUID | str) -> User | None:
    row = conn.execute("SELECT id, email, plan FROM users WHERE id = %s", (str(user_id),)).fetchone()
    return User(row["id"], row["email"], PLANS[row["plan"]]) if row else None


def create_api_key(conn: psycopg.Connection, user: User, label: str | None = None) -> str:
    """Returns the plaintext key. It is shown once and only its hash is stored."""
    active = conn.execute("SELECT count(*) AS n FROM api_keys WHERE user_id = %s AND revoked_at IS NULL",
                          (user.id,)).fetchone()["n"]
    if active >= MAX_ACTIVE_KEYS:
        raise PlanError(f"You already have {MAX_ACTIVE_KEYS} active API keys. Revoke one first.")
    key = "dfk_" + secrets.token_urlsafe(32)
    conn.execute(
        "INSERT INTO api_keys (id, user_id, key_prefix, key_hash, label) VALUES (%s, %s, %s, %s, %s)",
        (uuid.uuid4(), user.id, key[:10], _hash(key), label),
    )
    conn.commit()
    return key


def user_for_api_key(conn: psycopg.Connection, key: str) -> User:
    row = conn.execute(
        "SELECT u.id, u.email, u.plan FROM api_keys k JOIN users u ON u.id = k.user_id "
        "WHERE k.key_hash = %s AND k.revoked_at IS NULL",
        (_hash(key),),
    ).fetchone()
    if not row:
        raise AuthError("invalid or revoked API key")
    conn.execute("UPDATE api_keys SET last_used_at = now() WHERE key_hash = %s "
                 "AND (last_used_at IS NULL OR last_used_at < now() - interval '1 minute')", (_hash(key),))
    conn.commit()
    return User(row["id"], row["email"], PLANS[row["plan"]])


def list_api_keys(conn: psycopg.Connection, user: User) -> list[dict]:
    return conn.execute(
        "SELECT id, key_prefix, label, created_at, revoked_at FROM api_keys WHERE user_id = %s "
        "ORDER BY revoked_at IS NOT NULL, created_at DESC", (user.id,)
    ).fetchall()


def revoke_user_key(conn: psycopg.Connection, user: User, key_id: str) -> bool:
    n = conn.execute("UPDATE api_keys SET revoked_at = now() WHERE id = %s AND user_id = %s AND revoked_at IS NULL",
                     (key_id, user.id)).rowcount
    conn.commit()
    return n > 0


def set_plan(conn: psycopg.Connection, user: User, plan: str) -> User:
    if plan not in PLANS:
        raise ValueError(f"unknown plan {plan!r}")
    conn.execute("UPDATE users SET plan = %s WHERE id = %s", (plan, user.id))
    conn.commit()
    return User(user.id, user.email, PLANS[plan])


def revoke_api_key(conn: psycopg.Connection, key_prefix: str) -> int:
    n = conn.execute("UPDATE api_keys SET revoked_at = now() WHERE key_prefix = %s AND revoked_at IS NULL",
                     (key_prefix,)).rowcount
    conn.commit()
    return n


# --- metering: free views (fair use) + credits for data that leaves ---------------------------------

_MONTH = "date_trunc('month', now())::date"


def views_today(conn: psycopg.Connection, user: User) -> int:
    return conn.execute("SELECT count(*) AS n FROM row_views WHERE user_id = %s AND first_day = current_date",
                        (user.id,)).fetchone()["n"]


def check_views(conn: psycopg.Connection, user: User) -> None:
    """Viewing is free, but not unlimited: the daily fair-use cap stops bulk scraping through search."""
    if views_today(conn, user) >= user.plan.views_per_day:
        raise PlanError(f"Daily fair-use limit reached: {user.plan.views_per_day} new businesses viewed today. "
                        "Businesses you've already seen this month still open. Resets at midnight UTC.")


def viewed_this_month(conn: psycopg.Connection, user: User, business_id) -> bool:
    return conn.execute(f"SELECT 1 FROM row_views WHERE user_id = %s AND month = {_MONTH} AND business_id = %s",
                        (user.id, str(business_id))).fetchone() is not None


def record_views(conn: psycopg.Connection, user: User, business_ids: list) -> int:
    if not business_ids:
        return 0
    n = len(conn.execute(
        f"INSERT INTO row_views (user_id, month, business_id, first_day) "
        f"SELECT %s, {_MONTH}, unnest(%s::uuid[]), current_date ON CONFLICT DO NOTHING RETURNING business_id",
        (user.id, [str(b) for b in business_ids])).fetchall())
    conn.commit()
    return n


def credits(conn: psycopg.Connection, user: User) -> dict:
    """Plan credits reset monthly and are used first; top-ups carry over until used."""
    monthly = user.plan.credits_per_month
    used = conn.execute(f"SELECT count(*) AS n FROM credit_charges WHERE user_id = %s AND month = {_MONTH}",
                        (user.id,)).fetchone()["n"]
    granted = conn.execute("SELECT coalesce(sum(amount), 0)::int AS n FROM credit_grants WHERE user_id = %s",
                           (user.id,)).fetchone()["n"]
    spilled = conn.execute(   # usage beyond each month's plan credits came out of top-ups
        "SELECT coalesce(sum(greatest(n - %s, 0)), 0)::int AS n FROM "
        "(SELECT count(*) AS n FROM credit_charges WHERE user_id = %s GROUP BY month) x",
        (monthly, user.id)).fetchone()["n"]
    topup_left = max(0, granted - spilled)
    monthly_left = max(0, monthly - used)
    return {"monthly_allowance": monthly, "used_this_month": used, "monthly_left": monthly_left,
            "topup_left": topup_left, "left": monthly_left + topup_left}


def _already_charged(conn, user, ids) -> set[str]:
    rows = conn.execute(f"SELECT business_id FROM credit_charges WHERE user_id = %s AND month = {_MONTH} "
                        "AND business_id = ANY(%s::uuid[])", (user.id, [str(i) for i in ids])).fetchall()
    return {str(r["business_id"]) for r in rows}


def affordable(conn: psycopg.Connection, user: User, ids: list) -> list[str]:
    """The ids (in order) this user can receive right now: everything already paid for this month, plus as
    many new ones as their credits cover."""
    ids = [str(i) for i in ids]
    paid = _already_charged(conn, user, ids)
    left = credits(conn, user)["left"]
    out = []
    for i in ids:
        if i in paid:
            out.append(i)
        elif left > 0:
            out.append(i)
            left -= 1
    return out


def charge_credits(conn: psycopg.Connection, user: User, ids: list, kind: str) -> int:
    """Charge one credit per business not already charged this month. All or nothing."""
    ids = list(dict.fromkeys(str(i) for i in ids))
    new = [i for i in ids if i not in _already_charged(conn, user, ids)]
    left = credits(conn, user)["left"]
    if len(new) > left:
        raise InsufficientCredits(len(new), left)
    if new:
        conn.execute(f"INSERT INTO credit_charges (user_id, month, business_id, kind) "
                     f"SELECT %s, {_MONTH}, unnest(%s::uuid[]), %s ON CONFLICT DO NOTHING", (user.id, new, kind))
        conn.commit()
    return len(new)


def grant_credits(conn: psycopg.Connection, user: User, amount: int, reason: str | None = None) -> dict:
    conn.execute("INSERT INTO credit_grants (user_id, amount, reason) VALUES (%s, %s, %s)", (user.id, amount, reason))
    conn.commit()
    return credits(conn, user)


def record_usage(conn: psycopg.Connection, user: User, tool: str, rows: int, surface: str) -> None:
    """Call log (analytics / billing audit). rows = credits charged by the call."""
    conn.execute("INSERT INTO api_usage (user_id, tool, surface, rows_returned) VALUES (%s, %s, %s, %s)",
                 (user.id, tool, surface, rows))
    conn.commit()


def usage_summary(conn: psycopg.Connection, user: User) -> dict:
    return {
        "email": user.email,
        "plan": user.plan.name,
        "credits": credits(conn, user),
        "views_today": views_today(conn, user),
        "views_per_day": user.plan.views_per_day,
        "max_rows_per_call": user.plan.max_per_call,
        "mcp_enabled": user.plan.mcp,
        "exports_enabled": user.plan.export,
        "how_it_works": ("Searching and viewing in Deal Feed (web and AI chat) is free under a daily fair-use "
                         "limit. Credits are used when data leaves: 1 per business exported or returned by the REST "
                         "API, once per month per business."),
    }
