"""Iowa Secretary of State: Active Iowa Business Entities (data.iowa.gov, CC BY 4.0).

The exact column headers of the Socrata export were not verifiable from the
build environment, so the mapping below accepts several spellings. Run
`dealfeed inspect <file> --source ia_sos` on the first real download; if a
required column isn't found the error lists the real headers so you can add
the alias here.

Privacy rule (scope 2.6): registered-agent ADDRESS columns are dropped before
anything is stored in the database. Small-business agents are often the owner
at their home address. The untouched file still lives in the raw folder.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import psycopg

from .. import normalize as nz
from .common import ColumnSpec, finish_run, load_records, read_header, scan_csv, start_run

SOURCE = "ia_sos"
DOWNLOAD_URL = "https://data.iowa.gov/api/views/ez5t-3qay/rows.csv?accessType=DOWNLOAD"
ATTRIBUTION = "Iowa Secretary of State, Active Iowa Business Entities, data.iowa.gov (CC BY 4.0)"

SPEC = ColumnSpec(
    fields={
        "corp_number": ("Corp Number", "Corporation Number", "Business No", "Business Number", "Entity Number",
                        "Entity ID", "Business ID", "Corp No"),
        "legal_name": ("Legal Name", "Business Name", "Entity Name", "Corporation Name", "Name"),
        "entity_type": ("Corporation Type", "Corp Type", "Entity Type", "Business Type", "Type"),
        "effective_date": ("Effective Date", "Registration Date", "Formation Date", "Date of Incorporation",
                           "Filing Date", "Original Filing Date", "Effective"),
        "ra_name": ("Registered Agent", "Registered Agent Name", "Agent Name", "RA Name"),
        "po_address1": ("Home Office Address 1", "Home Office Address", "Principal Office Address 1",
                        "Principal Office Address", "Principal Address 1", "Principal Address", "Office Address 1",
                        "Address 1", "Address"),
        "po_address2": ("Home Office Address 2", "Principal Office Address 2", "Principal Address 2",
                        "Office Address 2", "Address 2"),
        "po_city": ("Home Office City", "Principal Office City", "Principal City", "Office City", "City"),
        "po_state": ("Home Office State", "Principal Office State", "Principal State", "Office State", "State"),
        "po_zip": ("Home Office Zip", "Home Office ZIP", "Principal Office Zip", "Principal Zip", "Office Zip",
                   "Zip", "ZIP Code", "Zip Code"),
        "modifier": ("Modifier", "Domestic/Foreign", "Domestic Foreign"),
        "state_of_formation": ("State of Incorporation", "State of Formation", "Home State", "Jurisdiction"),
    },
    required=("corp_number", "legal_name"),
    # Normalized header (lowercase alnum). Anything that looks like an RA address/location.
    drop_patterns=(r"^(ra|registeredagent|agent)(address|street|city|state|zip|location|loc)",
                   r"^(ra|registeredagent|agent).*(address|location)"),
)


def _row_to_record(row: dict, cols: dict[str, str], dropped: set[str]) -> tuple[str, dict, dict] | None:
    def g(f):
        c = cols.get(f)
        return nz.clean_text(row.get(c)) if c else None

    corp = g("corp_number")
    name = g("legal_name")
    if not corp or not name:
        return None
    eff = nz.parse_date(g("effective_date"))
    canon = {
        "legal_name": name,
        "normalized_name": nz.normalize_name(name),
        "core_name": nz.core_name(name),
        "entity_type": g("entity_type"),
        "modifier": g("modifier"),
        "state_of_formation": g("state_of_formation") or ("IA" if (g("modifier") or "").upper().startswith("DOM") else None),
        "formation_date": eff.isoformat() if eff else None,
        "ra_name": g("ra_name"),
        "address_line1": nz.normalize_address(g("po_address1"), g("po_address2")),
        "city": (g("po_city") or "").upper() or None,
        "state": (g("po_state") or "").upper() or None,
        "zip5": nz.zip5(g("po_zip")),
    }
    raw = {k: v for k, v in row.items() if k not in dropped}
    return corp, canon, raw


def load(conn: psycopg.Connection, path: Path, pulled_at: datetime | None = None) -> dict:
    header = read_header(path)
    cols = SPEC.resolve(header)
    dropped = set(SPEC.dropped(header))
    run_id = start_run(conn, SOURCE, path)
    try:
        df = scan_csv(path).collect()
        records = (r for r in (_row_to_record(row, cols, dropped) for row in df.iter_rows(named=True)) if r)
        loaded = load_records(conn, SOURCE, run_id, records, pulled_at or datetime.now(timezone.utc))
        finish_run(conn, run_id, "ok", rows_read=df.height, rows_loaded=loaded,
                   notes=f"dropped columns: {sorted(dropped)}" if dropped else None)
        return {"run_id": run_id, "rows_read": df.height, "rows_loaded": loaded, "dropped_columns": sorted(dropped)}
    except Exception as e:
        conn.rollback()
        finish_run(conn, run_id, "failed", notes=repr(e)[:2000])
        raise


def inspect(path: Path) -> dict:
    header = read_header(path)
    return {"columns": header, "mapping": SPEC.resolve(header), "dropped": SPEC.dropped(header)}


__all__ = ["load", "inspect", "SOURCE", "DOWNLOAD_URL", "ATTRIBUTION"]
