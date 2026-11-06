"""Name and address normalization (scope 3.2 step 2).

Two name forms:
  normalize_name("Smith Pest Control, L.L.C.") -> "SMITH PEST CONTROL LLC"
  core_name("Smith Pest Control, L.L.C.")      -> "SMITH PEST CONTROL"
`core_name` strips entity suffixes and filler so SOS and SBA spellings line up.
"""
from __future__ import annotations

import re
from datetime import date, datetime

import usaddress

_PUNCT = re.compile(r"[^\w\s&]")
_SPACES = re.compile(r"\s+")

# Multi-word phrases first, then single tokens. Applied after uppercasing and
# stripping punctuation, so "L.L.C." has already become "LLC".
_ENTITY_PHRASES = [
    (r"\bLIMITED LIABILITY COMPANY\b", "LLC"),
    (r"\bLIMITED LIABILITY CO\b", "LLC"),
    (r"\bPROFESSIONAL LIMITED LIABILITY COMPANY\b", "PLLC"),
    (r"\bPROFESSIONAL CORPORATION\b", "PC"),
    (r"\bLIMITED PARTNERSHIP\b", "LP"),
    (r"\bLIMITED LIABILITY PARTNERSHIP\b", "LLP"),
    (r"\bCORPORATION\b", "CORP"),
    (r"\bINCORPORATED\b", "INC"),
    (r"\bCOMPANY\b", "CO"),
    (r"\bLIMITED\b", "LTD"),
]
_ENTITY_TOKENS = {"LLC", "PLLC", "INC", "CORP", "CO", "LTD", "LP", "LLP", "PC", "PA", "LC"}
_FILLER_TOKENS = {"THE", "OF", "AND", "&"}


def normalize_name(name: str | None) -> str:
    if not name:
        return ""
    s = name.upper().replace(".", "")
    s = _PUNCT.sub(" ", s)
    s = _SPACES.sub(" ", s).strip()
    for pat, repl in _ENTITY_PHRASES:
        s = re.sub(pat, repl, s)
    return _SPACES.sub(" ", s).strip()


def core_name(name: str | None) -> str:
    """Name with entity suffixes and filler words removed, for matching."""
    tokens = normalize_name(name).split()
    # Strip trailing entity designators ("... CO INC" -> "...")
    while tokens and tokens[-1] in _ENTITY_TOKENS:
        tokens.pop()
    tokens = [t for t in tokens if t not in _FILLER_TOKENS]
    return " ".join(tokens)


def zip5(z: str | None) -> str | None:
    if not z:
        return None
    digits = re.sub(r"\D", "", str(z))
    if len(digits) < 5:
        # Leading zeros lost by spreadsheets; Iowa ZIPs start with 5 so this is rare.
        return digits.zfill(5) if digits else None
    return digits[:5]


# Invisible characters (control, zero-width, bidi overrides, BOM) are stripped from every field we ingest:
# public data ends up in an AI's context, and hidden text is a classic prompt-injection trick.
_HIDDEN = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff]")


def clean_text(s: str | None) -> str | None:
    if s is None:
        return None
    s = _SPACES.sub(" ", _HIDDEN.sub("", str(s))).strip()
    return s or None


def normalize_address(line1: str | None, line2: str | None = None) -> str | None:
    """Uppercase street address with standard abbreviations, via usaddress.

    Falls back to a simple cleanup when usaddress can't tag the string.
    """
    raw = " ".join(p for p in (line1, line2) if p)
    raw = clean_text(raw)
    if not raw:
        return None
    try:
        tagged, _ = usaddress.tag(raw)
    except usaddress.RepeatedLabelError:
        return raw.upper()
    order = [
        "AddressNumber", "StreetNamePreDirectional", "StreetName", "StreetNamePostType",
        "StreetNamePostDirectional", "OccupancyType", "OccupancyIdentifier",
        "USPSBoxType", "USPSBoxID",
    ]
    parts = [tagged[k] for k in order if k in tagged]
    out = " ".join(parts) if parts else raw
    return _abbrev(out.upper().replace(".", "").replace(",", ""))


_STREET_ABBR = {
    "STREET": "ST", "AVENUE": "AVE", "ROAD": "RD", "DRIVE": "DR", "BOULEVARD": "BLVD",
    "LANE": "LN", "COURT": "CT", "PLACE": "PL", "HIGHWAY": "HWY", "PARKWAY": "PKWY",
    "CIRCLE": "CIR", "SUITE": "STE", "NORTH": "N", "SOUTH": "S", "EAST": "E", "WEST": "W",
}


def _abbrev(s: str) -> str:
    return " ".join(_STREET_ABBR.get(t, t) for t in s.split())


_DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%m/%d/%y", "%Y%m%d")


def parse_date(s: str | None) -> date | None:
    if not s:
        return None
    s = str(s).strip()
    # Socrata sometimes emits "2001-05-03T00:00:00.000"; datetime text with a space too.
    s = s.split(" ")[0] if re.match(r"^\d{4}-\d{2}-\d{2} ", s) else s
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def parse_money(s: str | None) -> float | None:
    if s in (None, ""):
        return None
    try:
        return float(re.sub(r"[^\d.\-]", "", str(s)))
    except ValueError:
        return None


def parse_int(s: str | None) -> int | None:
    v = parse_money(s)
    return int(v) if v is not None else None


# Commercial registered-agent services. If the RA is one of these the owner
# hired a service; if the RA is an individual, the business is more likely
# owner-operated. Extend as you see names in the real file.
_RA_SERVICE_PATTERNS = [
    r"\bCT CORPORATION\b", r"\bCORPORATION SERVICE CO", r"\bREGISTERED AGENT", r"\bNATIONAL REGISTERED",
    r"\bINCORP SERVICES\b", r"\bNORTHWEST REGISTERED\b", r"\bCOGENCY GLOBAL\b", r"\bLEGALINC\b",
    r"\bUNITED STATES CORPORATION AGENTS\b", r"\bBUSINESS FILINGS\b", r"\bZENBUSINESS\b",
    r"\bHARBOR COMPLIANCE\b", r"\bLAW (FIRM|OFFICE)", r"\bATTORNEY", r"\bP ?C$", r"\bPLC$", r"\bLLP$",
]


def looks_like_ra_service(name: str | None) -> bool:
    n = normalize_name(name)
    if not n:
        return False
    if any(re.search(p, n) for p in _RA_SERVICE_PATTERNS):
        return True
    # Any entity designator means the RA is a company, not a person.
    return bool(set(n.split()) & _ENTITY_TOKENS)
