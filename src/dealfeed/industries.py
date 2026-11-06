"""Industry definitions for v1 and backups (scope Section 4).

Each industry has NAICS codes, name keywords used by the rules-based
classifier, and rough payroll-to-revenue ratios used to turn a payroll
estimate into a revenue *range*. The ratios are placeholder guesses: replace
them with real benchmarks (RMA, BizMiner, IBISWorld, or SBA 7(a) deal data)
before showing revenue to customers.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Industry:
    slug: str
    label: str
    naics: tuple[str, ...]
    include: tuple[str, ...]           # regexes on normalized name
    exclude: tuple[str, ...] = ()
    payroll_to_revenue: tuple[float, float] = (0.30, 0.45)   # payroll / revenue, low-high
    eta_demand: str = "medium"          # low, medium, high
    v1: bool = False
    # Acquisition profile for the buy score. PLACEHOLDER benchmarks (common ETA rules of thumb), to be
    # replaced with real comps (e.g. SBA 7(a) deal sizes, broker data) before customers rely on them.
    sde_margin: tuple[float, float] = (0.12, 0.22)            # SDE / revenue, low-high
    recurring: float = 0.5                                    # 0 = one-off jobs, 1 = contracts/renewals
    typical_multiple: tuple[float, float] = (2.5, 3.5)        # price / SDE


INDUSTRIES: tuple[Industry, ...] = (
    Industry(
        slug="pest_control", sde_margin=(0.18, 0.28), recurring=1.0, typical_multiple=(3.0, 5.0), label="Pest control", naics=("561710",),
        include=(r"\bPEST\b", r"EXTERMINAT", r"\bTERMITE", r"\bBUG\b", r"\bRODENT"),
        payroll_to_revenue=(0.30, 0.42), eta_demand="high", v1=True,
    ),
    Industry(
        slug="commercial_cleaning", sde_margin=(0.10, 0.20), recurring=0.8, typical_multiple=(2.5, 3.5), label="Commercial cleaning / janitorial", naics=("561720",),
        include=(r"JANITORIAL", r"\bCUSTODIAL", r"\bCOMMERCIAL CLEANING", r"\bBUILDING (MAINTENANCE|SERVICES)",
                 r"\bOFFICE CLEANING", r"\bCLEANING (SERVICE|SERVICES|SOLUTIONS|SYSTEMS|CO)\b"),
        exclude=(r"DRY CLEAN", r"\bCARPET", r"\bCAR ?WASH", r"\bWINDOW", r"\bDUCT", r"\bCHIMNEY", r"\bPRESSURE WASH"),
        payroll_to_revenue=(0.45, 0.60), eta_demand="high", v1=True,
    ),
    Industry(
        slug="insurance_agency", sde_margin=(0.25, 0.40), recurring=1.0, typical_multiple=(3.0, 5.0), label="Independent insurance agency", naics=("524210",),
        include=(r"\bINSURANCE (AGENCY|AGENCIES|SERVICES|GROUP|ASSOCIATES|CENTER)", r"\bINS AGENCY\b",
                 r"\bINSURANCE\b.*\bAGENCY\b"),
        exclude=(r"\bMUTUAL\b", r"\bINSURANCE CO\b", r"\bASSURANCE CO", r"\bREINSURANCE"),
        payroll_to_revenue=(0.35, 0.50), eta_demand="high", v1=True,
    ),
    Industry(
        slug="auto_repair", sde_margin=(0.10, 0.20), recurring=0.3, typical_multiple=(2.0, 3.0), label="Auto repair", naics=("811111",),
        include=(r"\bAUTO (REPAIR|SERVICE|CARE|CENTER)", r"\bAUTOMOTIVE\b", r"\bGARAGE\b", r"\bMUFFLER", r"\bTRANSMISSION"),
        exclude=(r"\bSALES\b", r"\bDEALER", r"\bPARTS\b", r"\bBODY\b"),
        payroll_to_revenue=(0.28, 0.40), eta_demand="medium",
    ),
    Industry(
        slug="bookkeeping_tax", sde_margin=(0.25, 0.40), recurring=0.9, typical_multiple=(2.5, 4.0), label="Bookkeeping / tax prep", naics=("541213", "541219"),
        include=(r"\bTAX (SERVICE|SERVICES|PREP)", r"\bBOOKKEEPING", r"\bACCOUNTING SERVICES?\b", r"\bINCOME TAX"),
        exclude=(r"\bCPA\b", r"\bCERTIFIED PUBLIC"),
        payroll_to_revenue=(0.40, 0.55), eta_demand="medium",
    ),
    Industry(
        slug="landscaping", sde_margin=(0.10, 0.20), recurring=0.6, typical_multiple=(2.0, 3.0), label="Landscaping maintenance", naics=("561730",),
        include=(r"\bLAWN", r"\bLANDSCAP", r"\bTREE (SERVICE|CARE)", r"\bSNOW REMOVAL", r"\bTURF\b", r"\bMOWING"),
        payroll_to_revenue=(0.35, 0.50), eta_demand="medium",
    ),
)

BY_SLUG = {i.slug: i for i in INDUSTRIES}
BY_NAICS = {code: i for i in INDUSTRIES for code in i.naics}

# Free-text aliases an AI caller might send in `industry` / keywords.
_ALIASES = {
    "pest": "pest_control", "pest control": "pest_control", "exterminator": "pest_control",
    "janitorial": "commercial_cleaning", "cleaning": "commercial_cleaning",
    "commercial cleaning": "commercial_cleaning", "custodial": "commercial_cleaning",
    "insurance": "insurance_agency", "insurance agency": "insurance_agency",
    "insurance agencies": "insurance_agency", "independent insurance": "insurance_agency",
    "auto repair": "auto_repair", "mechanic": "auto_repair", "automotive repair": "auto_repair",
    "bookkeeping": "bookkeeping_tax", "tax prep": "bookkeeping_tax", "tax preparation": "bookkeeping_tax",
    "landscaping": "landscaping", "lawn care": "landscaping", "lawn": "landscaping",
}


def resolve_industry(text: str) -> Industry | None:
    t = text.strip().lower()
    if t in BY_SLUG:
        return BY_SLUG[t]
    if t in _ALIASES:
        return BY_SLUG[_ALIASES[t]]
    if re.fullmatch(r"\d{2,6}", t):
        return BY_NAICS.get(t)
    for alias, slug in _ALIASES.items():
        if alias in t:
            return BY_SLUG[slug]
    return None


def classify_by_name(normalized_name: str) -> tuple[Industry, float] | None:
    """Rules-based classification on the business name. Returns (industry, confidence)."""
    for ind in INDUSTRIES:
        if any(re.search(p, normalized_name) for p in ind.exclude):
            continue
        hits = sum(1 for p in ind.include if re.search(p, normalized_name))
        if hits:
            return ind, min(0.55 + 0.1 * (hits - 1), 0.75)
    return None
