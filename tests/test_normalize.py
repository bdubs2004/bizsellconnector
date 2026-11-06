from datetime import date

from dealfeed import normalize as nz
from dealfeed.industries import classify_by_name, resolve_industry


def test_entity_suffixes_normalize():
    assert nz.normalize_name("Smith Pest Control, L.L.C.") == "SMITH PEST CONTROL LLC"
    assert nz.normalize_name("Smith Pest Control Limited Liability Company") == "SMITH PEST CONTROL LLC"
    assert nz.normalize_name("Acme Corporation") == "ACME CORP"


def test_core_name_lines_up_sos_and_sba_spellings():
    assert nz.core_name("Smith Pest Control LLC") == nz.core_name("SMITH PEST CONTROL")
    assert nz.core_name("Prairie Bug & Termite Control L.L.C.") == nz.core_name("PRAIRIE BUG AND TERMITE CONTROL LLC")
    assert nz.core_name("The Old Mill Co Inc") == "OLD MILL"


def test_zip_and_dates():
    assert nz.zip5("50309-1234") == "50309"
    assert nz.zip5(None) is None
    assert nz.parse_date("03/15/1988") == date(1988, 3, 15)
    assert nz.parse_date("1988-03-15T00:00:00.000") == date(1988, 3, 15)
    assert nz.parse_date("garbage") is None


def test_address_normalization():
    assert nz.normalize_address("1200 Ingersoll Avenue") == nz.normalize_address("1200 INGERSOLL AVE")


def test_registered_agent_service_detection():
    assert nz.looks_like_ra_service("CT Corporation System")
    assert nz.looks_like_ra_service("Northwest Registered Agent LLC")
    assert not nz.looks_like_ra_service("Dale Fictional")


def test_industry_rules():
    ind, conf = classify_by_name("OLD MILL EXTERMINATING CO")
    assert ind.slug == "pest_control" and 0 < conf < 0.8
    assert classify_by_name("MAIN STREET DRY CLEANERS") is None   # excluded from janitorial
    assert classify_by_name("MIDWEST WIDGET HOLDINGS LLC") is None
    assert resolve_industry("Pest control companies").slug == "pest_control"
    assert resolve_industry("561720").slug == "commercial_cleaning"
