"""Display formatting for the web app. State registries store names in ALL CAPS; we show them the way
people write them ("Thrifty Pest Control, Inc.") and keep the exact legal name on the business page."""
from __future__ import annotations

import json
import re

KEEP_UPPER = {"LLC", "L.L.C.", "LLP", "LP", "L.P.", "PC", "P.C.", "PLLC", "PA", "P.A.", "USA", "U.S.A.", "US",
              "II", "III", "IV", "VI", "DBA", "HVAC", "CPA", "DDS", "MD", "NA", "N.A.", "JV", "ATM", "RV", "TV",
              "AC", "A/C", "UPS", "K9", "S.C."}
TITLE_ABBR = {"ST.", "MT.", "FT.", "DR.", "JR.", "SR.", "MR.", "MRS.", "MS.", "CTR.", "SVC.", "SVCS.", "BLDG.", "BROS."}
LOWER = {"and", "of", "the", "for", "a", "an", "in", "on", "at", "by", "to", "de", "del", "la"}
_VOWELS = set("AEIOUY")
_TOKEN = re.compile(r"(\s+)")


def _cap_word(w: str) -> str:
    out, start = [], True
    for i, ch in enumerate(w):
        if ch.isalpha():
            if start:
                # "SWANSON'S" -> "Swanson's", but "O'BRIEN" -> "O'Brien"
                after_apostrophe = i > 0 and w[i - 1] in "'’"
                run = re.match(r"[A-Za-z]+", w[i:]).group(0)
                out.append(ch.lower() if after_apostrophe and len(run) == 1 else ch.upper())
            else:
                out.append(ch.lower())
            start = False
        else:
            out.append(ch)
            start = not ch.isdigit()
    return "".join(out)


def display_name(name: str | None) -> str:
    """Title-case an all-caps registry name, keeping legal suffixes and acronyms intact."""
    if not name or any(c.islower() for c in name):
        return name or ""
    parts = _TOKEN.split(name.strip())
    out, first = [], True
    for p in parts:
        if not p or p.isspace():
            out.append(p)
            continue
        core = p.strip(",;:()\"")
        letters = re.sub(r"[^A-Z]", "", core)
        if core in TITLE_ABBR:
            out.append(_cap_word(p))
        elif core in KEEP_UPPER or (letters and not (set(letters) & _VOWELS) and len(letters) <= 4):
            out.append(p)                      # LLC, P.C., HVAC, J&J, TLC, B&K
        elif not first and core.lower() in LOWER:
            out.append(p.lower())
        else:
            w = _cap_word(p)
            if re.match(r"Mc[a-z]{2,}", w):       # MCDONALD -> McDonald
                w = "Mc" + w[2].upper() + w[3:]
            out.append(w)
        first = False
    return "".join(out)


def script_json(value) -> str:
    """JSON safe to embed in <script type="application/json">: '<' can't close the tag."""
    return json.dumps(value).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
