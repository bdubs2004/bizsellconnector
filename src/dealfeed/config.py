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

    # Matching thresholds (rapidfuzz token_sort_ratio, 0-100)
    match_auto: float = field(default_factory=lambda: float(_env("DEALFEED_MATCH_AUTO", "92")))
    match_gray: float = field(default_factory=lambda: float(_env("DEALFEED_MATCH_GRAY", "80")))

    # Local single-user mode for the stdio MCP server (your own Claude Desktop).
    local_user_email: str | None = field(default_factory=lambda: _env("DEALFEED_LOCAL_USER"))

    # Public URL of the MCP server (used for OAuth metadata when hosted)
    public_url: str = field(default_factory=lambda: _env("DEALFEED_PUBLIC_URL", "http://localhost:8001"))


def get_settings() -> Settings:
    return Settings()
