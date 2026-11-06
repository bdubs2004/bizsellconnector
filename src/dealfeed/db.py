"""Postgres access. Plain psycopg 3 + SQL; no ORM."""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import psycopg
from psycopg.rows import dict_row

from .config import get_settings

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"


def connect(url: str | None = None) -> psycopg.Connection:
    return psycopg.connect(url or get_settings().database_url, row_factory=dict_row)


@contextmanager
def transaction(url: str | None = None) -> Iterator[psycopg.Connection]:
    conn = connect(url)
    try:
        with conn.transaction():
            yield conn
    finally:
        conn.close()


def request_conn() -> Iterator[psycopg.Connection]:
    """FastAPI dependency: one connection per request. Swap for psycopg_pool when hosted."""
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


COMPACT_TABLES = ("businesses", "source_records", "business_people", "registry_stage", "sba_loans",
                  "size_estimates")


def compact(url: str | None = None) -> dict:
    """VACUUM FULL + ANALYZE the big tables, returning space to the OS. Locks each table while it runs
    (seconds to minutes), so run it after a load, not while users are searching. Needed after the
    first full load (every row is written twice: insert, then score); monthly pulls only touch changed rows."""
    conn = psycopg.connect(url or get_settings().database_url, autocommit=True, row_factory=dict_row)
    try:
        before = conn.execute("SELECT pg_database_size(current_database()) AS b").fetchone()["b"]
        for t in COMPACT_TABLES:
            conn.execute(f"VACUUM (FULL, ANALYZE) {t}")
        after = conn.execute("SELECT pg_database_size(current_database()) AS b").fetchone()["b"]
        return {"before_mb": round(before / 2**20), "after_mb": round(after / 2**20)}
    finally:
        conn.close()


def migrate(conn: psycopg.Connection) -> list[str]:
    """Apply every migrations/*.sql file not yet applied. Returns names applied."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY, applied_at TIMESTAMPTZ DEFAULT now())"
    )
    done = {r["name"] for r in conn.execute("SELECT name FROM schema_migrations").fetchall()}
    applied = []
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if path.name in done:
            continue
        conn.execute(path.read_text())
        conn.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (path.name,))
        applied.append(path.name)
    conn.commit()
    return applied
