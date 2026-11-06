"""Multi-state registry loading, filters, and cross-state search."""
import zipfile
from collections import Counter
from datetime import date

import httpx
import pytest

from conftest import FIX, biz_id
from dealfeed.classify import pg_regex
from dealfeed.ingest import states
from dealfeed.services import search
from dealfeed.services.search import SearchParams


def mapped(key, min_age=0):
    skipped = Counter()
    rows = {sid: c for sid, c, _ in states.map_file(states.SOURCES[key], FIX / f"{key}_sample.csv",
                                                    states.min_age_cutoff(min_age, date(2026, 11, 6)), skipped)}
    return rows, skipped


def test_iowa_filters_nonprofit_and_foreign():
    rows, skipped = mapped("ia_sos")
    assert len(rows) == 12
    assert skipped == {"not a domestic for-profit entity": 2}
    hawkeye = rows["100001"]
    assert hawkeye["entity_type"] == "Corporation" and hawkeye["formation_date"] == "1988-03-15"
    assert hawkeye["people"] == [{"name": "DALE FICTIONAL", "role": "registered agent"}]
    assert "999 HOME" not in str(hawkeye), "registered agent address never kept"


def test_min_age_filter():
    rows, skipped = mapped("ia_sos", min_age=5)
    assert "100004" not in rows, "River City (formed 2023) is under 5 years old"
    assert skipped["younger than minimum age"] == 1


def test_colorado_status_name_suffix_and_skips():
    rows, skipped = mapped("co_sos")
    assert set(rows) == {"19911000001", "20041000002"}
    assert skipped == {"not a domestic for-profit entity": 2, "dissolved or withdrawn": 1}
    fr = rows["20041000002"]
    assert fr["legal_name"] == "FRONT RANGE JANITORIAL LLC"
    assert fr["status"] == "delinquent" and fr["status_since"] == "2021-03-01"
    assert fr["people"] == [{"name": "REGISTERED AGENTS INC", "role": "registered agent"}]


def test_new_york_address_kind_and_people():
    rows, skipped = mapped("ny_dos")
    assert set(rows) == {"500001", "500002"} and sum(skipped.values()) == 2
    empire, buffalo = rows["500001"], rows["500002"]
    assert empire["address_kind"] == "service_of_process" and empire["county"] == "ALBANY"
    assert buffalo["address_kind"] == "location" and buffalo["address_line1"].startswith("77 ELMWOOD")
    assert {"name": "MORGAN FAKEPERSON", "role": "chief executive officer"} in empire["people"]
    assert "PRIVATE HOME" not in str(empire), "CEO address never kept"


def test_connecticut_naics_and_no_email_or_demographics():
    rows, skipped = mapped("ct_sots")
    assert set(rows) == {"0700001", "0700002"}
    assert skipped == {"not a for-profit entity type": 1, "not active": 1, "foreign registration": 1}
    assert rows["0700001"]["naics"] == "561710"
    assert "owner@example.com" not in str(rows) and "woman" not in str(rows).lower()


def test_oregon_groups_roles_and_attaches_dbas():
    rows, skipped = mapped("or_sos")
    assert set(rows) == {"800001", "800003"}
    cascade = rows["800001"]
    assert cascade["dba_names"] == ["CASCADE BUG BUSTERS"]
    assert cascade["address_line1"].startswith("400 SW MORRISON"), "principal place beats mailing address"
    assert cascade["people"] == [{"name": "JORDAN SAMPLETON", "role": "registered agent"}]
    assert skipped == {"assumed name owned by an individual": 1, "not a domestic for-profit entity": 1}


def test_texas_types_exempt_and_status():
    rows, skipped = mapped("tx_cpa")
    assert set(rows) == {"32000000001", "32000000002"}
    assert skipped == {"not a confirmed domestic for-profit type": 2, "no SOS registration": 1}
    assert rows["32000000002"]["status"] == "delinquent" and rows["32000000002"]["naics"] == "561720"


def test_zip_files_load(tmp_path):
    z = tmp_path / "ia.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.write(FIX / "ia_sos_sample.csv", "active_iowa_business_entities_554_rows.csv")
    assert states.read_header(z)[0] == "corp_number"
    assert sum(1 for _ in states.iter_rows(z)) == 14


def test_socrata_fetch_pages_and_filters(tmp_path):
    seen = []
    pages = ['"entityid","entityname"\n"1","A"\n"2","B"\n', '"entityid","entityname"\n"3","C"\n']

    def handler(req: httpx.Request):
        seen.append(dict(req.url.params))
        return httpx.Response(200, text=pages[len(seen) - 1])

    client = httpx.Client(transport=httpx.MockTransport(handler))
    path = states.fetch("co_sos", tmp_path, min_age_years=5, page_size=2, client=client)
    assert path.read_text().splitlines() == ['"entityid","entityname"', '"1","A"', '"2","B"', '"3","C"']
    assert seen[0]["$offset"] == "0" and seen[1]["$offset"] == "2"
    assert "entitystatus in" in seen[0]["$where"] and "entityformdate <" in seen[0]["$where"]


def test_socrata_fetch_retries_dropped_connections(tmp_path, monkeypatch):
    monkeypatch.setattr(states.time, "sleep", lambda s: None)
    calls = []

    def handler(req):
        calls.append(1)
        if len(calls) == 1:
            raise httpx.RemoteProtocolError("peer closed connection", request=req)
        if len(calls) == 2:
            return httpx.Response(503, text="busy")
        return httpx.Response(200, text='"entityid"\n"1"\n')

    client = httpx.Client(transport=httpx.MockTransport(handler))
    path = states.fetch("co_sos", tmp_path, page_size=10, client=client)
    assert len(calls) == 3 and path.read_text().splitlines() == ['"entityid"', '"1"']


def test_pg_regex_word_boundaries(conn):
    rx = pg_regex((r"\bPEST\b", r"EXTERMINAT"))
    hit = lambda s: conn.execute("SELECT %s ~ %s AS m", (s, rx)).fetchone()["m"]  # noqa: E731
    assert hit("ACME PEST CONTROL") and hit("OLD MILL EXTERMINATING")
    assert not hit("PESTOLOGY LABS")


def test_classification_from_state_filing_and_rules(conn):
    row = lambda name: conn.execute("SELECT naics_code, naics_source FROM businesses WHERE legal_name = %s",  # noqa: E731
                                    (name,)).fetchone()
    assert row("NUTMEG PEST CONTROL, LLC") == {"naics_code": "561710", "naics_source": "state"}
    assert row("ELM CITY CLEANING SERVICES, INC.") == {"naics_code": "561720", "naics_source": "rules"}
    assert row("ROCKY MOUNTAIN PEST PROS, INC.")["naics_source"] == "ppp"


def test_cross_state_search_and_market(conn):
    allp = search.search_targets(conn, SearchParams(industry="pest control", limit=50), limit=50)
    st = {t["state"] for t in allp["results"]}
    assert {"IA", "CO", "NY", "CT", "OR", "TX"} <= st
    co = search.search_targets(conn, SearchParams(industry="pest control", state="co"), limit=50)
    assert [t["name"] for t in co["results"]] == ["ROCKY MOUNTAIN PEST PROS, INC."]
    assert co["results"][0]["data_as_of"]["state_registry"]
    m = search.market_summary(conn, None, "pest control")
    assert set(m["by_state"]) >= {"IA", "CO", "TX"}
    none = search.search_targets(conn, SearchParams(state="WY"), limit=10)
    assert none["returned"] == 0 and any("Covered states" in n for n in none["notes"])


def test_delinquent_penalized_with_reason(conn):
    r = conn.execute("SELECT s.score, s.reasons FROM scores s JOIN businesses b ON b.id = s.business_id "
                     "WHERE b.legal_name = 'FRONT RANGE JANITORIAL LLC'").fetchone()
    assert any("Behind on Colorado filings since 2021" in x for x in r["reasons"])


def test_dba_names_on_business(conn):
    dbas = conn.execute("SELECT dba_names FROM businesses WHERE id = %s",
                        (biz_id(conn, "CASCADE PEST CONTROL%"),)).fetchone()["dba_names"]
    assert dbas == ["CASCADE BUG BUSTERS"]


@pytest.mark.parametrize("key", states.STATE_SOURCE_KEYS)
def test_every_fixture_matches_its_spec(key):
    src = states.SOURCES[key]
    assert states.header_matches(src, states.read_header(FIX / f"{key}_sample.csv"))
