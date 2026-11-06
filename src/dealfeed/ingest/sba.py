"""SBA FOIA loan data: PPP, 7(a) and 504 (data.sba.gov, public domain).

Files are large (PPP is ~5 GB nationally), so they are scanned lazily with
polars and filtered to the target states before anything is loaded.

Privacy rules applied here (scope 2.6):
  * PPP demographic columns (race, ethnicity, gender, veteran) are dropped.
  * PPP loans to sole proprietors / self-employed individuals / independent
    contractors are skipped: the "borrower name" is a person, and those
    businesses are below the ETA size range anyway.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path

import polars as pl
import psycopg

from .. import normalize as nz
from ..states_meta import SUPPORTED_STATES
from .common import ColumnSpec, finish_run, load_records, read_header, scan_csv, stable_id, start_run

PPP_SPEC = ColumnSpec(
    fields={
        "loan_number": ("LoanNumber", "Loan Number"),
        "name": ("BorrowerName",),
        "address": ("BorrowerAddress",),
        "city": ("BorrowerCity",),
        "state": ("BorrowerState",),
        "zip": ("BorrowerZip",),
        "approval_date": ("DateApproved",),
        "amount": ("CurrentApprovalAmount", "InitialApprovalAmount"),
        "initial_amount": ("InitialApprovalAmount",),
        "jobs": ("JobsReported",),
        "naics": ("NAICSCode",),
        "lender": ("ServicingLenderName", "OriginatingLender"),
        "status": ("LoanStatus",),
        "business_type": ("BusinessType",),
        "business_age_desc": ("BusinessAgeDescription",),
        "county": ("ProjectCountyName",),
        "processing_method": ("ProcessingMethod",),   # PPP = first draw, PPS = second draw
        "forgiveness_amount": ("ForgivenessAmount",),
        "nonprofit": ("NonProfit",),
    },
    required=("loan_number", "name", "state", "amount"),
    drop_patterns=(r"^race$", r"^ethnicity$", r"^gender$", r"^veteran$"),
)

PPP_SKIP_BUSINESS_TYPES = {"sole proprietorship", "self-employed individuals", "independent contractors"}
# Nonprofits got PPP too; they aren't acquisition targets.
PPP_NONPROFIT_MARKERS = ("non-profit", "nonprofit", "501(c)")

SBA_SPEC = ColumnSpec(
    fields={
        "program": ("Program",),
        "name": ("BorrName", "BorrowerName", "Borrower Name"),
        "address": ("BorrStreet", "BorrowerStreet", "BorrowerAddress"),
        "city": ("BorrCity", "BorrowerCity"),
        "state": ("BorrState", "BorrowerState"),
        "zip": ("BorrZip", "BorrowerZip"),
        "amount": ("GrossApproval", "Gross Approval"),
        "approval_date": ("ApprovalDate", "Approval Date"),
        "lender": ("BankName", "ThirdPartyLender_Name", "CDC_Name", "LenderName"),
        "status": ("LoanStatus", "Loan Status"),
        "naics": ("NaicsCode", "NAICSCode"),
        "business_age_desc": ("BusinessAge", "Business Age"),
        "business_type": ("BusinessType",),
        "jobs": ("JobsSupported", "Jobs Supported"),
        "county": ("ProjectCounty",),
        "term_months": ("TerminMonths", "TermInMonths"),
        "subprogram": ("subpgmdesc", "SubprogramDescription"),
    },
    required=("name", "state", "amount", "approval_date"),
)


def _filtered(path: Path, state_col: str, states: list[str]) -> pl.DataFrame:
    return (
        scan_csv(path)
        .filter(pl.col(state_col).str.strip_chars().str.to_uppercase().is_in([s.upper() for s in states]))
        .collect()
    )


def _common_canon(g) -> dict:
    name = g("name")
    d = nz.parse_date(g("approval_date"))
    return {
        "legal_name": name,
        "normalized_name": nz.normalize_name(name),
        "core_name": nz.core_name(name),
        "address_line1": nz.normalize_address(g("address")),
        "city": (g("city") or "").upper() or None,
        "state": (g("state") or "").upper() or None,
        "zip5": nz.zip5(g("zip")),
        "approval_date": d.isoformat() if d else None,
        "amount": nz.parse_money(g("amount")),
        "jobs": nz.parse_int(g("jobs")),
        "naics": (g("naics") or "").split(".")[0] or None,
        "lender": g("lender"),
        "status": g("status"),
        "business_type": g("business_type"),
        "business_age_desc": g("business_age_desc"),
        "county": (g("county") or "").upper() or None,
    }


def _insert_loans(conn: psycopg.Connection, source: str, program: str, ids: list[str]) -> None:
    """Mirror freshly loaded loan records into sba_loans (business_id filled by matching)."""
    conn.execute(
        """
        INSERT INTO sba_loans (id, source_record_id, program, amount, approval_date, lender, status,
                               jobs_reported, naics_code, business_age_desc, county, state)
        SELECT sr.id, sr.id, %(program)s, (sr.canon->>'amount')::numeric, (sr.canon->>'approval_date')::date,
               sr.canon->>'lender', sr.canon->>'status', (sr.canon->>'jobs')::int, sr.canon->>'naics',
               sr.canon->>'business_age_desc', sr.canon->>'county', sr.canon->>'state'
          FROM source_records sr
         WHERE sr.source = %(source)s AND sr.source_id = ANY(%(ids)s)
        ON CONFLICT (source_record_id) DO UPDATE
           SET amount = EXCLUDED.amount, approval_date = EXCLUDED.approval_date, lender = EXCLUDED.lender,
               status = EXCLUDED.status, jobs_reported = EXCLUDED.jobs_reported, naics_code = EXCLUDED.naics_code,
               business_age_desc = EXCLUDED.business_age_desc, county = EXCLUDED.county
        """,
        {"program": program, "source": source, "ids": ids},
    )
    conn.commit()


def load_ppp(conn: psycopg.Connection, path: Path, states: list[str] | None = None,
             pulled_at: datetime | None = None) -> dict:
    states = states or list(SUPPORTED_STATES)
    header = read_header(path)
    cols = PPP_SPEC.resolve(header)
    dropped = set(PPP_SPEC.dropped(header))
    run_id = start_run(conn, "ppp", path)
    try:
        df = _filtered(path, cols["state"], states)
        skipped = skipped_np = 0
        records = []
        for row in df.iter_rows(named=True):
            def g(f, row=row):
                c = cols.get(f)
                return nz.clean_text(row.get(c)) if c else None
            btype = (g("business_type") or "").strip().lower()
            if btype in PPP_SKIP_BUSINESS_TYPES:
                skipped += 1
                continue
            if (g("nonprofit") or "").upper() == "Y" or any(m in btype for m in PPP_NONPROFIT_MARKERS):
                skipped_np += 1
                continue
            canon = _common_canon(g)
            canon["program"] = "ppp"
            canon["draw"] = "second" if (g("processing_method") or "").upper() == "PPS" else "first"
            canon["forgiveness_amount"] = nz.parse_money(g("forgiveness_amount"))
            records.append((g("loan_number"), canon, {k: v for k, v in row.items() if k not in dropped}))
        loaded = load_records(conn, "ppp", run_id, records, pulled_at or datetime.now(timezone.utc))
        _insert_loans(conn, "ppp", "ppp", [r[0] for r in records])
        finish_run(conn, run_id, "ok", rows_read=df.height, rows_loaded=loaded,
                   notes=f"skipped {skipped} individual-borrower and {skipped_np} nonprofit loans; "
                         f"dropped {sorted(dropped)}")
        return {"run_id": run_id, "rows_in_states": df.height, "rows_loaded": loaded, "skipped_individuals": skipped,
                "skipped_nonprofits": skipped_np}
    except Exception as e:
        conn.rollback()
        finish_run(conn, run_id, "failed", notes=repr(e)[:2000])
        raise


def load_sba(conn: psycopg.Connection, path: Path, program: str, states: list[str] | None = None,
             pulled_at: datetime | None = None) -> dict:
    """Load a 7(a) or 504 FOIA file. `program` is '7a' or '504'."""
    if program not in ("7a", "504"):
        raise ValueError("program must be '7a' or '504'")
    source = f"sba_{program}"
    states = states or list(SUPPORTED_STATES)
    header = read_header(path)
    cols = SBA_SPEC.resolve(header)
    run_id = start_run(conn, source, path)
    try:
        df = _filtered(path, cols["state"], states)
        records = []
        for row in df.iter_rows(named=True):
            def g(f, row=row):
                c = cols.get(f)
                return nz.clean_text(row.get(c)) if c else None
            canon = _common_canon(g)
            canon["program"] = program
            canon["term_months"] = nz.parse_int(g("term_months"))
            # FOIA 7(a)/504 rows carry no loan number; build a stable synthetic key.
            key = "|".join(str(canon.get(k)) for k in ("normalized_name", "zip5", "approval_date", "amount"))
            source_id = hashlib.sha1(key.encode()).hexdigest()[:20]
            records.append((source_id, canon, row))
        loaded = load_records(conn, source, run_id, records, pulled_at or datetime.now(timezone.utc))
        _insert_loans(conn, source, program, [r[0] for r in records])
        finish_run(conn, run_id, "ok", rows_read=df.height, rows_loaded=loaded)
        return {"run_id": run_id, "rows_in_states": df.height, "rows_loaded": loaded}
    except Exception as e:
        conn.rollback()
        finish_run(conn, run_id, "failed", notes=repr(e)[:2000])
        raise


def inspect(path: Path, kind: str) -> dict:
    spec = PPP_SPEC if kind == "ppp" else SBA_SPEC
    header = read_header(path)
    return {"columns": header, "mapping": spec.resolve(header), "dropped": spec.dropped(header)}


__all__ = ["load_ppp", "load_sba", "inspect", "stable_id"]
