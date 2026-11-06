"""Users, API keys, plans and usage metering (scope 6.1 guardrails, Section 9 pricing)."""
from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass

import psycopg


@dataclass(frozen=True)
class Plan:
    name: str
    rows_per_month: int | None    # None = unlimited (metered/billed instead)
    max_per_call: int
    mcp: bool
    export: bool
    max_export_rows: int


PLANS = {
    "free": Plan("free", rows_per_month=50, max_per_call=10, mcp=False, export=False, max_export_rows=0),
    "searcher": Plan("searcher", rows_per_month=1_000, max_per_call=50, mcp=True, export=True, max_export_rows=500),
    "fund": Plan("fund", rows_per_month=5_000, max_per_call=50, mcp=True, export=True, max_export_rows=2_000),
    # Usage-billed; the cap is an abuse ceiling, not a product limit.
    "api": Plan("api", rows_per_month=100_000, max_per_call=50, mcp=True, export=True, max_export_rows=5_000),
}


class AuthError(Exception):
    pass


class PlanError(Exception):
    """The caller's plan doesn't allow this, or the monthly quota is used up."""


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
    return User(row["id"], row["email"], PLANS[row["plan"]])


def revoke_api_key(conn: psycopg.Connection, key_prefix: str) -> int:
    n = conn.execute("UPDATE api_keys SET revoked_at = now() WHERE key_prefix = %s AND revoked_at IS NULL",
                     (key_prefix,)).rowcount
    conn.commit()
    return n


# --- metering ---------------------------------------------------------------

def rows_used_this_month(conn: psycopg.Connection, user: User) -> int:
    return conn.execute(
        "SELECT coalesce(sum(rows_returned), 0) AS n FROM api_usage "
        "WHERE user_id = %s AND called_at >= date_trunc('month', now())",
        (user.id,),
    ).fetchone()["n"]


def allowed_rows(conn: psycopg.Connection, user: User, requested: int) -> int:
    """How many rows this call may return given per-call and monthly limits. Raises if zero."""
    n = max(1, min(requested, user.plan.max_per_call))
    if user.plan.rows_per_month is not None:
        remaining = user.plan.rows_per_month - rows_used_this_month(conn, user)
        if remaining <= 0:
            raise PlanError(f"Monthly row limit reached for the {user.plan.name} plan "
                            f"({user.plan.rows_per_month} rows). Resets on the 1st, or upgrade.")
        n = min(n, remaining)
    return n


def record_usage(conn: psycopg.Connection, user: User, tool: str, rows: int, surface: str) -> None:
    conn.execute("INSERT INTO api_usage (user_id, tool, surface, rows_returned) VALUES (%s, %s, %s, %s)",
                 (user.id, tool, surface, rows))
    conn.commit()


def usage_summary(conn: psycopg.Connection, user: User) -> dict:
    used = rows_used_this_month(conn, user)
    return {
        "email": user.email,
        "plan": user.plan.name,
        "rows_used_this_month": used,
        "rows_per_month": user.plan.rows_per_month,
        "max_rows_per_call": user.plan.max_per_call,
        "mcp_enabled": user.plan.mcp,
        "exports_enabled": user.plan.export,
    }
