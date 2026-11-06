"""Registry names are stored in ALL CAPS; the web app shows them the way people write them."""
import pytest

from dealfeed.web.format import display_name, script_json


@pytest.mark.parametrize("raw,shown", [
    ("THRIFTY PEST CONTROL, INC.", "Thrifty Pest Control, Inc."),
    ("SWANSON'S PEST MANAGEMENT, INC.", "Swanson's Pest Management, Inc."),
    ("O'BRIEN HVAC LLC", "O'Brien HVAC LLC"),
    ("A-Q ALLSTATE TERMITE AND PEST CONTROL, CO.", "A-Q Allstate Termite and Pest Control, Co."),
    ("J&J CLEANING OF THE MIDWEST", "J&J Cleaning of the Midwest"),
    ("MCDONALD AGENCY (IOWA) INC", "McDonald Agency (Iowa) Inc"),
    ("ST. JOHN SERVICES P.C.", "St. John Services P.C."),
    ("TLC JANITORIAL", "TLC Janitorial"),
    ("Hawkeye Pest Solutions LLC", "Hawkeye Pest Solutions LLC"),   # mixed case is left alone
    (None, ""),
])
def test_display_name(raw, shown):
    assert display_name(raw) == shown


def test_script_json_cannot_close_the_tag():
    out = script_json([{"name": "</script><img src=x onerror=alert(1)>"}])
    assert "<" not in out and ">" not in out
