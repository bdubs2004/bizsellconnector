"""Runtime settings, all from environment variables (see .env.example)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str | None = None) -> str | None:
    val = os.environ.get(name)
    return val if val not in (None, "") else default


@dataclass(frozen=True)
class Settings:
    database_url: str = field(
        default_factory=lambda: _env("DATABASE_URL", "postgresql://postgres@localhost:5432/dealfeed")
    )
    # Where raw downloaded files live. Local folder now; swap for R2/S3 in Phase 3.
    raw_dir: Path = field(default_factory=lambda: Path(_env("DEALFEED_RAW_DIR", "./data/raw")))

    # LLM (optional). With no key, gray-zone matches go straight to manual review
    # and unclassified businesses stay unclassified.
    anthropic_api_key: str | None = field(default_factory=lambda: _env("ANTHROPIC_API_KEY"))
    llm_model: str = field(default_factory=lambda: _env("DEALFEED_LLM_MODEL", "claude-haiku-5-5"))
    llm_enabled: bool = field(default_factory=lambda: _env("DEALFEED_LLM", "off") == "on")

    # Entities formed more recently than this are skipped at ingest (0 = keep all).
    min_age_years: int = field(default_factory=lambda: int(_env("DEALFEED_MIN_AGE_YEARS", "5")))
    # Optional Socrata app token: raises the anonymous rate limit on state open-data portals.
    socrata_app_token: str | None = field(default_factory=lambda: _env("SOCRATA_APP_TOKEN"))

    # Default SBA loan rate for buy scores until a user sets their own buy box. An assumption, shown as such.
    reference_rate_pct: float = field(default_factory=lambda: float(_env("DEALFEED_REFERENCE_RATE", "10.5")))

    # Matching thresholds (rapidfuzz token_sort_ratio, 0-100)
    match_auto: float = field(default_factory=lambda: float(_env("DEALFEED_MATCH_AUTO", "92")))
    match_gray: float = field(default_factory=lambda: float(_env("DEALFEED_MATCH_GRAY", "80")))

    # Local single-user mode for the stdio MCP server (your own Claude Desktop).
    local_user_email: str | None = field(default_factory=lambda: _env("DEALFEED_LOCAL_USER"))

    # Web app. Auth is a dev-only email sign-in until Clerk/Supabase is wired up;
    # it refuses to run when DEALFEED_ENV=production.
    env: str = field(default_factory=lambda: _env("DEALFEED_ENV", "development"))
    web_auth: str = field(default_factory=lambda: _env("DEALFEED_WEB_AUTH", "dev"))
    session_secret: str | None = field(default_factory=lambda: _env("DEALFEED_SESSION_SECRET"))
    dev_signup_plan: str = field(default_factory=lambda: _env("DEALFEED_DEV_SIGNUP_PLAN", "searcher"))

    # Reverse proxies whose X-Forwarded-For we trust (comma-separated IPs). Default: only localhost.
    trusted_proxies: str = field(default_factory=lambda: _env("DEALFEED_TRUSTED_PROXIES", "127.0.0.1"))

    # Business contact shown on the privacy / removal-request page. Use a business address, never a personal one.
    contact_email: str = field(default_factory=lambda: _env("DEALFEED_CONTACT_EMAIL", "privacy@example.com"))

    # Public URL of the MCP server (used for OAuth metadata when hosted)
    public_url: str = field(default_factory=lambda: _env("DEALFEED_PUBLIC_URL", "http://localhost:8001"))


def get_settings() -> Settings:
    return Settings()
