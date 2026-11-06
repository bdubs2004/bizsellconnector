"""Census Geocoder batch lookup: lat/lon + county for business addresses.

Free, no key. Batch endpoint takes up to 10,000 addresses per request as a CSV
upload. NOTE: written against the documented format but not yet exercised
against the live service (the build environment couldn't reach census.gov).
Run `dealfeed geocode --limit 50` first and eyeball the results.
"""
from __future__ import annotations

import csv
import io
import logging

import httpx
import psycopg

log = logging.getLogger(__name__)

BATCH_URL = "https://geocoding.geo.census.gov/geocoder/geographies/addressbatch"
BATCH_SIZE = 5000

# Iowa county FIPS (state 19) -> name. FIPS codes are odd numbers in alphabetical order.
_IA_COUNTIES = """Adair Adams Allamakee Appanoose Audubon Benton Black_Hawk Boone Bremer Buchanan Buena_Vista Butler
Calhoun Carroll Cass Cedar Cerro_Gordo Cherokee Chickasaw Clarke Clay Clayton Clinton Crawford Dallas Davis Decatur
Delaware Des_Moines Dickinson Dubuque Emmet Fayette Floyd Franklin Fremont Greene Grundy Guthrie Hamilton Hancock
Hardin Harrison Henry Howard Humboldt Ida Iowa Jackson Jasper Jefferson Johnson Jones Keokuk Kossuth Lee Linn Louisa
Lucas Lyon Madison Mahaska Marion Marshall Mills Mitchell Monona Monroe Montgomery Muscatine O'Brien Osceola Page
Palo_Alto Plymouth Pocahontas Polk Pottawattamie Poweshiek Ringgold Sac Scott Shelby Sioux Story Tama Taylor Union
Van_Buren Wapello Warren Washington Wayne Webster Winnebago Winneshiek Woodbury Worth Wright""".split()
COUNTY_BY_FIPS = {("19", f"{2 * i + 1:03d}"): n.replace("_", " ").upper() for i, n in enumerate(_IA_COUNTIES)}
assert len(COUNTY_BY_FIPS) == 99


def _parse_response(text: str) -> dict[str, dict]:
    out = {}
    for row in csv.reader(io.StringIO(text)):
        # id, input, Match/No_Match/Tie, Exact/Non_Exact, matched addr, "lon,lat", tiger id, side, state, county, tract, block
        if len(row) < 3 or row[2] != "Match":
            continue
        try:
            lon, lat = (float(x) for x in row[5].split(","))
        except (ValueError, IndexError):
            continue
        state_fp = row[8] if len(row) > 8 else None
        county_fp = row[9] if len(row) > 9 else None
        out[row[0]] = {"lat": lat, "lon": lon, "county": COUNTY_BY_FIPS.get((state_fp, county_fp))}
    return out


def geocode_missing(conn: psycopg.Connection, limit: int | None = None) -> dict:
    rows = conn.execute(
        "SELECT id, address_line1, city, state, zip FROM businesses "
        "WHERE latitude IS NULL AND address_line1 IS NOT NULL AND status <> 'inactive' "
        "ORDER BY id LIMIT %s", (limit,)
    ).fetchall()
    matched = 0
    for start in range(0, len(rows), BATCH_SIZE):
        chunk = rows[start:start + BATCH_SIZE]
        buf = io.StringIO()
        w = csv.writer(buf)
        for r in chunk:
            w.writerow([str(r["id"]), r["address_line1"], r["city"] or "", r["state"] or "", r["zip"] or ""])
        resp = httpx.post(
            BATCH_URL,
            data={"benchmark": "Public_AR_Current", "vintage": "Current_Current"},
            files={"addressFile": ("addresses.csv", buf.getvalue(), "text/csv")},
            timeout=600,
        )
        resp.raise_for_status()
        results = _parse_response(resp.text)
        with conn.cursor() as cur:
            cur.executemany(
                "UPDATE businesses SET latitude = %s, longitude = %s, county = coalesce(%s, county) WHERE id = %s",
                [(v["lat"], v["lon"], v["county"], k) for k, v in results.items()],
            )
        conn.commit()
        matched += len(results)
        log.info("geocoded %d/%d", matched, len(rows))
    return {"attempted": len(rows), "matched": matched}
