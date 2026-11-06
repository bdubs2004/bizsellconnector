"""Lenient input parsing for values AIs (and people) send in many shapes.

Accept the obvious variants ("Iowa", "ia", "350k", "$1.05M", "10.5%", "20+ years", "Polk County, IA",
"Researching"), and when something truly can't be understood, raise ValueError with a message that says
exactly what's accepted, so an AI can correct itself in one retry.
"""
from __future__ import annotations

import re

from .states_meta import STATE_NAMES

_NAME_TO_CODE = {v.upper(): k for k, v in STATE_NAMES.items()}


def state_code(v: str | None) -> str | None:
    """'IA', 'ia', 'Iowa', ' iowa ' -> 'IA'. Empty -> None."""
    if v is None:
        return None
    s = str(v).strip().upper().removesuffix(" STATE")
    if not s:
        return None
    if s in STATE_NAMES:
        return s
    if s in _NAME_TO_CODE:
        return _NAME_TO_CODE[s]
    covered = ", ".join(f"{k} ({n})" for k, n in STATE_NAMES.items())
    if len(s) == 2:
        raise ValueError(f"No data for state {s!r} yet. Covered states: {covered}.")
    raise ValueError(f"Unknown state {v!r}. Use a two-letter code. Covered states: {covered}.")


_MULT = {"K": 1e3, "THOUSAND": 1e3, "M": 1e6, "MM": 1e6, "MIL": 1e6, "MILLION": 1e6, "B": 1e9, "BILLION": 1e9}


def number(v):
    """1050000, '1,050,000', '$1.05M', '350k', '2.5 million', '10.5%' -> float. None/'' -> None."""
    if v is None or isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v
    s = str(v).strip().upper().replace(",", "").replace("$", "").replace("%", "").replace("X", "").strip()
    if not s:
        return None
    m = re.fullmatch(r"(-?\d+(?:\.\d+)?)\s*([A-Z]+)?", s)
    if not m or (m.group(2) and m.group(2) not in _MULT):
        raise ValueError(f"Couldn't read {v!r} as a number. Use plain numbers like 1050000 or '1.05M'.")
    return float(m.group(1)) * (_MULT[m.group(2)] if m.group(2) else 1)


def loose_int(v):
    """20, '20', '20+', '20 years', '20.0' -> 20."""
    if v is None or isinstance(v, bool) or isinstance(v, int):
        return v
    if isinstance(v, float):
        return int(v)
    m = re.match(r"\s*(\d+)", str(v))
    if not m:
        if not str(v).strip():
            return None
        raise ValueError(f"Couldn't read {v!r} as a whole number.")
    return int(m.group(1))


def county(v: str) -> str:
    """'Polk', 'Polk County', 'Polk County, IA', 'polk county iowa', 'St. Louis' -> 'POLK' / 'ST LOUIS'."""
    s = str(v).strip().upper().split(",")[0]
    for name in sorted(_NAME_TO_CODE, key=len, reverse=True):         # trailing state name
        s = re.sub(rf"\s+{re.escape(name)}$", "", s)
    s = re.sub(r"\s+(COUNTY|PARISH|BOROUGH)$", "", s.strip())
    return re.sub(r"\s+", " ", s.replace(".", "")).strip()


SORT_ALIASES = {
    "priority": "priority", "combined": "priority", "both": "priority", "default": "priority", "best": "priority",
    "sell": "sell", "score": "sell", "sell_score": "sell", "likely_to_sell": "sell", "likely to sell": "sell",
    "sellscore": "sell",
    "buy": "buy", "buy_score": "buy", "fit": "buy", "buy_fit": "buy", "buyscore": "buy",
}


def sort_key(v: str | None) -> str:
    if v is None or not str(v).strip():
        return "priority"
    k = str(v).strip().lower().replace("-", "_")
    if k in SORT_ALIASES:
        return SORT_ALIASES[k]
    raise ValueError(f"Unknown sort {v!r}. Use 'priority' (likely to sell x buy-box fit), 'sell' or 'buy'.")


STATUSES = ("new", "researching", "contacted", "passed")


def pipeline_status(v: str | None, allow_all: bool = False) -> str | None:
    if v is None:
        return None
    s = str(v).strip().lower()
    if allow_all and s in ("", "all", "any", "*"):
        return None
    if s in STATUSES:
        return s
    raise ValueError(f"Unknown status {v!r}. Use one of: {', '.join(STATUSES)}.")


def business_id(v) -> str:
    return str(v or "").strip().strip("{}").lower()
