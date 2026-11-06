"""State business-registry sources: one registry, one streaming loader.

Every state below publishes its business-entity register as a free bulk file.
Each column layout was checked against the live dataset (Socrata metadata API
or a real download) in November 2026.

  IA  Iowa Secretary of State, Active Iowa Business Entities   Iowa Data Hub, CC BY 4.0
  CO  Colorado Secretary of State, Business Entities            data.colorado.gov, Public Domain
  NY  NY Department of State, Active Corporations               data.ny.gov
  CT  CT Secretary of the State, Business Registry master       data.ct.gov, Public Domain
  OR  Oregon Secretary of State, Active Businesses              data.oregon.gov
  TX  Texas Comptroller, Active Franchise Taxpayers             data.texas.gov

Not included (checked, not free/easy): MN, SD, NC, IN, SC (paid), WA (bulk
extract retired 2024), OH (new filings only), FL and AK (free, but their sites
block automated downloads; FL is fixed-width; add later from a manual download).

What gets loaded, for every state:
  * domestic for-profit entities only (LLC, corporation, partnerships, professional
    entities). Nonprofits, churches, co-ops, banks, insurers, trusts, name
    reservations and out-of-state ("foreign") registrations are skipped: they
    aren't acquisition targets, and a foreign registration date isn't the
    business's real age.
  * entities at least DEALFEED_MIN_AGE_YEARS old (default 5; 0 keeps everything)
  * names of registered agents / officers from the filing (no personal addresses)
Nothing is stored from columns we don't map; the untouched file stays in the raw folder.
"""
from __future__ import annotations

import csv
import io
import logging
import re
import time
import zipfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable, Iterator

import httpx
import psycopg

from .. import normalize as nz
from ..config import get_settings
from ..states_meta import SUPPORTED_STATES
from .common import ColumnSpec, _norm_header, finish_run, load_records, start_run

log = logging.getLogger(__name__)

csv.field_size_limit(10_000_000)


class Skip(Exception):
    """Raised by a mapper to drop a row; the message is the reason (counted in run notes)."""


# --- normalized entity types ---------------------------------------------------------------
LLC, CORP, PROF_CORP, PROF_LLC, LP, LLP, LLLP, GP = (
    "LLC", "Corporation", "Professional corporation", "Professional LLC",
    "Limited partnership", "LLP", "LLLP", "General partnership")


@dataclass(frozen=True)
class Fetch:
    kind: str                  # "socrata" or "url"
    domain: str = ""
    dataset: str = ""
    url: str = ""
    order: str = ":id"
    # Builds a SoQL $where so the state's server does the filtering. Gets the "formed before" cutoff.
    where: Callable[[date | None], str | None] | None = None


@dataclass(frozen=True)
class StateSource:
    key: str
    state: str
    name: str
    attribution: str
    license: str
    homepage: str
    fetch: Fetch
    spec: ColumnSpec
    mapper: Callable[["Row"], dict]
    group_by: str | None = None      # canonical field for long-format files (one row per role)
    group_mapper: Callable[[list["Row"]], list[tuple[str, dict]]] | None = None
    notes: str = ""


class Row:
    """One CSV row with access by canonical field name."""
    __slots__ = ("row", "cols")

    def __init__(self, row: dict, cols: dict[str, str]):
        self.row, self.cols = row, cols

    def __call__(self, f: str) -> str | None:
        c = self.cols.get(f)
        return nz.clean_text(self.row.get(c)) if c else None


def _person(*parts: str | None) -> str | None:
    s = " ".join(p for p in parts if p)
    return s.upper() if s.strip() else None


def _date(s: str | None) -> date | None:
    d = nz.parse_date(s)
    return d if d and d.year >= 1700 else None   # CT uses 0001-01-01 for "unknown"


def _naics(s: str | None) -> str | None:
    if not s:
        return None
    m = re.search(r"\b(\d{6})\b", s)
    return m.group(1) if m else None


def _in(values) -> str:
    return "(" + ",".join("'" + v.replace("'", "''") + "'" for v in values) + ")"


# ============================================================================================
# Iowa: Iowa Data Hub dataset 554 (moved off Socrata in 2026). Download is a zip with one CSV.
# ============================================================================================
IA_TYPES = {
    "34": LLC, "10": CORP, "61": CORP, "36": PROF_LLC, "12": PROF_CORP,
    "44": LLP, "56": LP, "58": LLLP, "43": GP,
}


def _ia(g: Row) -> dict:
    raw_type = g("corporation_type") or ""
    code = raw_type.split(" - ")[0].strip()
    etype = IA_TYPES.get(code)
    if not etype:
        raise Skip("not a domestic for-profit entity")
    lat, lon = nz.parse_money(g("ho_latitude")), nz.parse_money(g("ho_longitude"))
    return {
        "source_id": g("corp_number"), "legal_name": g("legal_name"), "entity_type": etype,
        "entity_type_raw": raw_type, "formation_date": _date(g("effective_date")), "status": "active",
        "address": (g("ho_address_1"), g("ho_address_2")), "city": g("ho_city"), "addr_state": g("ho_state"),
        "zip": g("ho_zip"), "latitude": lat, "longitude": lon,
        "people": [{"name": g("registered_agent").upper(), "role": "registered agent"}] if g("registered_agent") else [],
    }


IA = StateSource(
    key="ia_sos", state="IA", name="Active Iowa Business Entities",
    attribution="Iowa Secretary of State, Active Iowa Business Entities (Iowa Data Hub, CC BY 4.0)",
    license="CC BY 4.0", homepage="https://data.iowa.gov/catalog/dataset/554",
    fetch=Fetch(kind="url", url="https://idh-be.iowa.gov/api/v1/datasets/554/rows.csv"),
    spec=ColumnSpec(
        fields={
            "corp_number": ("corp_number", "Corp Number"),
            "legal_name": ("legal_name", "Legal Name"),
            "corporation_type": ("corporation_type", "Corporation Type"),
            "effective_date": ("effective_date", "Effective Date"),
            "registered_agent": ("registered_agent", "Registered Agent"),
            "ho_address_1": ("ho_address_1", "Home Office Address 1"),
            "ho_address_2": ("ho_address_2", "Home Office Address 2"),
            "ho_city": ("ho_city", "Home Office City"),
            "ho_state": ("ho_state", "Home Office State"),
            "ho_zip": ("ho_zip", "Home Office Zip"),
            "ho_latitude": ("ho_latitude",),
            "ho_longitude": ("ho_longitude",),
        },
        required=("corp_number", "legal_name", "corporation_type"),
    ),
    mapper=_ia,
    notes="File lists active entities only. Includes geocoded home-office coordinates.",
)


# ============================================================================================
# Colorado: data.colorado.gov 4ykn-tg5h. All statuses since 1864, so we filter server-side.
# ============================================================================================
CO_TYPES = {"DLLC": LLC, "DPC": CORP, "DPC-PBC": CORP, "DLP": LP, "DLLP": LLP, "DLLLP": LLLP, "GP": GP}
CO_STATUS = {"Good Standing": "active", "Exists": "active", "Delinquent": "delinquent", "Noncompliant": "delinquent"}


# Colorado appends status history to the name: "ACME, INC., Delinquent September 1, 2008"
_CO_SUFFIX = re.compile(
    r",?\s+(Delinquent|Noncompliant|Dissolved|Administratively Dissolved|Voluntarily Dissolved|Revoked|Withdrawn)"
    r"\s+([A-Z][a-z]+ \d{1,2}, \d{4})\s*$")


def co_clean_name(name: str | None) -> tuple[str | None, date | None]:
    if not name:
        return name, None
    m = _CO_SUFFIX.search(name)
    if not m:
        return name, None
    try:
        since = datetime.strptime(m.group(2), "%B %d, %Y").date()
    except ValueError:
        since = None
    return name[:m.start()].rstrip(" ,"), since


def _co(g: Row) -> dict:
    etype = CO_TYPES.get(g("entitytype") or "")
    if not etype:
        raise Skip("not a domestic for-profit entity")
    status = CO_STATUS.get(g("entitystatus") or "")
    if not status:
        raise Skip("dissolved or withdrawn")
    agent = g("agentorganizationname") or _person(g("agentfirstname"), g("agentmiddlename"), g("agentlastname"),
                                                  g("agentsuffix"))
    name, since = co_clean_name(g("entityname"))
    return {
        "source_id": g("entityid"), "legal_name": name, "entity_type": etype, "status_since": since,
        "entity_type_raw": g("entitytype"), "formation_date": _date(g("entityformdate")), "status": status,
        "address": (g("principaladdress1"), g("principaladdress2")), "city": g("principalcity"),
        "addr_state": g("principalstate"), "zip": g("principalzipcode"),
        "people": [{"name": agent.upper(), "role": "registered agent"}] if agent else [],
    }


CO = StateSource(
    key="co_sos", state="CO", name="Business Entities in Colorado",
    attribution="Colorado Department of State, Business Entities in Colorado (data.colorado.gov, Public Domain)",
    license="Public Domain", homepage="https://data.colorado.gov/d/4ykn-tg5h",
    fetch=Fetch(kind="socrata", domain="data.colorado.gov", dataset="4ykn-tg5h", order="entityid",
                where=lambda cutoff: (f"entitystatus in {_in(CO_STATUS)} AND entitytype in {_in(CO_TYPES)}"
                                      + (f" AND entityformdate < '{cutoff.isoformat()}'" if cutoff else ""))),
    spec=ColumnSpec(
        fields={k: (k,) for k in (
            "entityid", "entityname", "principaladdress1", "principaladdress2", "principalcity", "principalstate",
            "principalzipcode", "entitystatus", "entitytype", "agentfirstname", "agentmiddlename", "agentlastname",
            "agentsuffix", "agentorganizationname", "entityformdate")},
        required=("entityid", "entityname", "entitytype", "entitystatus"),
    ),
    mapper=_co,
    notes="'Delinquent' (missed periodic report) is kept with status 'delinquent'; dissolved entities are skipped.",
)


# ============================================================================================
# New York: data.ny.gov n9v6-gdp6, active entities only. No principal-office address for most
# entities: we use the "Location" address when present, else the address for service of process.
# ============================================================================================
NY_TYPES = {
    "DOMESTIC LIMITED LIABILITY COMPANY": LLC,
    "DOMESTIC BUSINESS CORPORATION": CORP,
    "DOMESTIC BENEFIT CORPORATION": CORP,
    "DOMESTIC PROFESSIONAL SERVICE CORPORATION": PROF_CORP,
    "DOMESTIC DESIGN PROFESSIONAL SERVICE CORPORATION": PROF_CORP,
    "DOMESTIC PROFESSIONAL SERVICE LIMITED LIABILITY COMPANY": PROF_LLC,
    "DOMESTIC LIMITED PARTNERSHIP": LP,
    "DOMESTIC REGISTERED LIMITED LIABILITY PARTNERSHIP": LLP,
}


def _ny(g: Row) -> dict:
    etype = NY_TYPES.get((g("entity_type") or "").upper())
    if not etype:
        raise Skip("not a domestic for-profit entity")
    if g("location_address_1"):
        addr = (g("location_address_1"), g("location_address_2"))
        city, st, zp, kind = g("location_city"), g("location_state"), g("location_zip"), "location"
    else:
        addr = (g("dos_process_address_1"), g("dos_process_address_2"))
        city, st, zp, kind = g("dos_process_city"), g("dos_process_state"), g("dos_process_zip"), "service_of_process"
    people = []
    if g("registered_agent_name"):
        people.append({"name": g("registered_agent_name").upper(), "role": "registered agent"})
    if g("ceo_name"):
        people.append({"name": g("ceo_name").upper(), "role": "chief executive officer"})
    county = (g("county") or "").upper() or None
    return {
        "source_id": g("dos_id"), "legal_name": g("current_entity_name"), "entity_type": etype,
        "entity_type_raw": g("entity_type"), "formation_date": _date(g("initial_dos_filing_date")),
        "status": "active", "address": addr, "city": city, "addr_state": st, "zip": zp, "address_kind": kind,
        "county": county, "people": people,
    }


NY = StateSource(
    key="ny_dos", state="NY", name="Active Corporations: Beginning 1800",
    attribution="New York Department of State, Active Corporations: Beginning 1800 (data.ny.gov)",
    license="NY Open Data terms", homepage="https://data.ny.gov/d/n9v6-gdp6",
    fetch=Fetch(kind="socrata", domain="data.ny.gov", dataset="n9v6-gdp6", order="dos_id",
                where=lambda cutoff: (f"entity_type in {_in(NY_TYPES)}"
                                      + (f" AND initial_dos_filing_date < '{cutoff.isoformat()}'" if cutoff else ""))),
    spec=ColumnSpec(
        fields={
            "dos_id": ("dos_id", "DOS ID"),
            "current_entity_name": ("current_entity_name", "Current Entity Name"),
            "initial_dos_filing_date": ("initial_dos_filing_date", "Initial DOS Filing Date"),
            "county": ("county", "County"),
            "entity_type": ("entity_type", "Entity Type"),
            "dos_process_address_1": ("dos_process_address_1", "DOS Process Address 1"),
            "dos_process_address_2": ("dos_process_address_2", "DOS Process Address 2"),
            "dos_process_city": ("dos_process_city", "DOS Process City"),
            "dos_process_state": ("dos_process_state", "DOS Process State"),
            "dos_process_zip": ("dos_process_zip", "DOS Process Zip"),
            "ceo_name": ("chairman_name", "CEO Name"),
            "registered_agent_name": ("registered_agent_name", "Registered Agent Name"),
            "location_address_1": ("location_address_1", "Location Address 1"),
            "location_address_2": ("location_address_2", "Location Address 2"),
            "location_city": ("location_city", "Location City"),
            "location_state": ("location_state", "Location State"),
            "location_zip": ("location_zip", "Location Zip"),
        },
        required=("dos_id", "current_entity_name", "entity_type"),
    ),
    mapper=_ny,
    notes="Most entities have no business location on file; the service-of-process address is used instead.",
)


# ============================================================================================
# Connecticut: data.ct.gov n7gp-d28j (Business Master). Officers live in a separate dataset
# (Principals, ka36-64k6) that also holds residence addresses; not loaded yet.
# ============================================================================================
CT_TYPES = {"LLC": LLC, "Stock": CORP, "B Corp": CORP, "Limited Partnership": LP, "LLP": LLP,
            "General Partnerships": GP}


def _ct(g: Row) -> dict:
    etype = CT_TYPES.get(g("business_type") or "")
    if not etype:
        raise Skip("not a for-profit entity type")
    if (g("status") or "") != "Active":
        raise Skip("not active")
    if (g("citizenship") or "").lower() != "domestic":
        raise Skip("foreign registration")
    return {
        "source_id": g("accountnumber") or g("id"), "legal_name": g("name"), "entity_type": etype,
        "entity_type_raw": g("business_type"), "formation_date": _date(g("date_registration")),
        "status": "active", "address": (g("street"), g("unit")), "city": g("city"), "addr_state": g("addr_state"),
        "zip": g("zip"), "naics": _naics(g("naics_sub_code")) or _naics(g("naics_code")), "people": [],
    }


CT = StateSource(
    key="ct_sots", state="CT", name="Connecticut Business Registry - Business Master",
    attribution="Connecticut Secretary of the State, Business Registry - Business Master (data.ct.gov, Public Domain)",
    license="Public Domain", homepage="https://data.ct.gov/d/n7gp-d28j",
    fetch=Fetch(kind="socrata", domain="data.ct.gov", dataset="n7gp-d28j", order="accountnumber",
                where=lambda cutoff: (f"status = 'Active' AND citizenship = 'Domestic' AND business_type in {_in(CT_TYPES)}"
                                      + (f" AND date_registration < '{cutoff.isoformat()}'" if cutoff else ""))),
    spec=ColumnSpec(
        fields={
            "id": ("id", "Id"),
            "accountnumber": ("accountnumber", "AccountNumber"),
            "name": ("name", "Name"),
            "business_type": ("business_type", "Business_Type"),
            "status": ("status", "Status"),
            "citizenship": ("citizenship", "Citizenship"),
            "date_registration": ("date_registration", "Date_Registration"),
            "street": ("billingstreet", "Business_Street"),
            "unit": ("billing_unit", "Business_Unit"),
            "city": ("billingcity", "Business_City"),
            "addr_state": ("billingstate", "Business_State"),
            "zip": ("billingpostalcode", "Business_ZIP"),
            "naics_code": ("naics_code", "NAICS_Code"),
            "naics_sub_code": ("naics_sub_code", "NAICS_Sub_Code"),
        },
        required=("accountnumber", "name", "business_type", "status"),
    ),
    mapper=_ct,
    notes="Self-reported NAICS codes on about half of records. Email and owner-demographic columns are never loaded.",
)


# ============================================================================================
# Oregon: data.oregon.gov tckn-sxa6, long format: one row per (business, role). Assumed business
# names (DBAs) are their own rows, linked to the owning entity by "Entity of Record Reg Number".
# ============================================================================================
OR_TYPES = {
    "DOMESTIC LIMITED LIABILITY COMPANY": LLC, "DOMESTIC BUSINESS CORPORATION": CORP,
    "DOMESTIC PROFESSIONAL CORPORATION": PROF_CORP, "DOMESTIC LIMITED PARTNERSHIP": LP,
    "DOMESTIC REGISTERED LIMITED LIABILITY PARTNERSHIP": LLP,
}
OR_DBA = "ASSUMED BUSINESS NAME"


def _or_group(rows: list[Row]) -> list[tuple[str, dict]]:
    first = rows[0]
    etype_raw = (first("entity_type") or "").upper()
    if etype_raw == OR_DBA:
        owner = first("entity_of_record_reg_number")
        if not owner:
            raise Skip("assumed name owned by an individual")
        return [("__dba__", {"owner": owner.split(".")[0], "name": first("business_name")})]
    etype = OR_TYPES.get(etype_raw)
    if not etype:
        raise Skip("not a domestic for-profit entity")
    rec = {
        "source_id": first("registry_number"), "legal_name": first("business_name"), "entity_type": etype,
        "entity_type_raw": first("entity_type"), "formation_date": _date(first("registry_date")),
        "status": "active", "people": [],
    }
    for r in rows:
        kind = (r("associated_name_type") or "").upper()
        if kind == "PRINCIPAL PLACE OF BUSINESS" or (kind == "MAILING ADDRESS" and "address" not in rec):
            rec.update(address=(r("address"), r("address_continued")), city=r("city"), addr_state=r("state"),
                       zip=r("zip"))
        elif kind in ("REGISTERED AGENT", "AUTHORIZED REPRESENTATIVE"):
            name = _person(r("first_name"), r("middle_name"), r("last_name"), r("suffix")) or r("entity_of_record_name")
            if name:
                rec["people"].append({"name": name.upper(), "role": kind.lower()})
    return [(rec["source_id"], rec)]


OR = StateSource(
    key="or_sos", state="OR", name="Active Businesses - ALL",
    attribution="Oregon Secretary of State, Active Businesses - ALL (data.oregon.gov)",
    license="Oregon open data terms", homepage="https://data.oregon.gov/d/tckn-sxa6",
    fetch=Fetch(kind="socrata", domain="data.oregon.gov", dataset="tckn-sxa6", order="registry_number",
                where=lambda cutoff: f"entity_type in {_in(list(OR_TYPES) + [OR_DBA])}"),
    spec=ColumnSpec(
        fields={
            "registry_number": ("registry_number", "Registry Number"),
            "business_name": ("business_name", "Business Name"),
            "entity_type": ("entity_type", "Entity Type"),
            "registry_date": ("registry_date", "Registry Date"),
            "associated_name_type": ("associated_name_type", "Associated Name Type"),
            "first_name": ("first_name", "First Name"),
            "middle_name": ("middle_name", "Middle Name"),
            "last_name": ("last_name", "Last Name"),
            "suffix": ("suffix", "Suffix"),
            "entity_of_record_reg_number": ("entity_of_record_reg_number", "Entity of Record Reg Number"),
            "entity_of_record_name": ("entity_of_record_name", "Entity of Record Name"),
            "address": ("address", "Address"),
            "address_continued": ("address_continued", "Address Continued"),
            "city": ("city", "City"),
            "state": ("state", "State"),
            "zip": ("zip", "Zip"),
        },
        required=("registry_number", "business_name", "entity_type", "associated_name_type"),
    ),
    mapper=lambda g: {},   # unused: Oregon is grouped
    group_by="registry_number",
    group_mapper=_or_group,
    notes="Long format (one row per business per role). Assumed business names become DBA names on the owner.",
)


# ============================================================================================
# Texas: Comptroller's Active Franchise Taxpayers, data.texas.gov 9cir-efmm. Not the SOS register,
# but every Texas LLC/corp/LP owes franchise tax, and it carries the SOS charter date and NAICS.
# Only the org-type codes we could confirm are loaded (CT Texas corp, CL Texas LLC, PL partnership).
# ============================================================================================
TX_TYPES = {"CT": CORP, "CL": LLC, "PL": LP}


def _tx(g: Row) -> dict:
    etype = TX_TYPES.get((g("org_type") or "").strip())
    if not etype:
        raise Skip("not a confirmed domestic for-profit type")
    if g("exempt_reason"):
        raise Skip("tax-exempt organization")
    if not g("sos_file_number"):
        raise Skip("no SOS registration")
    rt = (g("right_to_transact") or "").strip()
    return {
        "source_id": g("taxpayer_number"), "legal_name": g("taxpayer_name"), "entity_type": etype,
        "entity_type_raw": (g("org_type") or "").strip(), "formation_date": _date(g("sos_charter_date")),
        "status": "active" if rt == "A" else "delinquent",
        "address": (g("address"), None), "city": g("city"), "addr_state": g("addr_state"), "zip": g("zip"),
        "naics": _naics(g("naics")), "tx_county_code": g("county_code"), "people": [],
    }


TX = StateSource(
    key="tx_cpa", state="TX", name="Active Franchise Taxpayers",
    attribution="Texas Comptroller of Public Accounts, Active Franchise Tax Permit Holders (data.texas.gov)",
    license="Texas open data terms", homepage="https://data.texas.gov/d/9cir-efmm",
    fetch=Fetch(kind="socrata", domain="data.texas.gov", dataset="9cir-efmm", order="taxpayer_number",
                where=lambda cutoff: (f"taxpayer_organizational_type in {_in(TX_TYPES)} AND current_exempt_reason_code IS NULL"
                                      + (f" AND sos_charter_date < '{cutoff.isoformat()}'" if cutoff else ""))),
    spec=ColumnSpec(
        fields={
            "taxpayer_number": ("taxpayer_number", "Taxpayer Number"),
            "taxpayer_name": ("taxpayer_name", "Taxpayer Name"),
            "address": ("taxpayer_address", "Taxpayer Address"),
            "city": ("taxpayer_city", "Taxpayer City"),
            "addr_state": ("taxpayer_state", "Taxpayer State"),
            "zip": ("taxpayer_zip", "Taxpayer Zip"),
            "county_code": ("taxpayer_county_code", "Taxpayer County Code"),
            "org_type": ("taxpayer_organizational_type", "Taxpayer Organizational Type"),
            "sos_file_number": ("secretary_of_state_sos_or_coa_file_number", "Secretary of State (SOS) or COA File Number"),
            "sos_charter_date": ("sos_charter_date", "SOS Charter Date"),
            "right_to_transact": ("right_to_transact_business_code", "Right to Transact Business Code"),
            "exempt_reason": ("current_exempt_reason_code", "Current Exempt Reason Code"),
            "naics": ("_621111", "NAICS Code"),
        },
        required=("taxpayer_number", "taxpayer_name", "org_type"),
    ),
    mapper=_tx,
    notes="Address is the franchise-tax mailing address (sometimes an accountant). Lost right to transact = 'delinquent'.",
)


SOURCES: dict[str, StateSource] = {s.key: s for s in (IA, CO, NY, CT, OR, TX)}
assert tuple(s.state for s in SOURCES.values()) == SUPPORTED_STATES, "keep states_meta in sync"
BY_STATE: dict[str, StateSource] = {s.state: s for s in SOURCES.values()}
STATE_SOURCE_KEYS = tuple(SOURCES)


def get_source(key_or_state: str) -> StateSource:
    k = key_or_state.strip()
    if k in SOURCES:
        return SOURCES[k]
    if k.upper() in BY_STATE:
        return BY_STATE[k.upper()]
    raise KeyError(f"Unknown state source {key_or_state!r}. Available: {', '.join(SUPPORTED_STATES)}")


# --- reading ---------------------------------------------------------------------------------

def _open_text(path: Path) -> io.TextIOBase:
    """Open a CSV, or the single CSV inside a zip, as text."""
    if zipfile.is_zipfile(path):
        z = zipfile.ZipFile(path)
        names = [n for n in z.namelist() if n.lower().endswith(".csv")]
        if len(names) != 1:
            raise ValueError(f"{path} should contain exactly one .csv, found {names}")
        return io.TextIOWrapper(z.open(names[0]), encoding="utf-8-sig", errors="replace", newline="")
    return open(path, encoding="utf-8-sig", errors="replace", newline="")


def read_header(path: Path) -> list[str]:
    with _open_text(path) as f:
        return next(csv.reader(f))


def iter_rows(path: Path) -> Iterator[dict]:
    with _open_text(path) as f:
        yield from csv.DictReader(f)


def inspect(key: str, path: Path) -> dict:
    src = get_source(key)
    header = read_header(path)
    return {"source": src.key, "columns": header, "mapping": src.spec.resolve(header)}


# --- loading ---------------------------------------------------------------------------------

def min_age_cutoff(min_age_years: int | None, today: date | None = None) -> date | None:
    years = get_settings().min_age_years if min_age_years is None else min_age_years
    if not years:
        return None
    t = today or date.today()
    try:
        return t.replace(year=t.year - years)
    except ValueError:               # Feb 29
        return t.replace(year=t.year - years, day=28)


def _finish(rec: dict, src: StateSource, cutoff: date | None) -> tuple[str, dict]:
    name = rec.get("legal_name")
    if not rec.get("source_id") or not name:
        raise Skip("missing id or name")
    fd: date | None = rec.get("formation_date")
    if cutoff and fd and fd >= cutoff:
        raise Skip("younger than minimum age")
    addr = rec.get("address") or (None, None)
    canon = {
        "registry_state": src.state,
        "legal_name": name,
        "normalized_name": nz.normalize_name(name),
        "core_name": nz.core_name(name),
        "entity_type": rec.get("entity_type"),
        "entity_type_raw": rec.get("entity_type_raw"),
        "formation_date": fd.isoformat() if fd else None,
        "status": rec.get("status", "active"),
        "status_since": rec["status_since"].isoformat() if rec.get("status_since") else None,
        "address_line1": nz.normalize_address(*addr),
        "address_kind": rec.get("address_kind", "principal"),
        "city": (rec.get("city") or "").upper() or None,
        "addr_state": (rec.get("addr_state") or "").upper() or None,
        "zip5": nz.zip5(rec.get("zip")),
        "county": rec.get("county"),
        "latitude": rec.get("latitude"),
        "longitude": rec.get("longitude"),
        "naics": rec.get("naics"),
        "people": [p for p in rec.get("people", []) if p.get("name")],
        "dba_names": rec.get("dba_names", []),
    }
    if rec.get("tx_county_code"):
        canon["tx_county_code"] = rec["tx_county_code"]
    return str(rec["source_id"]), canon


def map_file(src: StateSource, path: Path, cutoff: date | None, skipped: Counter) -> Iterator[tuple[str, dict, dict]]:
    """Yield (source_id, canon, raw) for every row that passes the filters."""
    header = read_header(path)
    cols = src.spec.resolve(header)
    if src.group_by:
        groups: dict[str, list[Row]] = defaultdict(list)
        key_col = cols[src.group_by]
        for row in iter_rows(path):
            k = (row.get(key_col) or "").strip()
            if k:
                groups[k].append(Row(row, cols))
        dbas: dict[str, list[str]] = defaultdict(list)
        entities: dict[str, dict] = {}
        for rows in groups.values():
            try:
                for sid, rec in src.group_mapper(rows):
                    if sid == "__dba__":
                        dbas[rec["owner"]].append(rec["name"])
                    else:
                        entities[sid] = rec
            except Skip as s:
                skipped[str(s)] += 1
        del groups
        for sid, rec in entities.items():
            rec["dba_names"] = sorted(set(dbas.get(sid, [])))[:25]
            try:
                source_id, canon = _finish(rec, src, cutoff)
                yield source_id, canon, {}
            except Skip as s:
                skipped[str(s)] += 1
        return
    for row in iter_rows(path):
        try:
            source_id, canon = _finish(src.mapper(Row(row, cols)), src, cutoff)
            yield source_id, canon, {}
        except Skip as s:
            skipped[str(s)] += 1


def load(conn: psycopg.Connection, key: str, path: Path, pulled_at: datetime | None = None,
         min_age_years: int | None = None) -> dict:
    """Stage one state's file for the next build. The raw file itself is the archive; the database keeps
    a slim provenance row per entity in source_records and the business row the build produces."""
    src = get_source(key)
    cutoff = min_age_cutoff(min_age_years)
    run_id = start_run(conn, src.key, path)
    skipped: Counter = Counter()
    try:
        loaded = load_records(conn, src.key, run_id, map_file(src, path, cutoff, skipped),
                              pulled_at or datetime.now(timezone.utc), stage=True)
        notes = "skipped: " + ", ".join(f"{k} {v:,}" for k, v in skipped.most_common()) if skipped else None
        if cutoff:
            notes = f"min formation cutoff {cutoff}; " + (notes or "")
        finish_run(conn, run_id, "ok", rows_read=loaded + sum(skipped.values()), rows_loaded=loaded, notes=notes)
        return {"source": src.key, "run_id": run_id, "rows_loaded": loaded, "skipped": dict(skipped),
                "min_formation_cutoff": cutoff.isoformat() if cutoff else None}
    except Exception as e:
        conn.rollback()
        finish_run(conn, run_id, "failed", notes=repr(e)[:2000])
        raise


# --- fetching --------------------------------------------------------------------------------

def _headers() -> dict:
    h = {"User-Agent": "dealfeed-ingest/0.2"}
    tok = get_settings().socrata_app_token
    if tok:
        h["X-App-Token"] = tok
    return h


RETRY_STATUS = {429, 500, 502, 503, 504}


def _get(client: httpx.Client, url: str, params: dict, attempts: int = 6, backoff: float = 2.0) -> httpx.Response:
    """GET with exponential backoff. Socrata portals drop long responses and rate-limit
    anonymous clients now and then; a monthly multi-million-row pull must ride through that."""
    for i in range(attempts):
        try:
            r = client.get(url, params=params)
            if r.status_code not in RETRY_STATUS:
                r.raise_for_status()
                return r
            err: Exception = httpx.HTTPStatusError(f"HTTP {r.status_code}", request=r.request, response=r)
        except httpx.TransportError as e:
            err = e
        if i == attempts - 1:
            raise err
        wait = backoff * (2 ** i)
        log.warning("%s (attempt %d/%d), retrying in %.0fs", err, i + 1, attempts, wait)
        time.sleep(wait)
    raise RuntimeError("unreachable")


def fetch(key: str, dest_dir: Path, min_age_years: int | None = None, page_size: int = 20_000,
          max_rows: int | None = None, client: httpx.Client | None = None) -> Path:
    """Download a state's file into dest_dir. Socrata sources are paged with the source's
    server-side filter applied, so only rows we'd keep are transferred."""
    src = get_source(key)
    dest_dir.mkdir(parents=True, exist_ok=True)
    own = client is None
    client = client or httpx.Client(timeout=httpx.Timeout(300, connect=30), follow_redirects=True, headers=_headers())
    try:
        if src.fetch.kind == "url":
            dest = dest_dir / f"{src.key}.zip"
            tmp = dest.with_suffix(".part")
            with client.stream("GET", src.fetch.url) as r:
                r.raise_for_status()
                with open(tmp, "wb") as f:
                    for chunk in r.iter_bytes(1 << 20):
                        f.write(chunk)
            if not zipfile.is_zipfile(tmp):
                dest = dest.with_suffix(".csv")
            tmp.replace(dest)
            return dest

        dest = dest_dir / f"{src.key}.csv"
        tmp = dest.with_suffix(".part")
        url = f"https://{src.fetch.domain}/resource/{src.fetch.dataset}.csv"
        where = src.fetch.where(min_age_cutoff(min_age_years)) if src.fetch.where else None
        offset, total = 0, 0
        with open(tmp, "w", encoding="utf-8", newline="") as out:
            while True:
                limit = page_size if max_rows is None else min(page_size, max_rows - total)
                if limit <= 0:
                    break
                params = {"$limit": limit, "$offset": offset, "$order": src.fetch.order}
                if where:
                    params["$where"] = where
                r = _get(client, url, params)
                lines = r.text.splitlines(keepends=True)
                body = lines if offset == 0 else lines[1:]
                if offset == 0 and not lines:
                    break
                n = len(list(csv.reader(io.StringIO("".join(lines[1:])))))
                out.writelines(body if offset == 0 else body)
                total += n
                log.info("%s: %d rows", src.key, total)
                if n < limit:
                    break
                offset += limit
        tmp.replace(dest)
        return dest
    finally:
        if own:
            client.close()


def header_matches(src: StateSource, header: list[str]) -> bool:
    lookup = {_norm_header(h) for h in header}
    return all(any(_norm_header(a) in lookup for a in src.spec.fields[f]) for f in src.spec.required)
