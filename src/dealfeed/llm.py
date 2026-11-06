"""Optional Claude calls for gray-zone matching and NAICS classification.

Off unless DEALFEED_LLM=on and ANTHROPIC_API_KEY is set. Every caller has a
non-LLM fallback (manual review queue / leave unclassified), so the pipeline
runs fine without it.
"""
from __future__ import annotations

import logging
from typing import Literal

from pydantic import BaseModel, Field

from .config import get_settings

log = logging.getLogger(__name__)


class SameBusiness(BaseModel):
    verdict: Literal["yes", "no", "unsure"]
    reason: str = Field(description="One short sentence")


class NaicsGuess(BaseModel):
    naics_code: str = Field(description="6-digit 2022 NAICS code, or empty string if unknown")
    confidence: float = Field(description="0 to 1")


def enabled() -> bool:
    s = get_settings()
    return s.llm_enabled and bool(s.anthropic_api_key)


def _client():
    import anthropic  # optional dependency: pip install dealfeed[llm]
    return anthropic.Anthropic(api_key=get_settings().anthropic_api_key)


def _parse(output_model, prompt: str):
    import anthropic
    s = get_settings()
    try:
        resp = _client().messages.parse(
            model=s.llm_model,
            max_tokens=1024,
            output_config={"effort": "low"},
            messages=[{"role": "user", "content": prompt}],
            output_format=output_model,
        )
    except (anthropic.RateLimitError, anthropic.APIConnectionError, anthropic.APIStatusError) as e:
        log.warning("LLM call failed, falling back: %s", e)
        return None
    if resp.stop_reason == "refusal":
        return None
    return resp.parsed_output


def same_business(a: dict, b: dict) -> SameBusiness | None:
    fields = ("legal_name", "address_line1", "city", "zip5")
    fmt = lambda r: "\n".join(f"  {f}: {r.get(f)}" for f in fields)  # noqa: E731
    prompt = (
        "Two records from different government datasets (Iowa Secretary of State and SBA loan data). "
        "Are they the same small business? Small spelling and suffix differences (LLC, Inc) are normal. "
        "Different street addresses in the same town can still be the same business if it moved. "
        "Answer 'unsure' if you can't tell.\n\n"
        f"Record A:\n{fmt(a)}\n\nRecord B:\n{fmt(b)}"
    )
    return _parse(SameBusiness, prompt)


def classify_naics(name: str, extra_context: str = "") -> NaicsGuess | None:
    prompt = (
        "Assign the most likely 6-digit 2022 NAICS code to this Iowa business from its name"
        + (" and context" if extra_context else "")
        + ". If the name gives no real clue, return an empty code with confidence 0.\n\n"
        f"Name: {name}\n{extra_context}"
    )
    return _parse(NaicsGuess, prompt)
