"""Test setup: a throwaway Postgres database rebuilt from the fixtures.

Set DEALFEED_TEST_DATABASE_URL to point somewhere else; the default is a local
`dealfeed_test` database. Its schema is DROPPED on every run.
"""
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

TEST_DB = os.environ.get("DEALFEED_TEST_DATABASE_URL", "postgresql://postgres@localhost:5432/dealfeed_test")
os.environ["DATABASE_URL"] = TEST_DB
os.environ.setdefault("DEALFEED_LLM", "off")
# Fixtures include a business formed in 2023 to test the "too young" score rule; the separate
# min-age ingest filter has its own test.
os.environ["DEALFEED_MIN_AGE_YEARS"] = "0"

from dealfeed import db, pipeline  # noqa: E402
from dealfeed.ingest import sba, states  # noqa: E402

FIX = Path(__file__).parent / "fixtures"
PULL_1 = datetime(2026, 10, 1, tzinfo=timezone.utc)


def reset_db():
    conn = db.connect(TEST_DB)
    conn.execute("DROP SCHEMA public CASCADE")
    conn.execute("CREATE SCHEMA public")
    conn.commit()
    db.migrate(conn)
    return conn


def load_fixtures(conn):
    for key in states.STATE_SOURCE_KEYS:
        states.load(conn, key, FIX / f"{key}_sample.csv", PULL_1)
    sba.load_ppp(conn, FIX / "ppp_sample.csv", None, PULL_1)
    sba.load_sba(conn, FIX / "sba_7a_sample.csv", "7a", None, PULL_1)
    return pipeline.build(conn)


@pytest.fixture(scope="session")
def built():
    """Session-wide DB with fixtures loaded and built. Tests must not mutate businesses."""
    conn = reset_db()
    result = load_fixtures(conn)
    yield conn, result
    conn.close()


@pytest.fixture
def conn(built):
    return built[0]


@pytest.fixture(autouse=True)
def _reset_rate_limits():
    from dealfeed import security
    security.limiter.reset()
    yield


def page_csrf(client, path: str = "/login") -> str:
    html = client.get(path).text
    m = re.search(r'name="csrf_token" value="([^"]+)"', html) or re.search(r'"X-CSRF-Token": "([^"]+)"', html)
    return m.group(1)


def web_login(client, email: str, next_url: str = "/search"):
    """Sign in like a browser: take the CSRF token from the login form, then attach the new session's token
    to every later request (as HTMX does via <body hx-headers>)."""
    r = client.post("/login", data={"email": email, "next": next_url, "csrf_token": page_csrf(client)},
                    follow_redirects=False)
    client.headers["X-CSRF-Token"] = page_csrf(client, "/privacy")
    return r


def biz_id(conn, name_like: str) -> str:
    return str(conn.execute("SELECT id FROM businesses WHERE legal_name ILIKE %s", (name_like,)).fetchone()["id"])
