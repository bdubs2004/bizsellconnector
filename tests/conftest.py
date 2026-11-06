"""Test setup: a throwaway Postgres database rebuilt from the fixtures.

Set DEALFEED_TEST_DATABASE_URL to point somewhere else; the default is a local
`dealfeed_test` database. Its schema is DROPPED on every run.
"""
import os
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


def biz_id(conn, name_like: str) -> str:
    return str(conn.execute("SELECT id FROM businesses WHERE legal_name ILIKE %s", (name_like,)).fetchone()["id"])
