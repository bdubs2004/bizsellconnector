"""Regenerates the fictional sample CSVs used by tests and `dealfeed demo`.

Every business, person and lender here is made up. Headers match the real
files: PPP and 7(a) follow the SBA FOIA data dictionaries; the state registry
files use the exact column names of each state's live dataset (checked Nov 2026,
Socrata /resource CSV exports use the API field names shown here).

    python tests/fixtures/make_fixtures.py
"""
import csv
from pathlib import Path

HERE = Path(__file__).parent

SOS_HEADER = ["corp_number", "legal_name", "corporation_type", "effective_date", "registered_agent",
              "ra_address_1", "ra_address_2", "ra_city", "ra_state", "ra_zip", "ra_latitude", "ra_longitude",
              "ra_location", "home_office", "ho_address_1", "ho_address_2", "ho_city", "ho_state", "ho_zip",
              "ho_country", "ho_latitude", "ho_longitude", "ho_location"]
IA_LLC, IA_CORP = "34 - DOMESTIC LIMITED LIABILITY COMPANY", "10 - DOMESTIC PROFIT"

#        corp,     name,                                    type,          eff date,     RA,                       addr,                       city,           zip
SOS = [
    ("100001", "Hawkeye Pest Solutions, Inc.",            IA_CORP, "03/15/1988", "Dale Fictional",        "1200 Ingersoll Avenue",     "Des Moines",   "50309"),
    ("100002", "Prairie Bug & Termite Control L.L.C.",    IA_LLC,  "06/01/1996", "Marla Example",         "415 Lincoln Way",           "Ames",         "50010"),
    ("100003", "Cedar Valley Janitorial Services Co",     IA_CORP, "09/22/1992", "Gene Placeholder",      "88 1st Avenue SE",          "Cedar Rapids", "52401"),
    ("100004", "River City Cleaning Services LLC",        IA_LLC,  "02/10/2023", "Pat Sample",            "2100 E River Drive",        "Davenport",    "52803"),
    ("100005", "Heartland Insurance Agency, Inc.",        IA_CORP, "11/30/1979", "CT Corporation System", "301 SW Oralabor Road",      "Ankeny",       "50023"),
    ("100006", "Northwoods Insurance Group LLC",          IA_LLC,  "04/04/2008", "Lee Testcase",          "19 N Federal Avenue",       "Mason City",   "50401"),
    ("100007", "Sunrise Pest Management LLC",             IA_LLC,  "07/19/2001", "Robin Notreal",         "730 S Gilbert Street",      "Iowa City",    "52240"),
    ("100008", "Quad City Lawn & Landscape Inc",          IA_CORP, "05/05/1999", "Chris Madeup",          "2400 Spruce Hills Drive",   "Bettendorf",   "52722"),
    ("100009", "Midwest Widget Holdings LLC",             IA_LLC,  "01/12/2015", "Northwest Registered Agent LLC", "500 Main Street",  "Dubuque",      "52001"),
    ("100010", "Golden Plains Auto Repair LLC",           IA_LLC,  "08/08/1990", "Sam Imaginary",         "1515 W 4th Street",         "Waterloo",     "50702"),
    ("100011", "Old Mill Exterminating Company",          IA_CORP, "10/10/1985", "Terry Fakename",        "220 Commercial Street",     "Waterloo",     "50701"),
    ("100012", "Townsquare Tax Service LLC",              IA_LLC,  "03/03/2012", "Jo Exampleton",         "12 Central Avenue",         "Fort Dodge",   "50501"),
    # skipped at ingest: nonprofit, and a foreign (out-of-state) registration
    ("100013", "First Example Church of Ames",            "22 - REVISED DOMESTIC NON-PROFIT", "1950-01-01", "Pastor Sample", "1 Church St", "Ames", "50010"),
    ("100014", "Megacorp Services LLC",                   "35 - FOREIGN LIMITED LIABILITY COMPANY", "2001-01-01", "CT Corporation System", "9 Big Tower", "Des Moines", "50309"),
]

PPP_HEADER = ["LoanNumber", "DateApproved", "SBAOfficeCode", "ProcessingMethod", "BorrowerName", "BorrowerAddress",
              "BorrowerCity", "BorrowerState", "BorrowerZip", "LoanStatusDate", "LoanStatus", "Term",
              "SBAGuarantyPercentage", "InitialApprovalAmount", "CurrentApprovalAmount", "UndisbursedAmount",
              "FranchiseName", "ServicingLenderLocationID", "ServicingLenderName", "ServicingLenderAddress",
              "ServicingLenderCity", "ServicingLenderState", "ServicingLenderZip", "RuralUrbanIndicator",
              "HubzoneIndicator", "LMIIndicator", "BusinessAgeDescription", "ProjectCity", "ProjectCountyName",
              "ProjectState", "ProjectZip", "CD", "JobsReported", "NAICSCode", "Race", "Ethnicity",
              "UTILITIES_PROCEED", "PAYROLL_PROCEED", "MORTGAGE_INTEREST_PROCEED", "RENT_PROCEED",
              "REFINANCE_EIDL_PROCEED", "HEALTH_CARE_PROCEED", "DEBT_INTEREST_PROCEED", "BusinessType",
              "OriginatingLenderLocationID", "OriginatingLender", "OriginatingLenderCity", "OriginatingLenderState",
              "Gender", "Veteran", "NonProfit", "ForgivenessAmount", "ForgivenessDate"]

#     loan#,        date,         method, name,                                   addr,                     city,           st,   zip,          amount,     county,        jobs, naics,   btype
PPP = [
    ("9000000001", "04/28/2020", "PPP", "HAWKEYE PEST SOLUTIONS INC",            "1200 INGERSOLL AVE",     "DES MOINES",   "IA", "50309-1234", "152000.00", "POLK",         14, "561710", "Corporation"),
    ("9000000002", "05/02/2020", "PPP", "PRAIRIE BUG AND TERMITE CONTROL LLC",   "415 LINCOLN WAY",        "AMES",         "IA", "50010",      "61000.00",  "STORY",         6, "561710", "Limited  Liability Company(LLC)"),
    ("9000000003", "04/15/2020", "PPP", "CEDAR VALLEY JANITORIAL SERVICES",      "88 1ST AVE SE",          "CEDAR RAPIDS", "IA", "52401",      "245000.00", "LINN",         31, "561720", "Corporation"),
    ("9000000004", "02/03/2021", "PPS", "CEDAR VALLEY JANITORIAL SERVICES",      "88 1ST AVE SE",          "CEDAR RAPIDS", "IA", "52401",      "240000.00", "LINN",         29, "561720", "Corporation"),
    ("9000000005", "04/30/2020", "PPP", "HEARTLAND INSURANCE AGENCY INC",        "301 SW ORALABOR RD",     "ANKENY",       "IA", "50023",      "98000.00",  "POLK",          9, "524210", "Corporation"),
    ("9000000006", "05/06/2020", "PPP", "SUNRISE PEST MGMT",                     "730 S GILBERT ST",       "IOWA CITY",    "IA", "52240",      "70000.00",  "JOHNSON",       7, "561710", "Limited  Liability Company(LLC)"),
    ("9000000007", "04/20/2020", "PPP", "CORNBELT CLEANING CREW INC",            "3900 MERLE HAY RD",      "DES MOINES",   "IA", "50310",      "180000.00", "POLK",         25, "561720", "Corporation"),
    ("9000000008", "04/22/2020", "PPP", "QUAD CITY LAWN & LANDSCAPE INC",        "2400 SPRUCE HILLS DR",   "BETTENDORF",   "IA", "52722",      "88000.00",  "SCOTT",         11, "561730", "Corporation"),
    ("9000000009", "05/01/2020", "PPP", "JOHN Q SAMPLE",                         "1 PRIVATE LN",           "AMES",         "IA", "50014",      "12000.00",  "STORY",          1, "561710", "Sole Proprietorship"),
    ("9000000010", "05/01/2020", "PPP", "CORNHUSKER PEST CO",                    "100 O ST",               "LINCOLN",      "NE", "68508",      "50000.00",  "LANCASTER",      5, "561710", "Corporation"),
    ("9000000011", "04/29/2020", "PPP", "GOLDEN PLAINS AUTO REPAIR LLC",         "1515 W 4TH ST",          "WATERLOO",     "IA", "50702",      "14000.00",  "BLACK HAWK",     2, "811111", "Limited  Liability Company(LLC)"),
    # other states: exact matches to the CO/TX registry fixtures, plus a nonprofit that must be skipped
    ("9000000012", "04/27/2020", "PPP", "ROCKY MOUNTAIN PEST PROS INC",          "1600 BROADWAY",          "DENVER",       "CO", "80202",      "230000.00", "DENVER",        22, "561710", "Corporation"),
    ("9000000013", "04/28/2020", "PPP", "LONE STAR PEST CONTROL LLC",            "500 CONGRESS AVE",       "AUSTIN",       "TX", "78701",      "105000.00", "TRAVIS",        10, "561710", "Limited  Liability Company(LLC)"),
    ("9000000014", "04/28/2020", "PPP", "FIRST EXAMPLE CHURCH OF AMES",          "1 CHURCH ST",            "AMES",         "IA", "50010",      "40000.00",  "STORY",          6, "813110", "Non-Profit Organization"),
    # gray-zone name (86) at a different address in a target industry -> manual review queue
    ("9000000015", "05/04/2020", "PPP", "OLD MILL EXTERMINATORS",                "900 SAMPLE AVE",         "WATERLOO",     "IA", "50701",      "52000.00",  "BLACK HAWK",     5, "561710", "Corporation"),
]

SBA_HEADER = ["AsOfDate", "Program", "BorrName", "BorrStreet", "BorrCity", "BorrState", "BorrZip", "BankName",
              "GrossApproval", "SBAGuaranteedApproval", "ApprovalDate", "ApprovalFiscalYear", "DeliveryMethod",
              "subpgmdesc", "InitialInterestRate", "TerminMonths", "NaicsCode", "NaicsDescription", "ProjectCounty",
              "ProjectState", "BusinessType", "BusinessAge", "LoanStatus", "JobsSupported"]

SBA = [
    ("20260930", "7A", "CEDAR VALLEY JANITORIAL SERVICES CO", "88 1st Ave SE", "Cedar Rapids", "IA", "52401",
     "Example Community Bank", "1250000", "937500", "03/14/2025", "2025", "PLP", "Guaranty", "10.75", "120",
     "561720", "Janitorial Services", "LINN", "IA", "CORPORATION", "Change of Ownership", "EXEMPT", "30"),
    ("20260930", "7A", "IOWA PEST PROS LLC", "9 Sample Rd", "Urbandale", "IA", "50322",
     "Fictional Savings Bank", "850000", "637500", "06/02/2023", "2023", "PLP", "Guaranty", "11.25", "120",
     "561710", "Exterminating and Pest Control Services", "POLK", "IA", "CORPORATION", "Change of Ownership",
     "EXEMPT", "9"),
    ("20260930", "7A", "PLAINS PEST LLC", "77 Test Ave", "Omaha", "NE", "68102",
     "Fictional Savings Bank", "500000", "375000", "06/02/2024", "2024", "PLP", "Guaranty", "11.00", "120",
     "561710", "Exterminating and Pest Control Services", "DOUGLAS", "NE", "CORPORATION", "Change of Ownership",
     "EXEMPT", "5"),
]


CO_HEADER = ["entityid", "entityname", "principaladdress1", "principaladdress2", "principalcity", "principalstate",
             "principalzipcode", "principalcountry", "mailingaddress1", "mailingaddress2", "mailingcity", "mailingstate",
             "mailingzipcode", "mailingcountry", "entitystatus", "jurisdictonofformation", "entitytype", "agentfirstname",
             "agentmiddlename", "agentlastname", "agentsuffix", "agentorganizationname", "agentprincipaladdress1",
             "agentprincipaladdress2", "agentprincipalcity", "agentprincipalstate", "agentprincipalzipcode",
             "agentprincipalcountry", "agentmailingaddress1", "agentmailingaddress2", "agentmailingcity",
             "agentmailingstate", "agentmailingzipcode", "agentmailingcountry", "entityformdate"]
CO = [  # id, name, status, type, formed, agent first, agent last, agent org, address, city, zip
    ("19911000001", "ROCKY MOUNTAIN PEST PROS, INC.", "Good Standing", "DPC", "1991-04-02T00:00:00.000", "Alex", "Placeholder", "", "1600 Broadway", "Denver", "80202"),
    ("20041000002", "FRONT RANGE JANITORIAL LLC, Delinquent March 1, 2021", "Delinquent", "DLLC", "2004-06-15T00:00:00.000", "", "", "Registered Agents Inc", "30 S Tejon St", "Colorado Springs", "80903"),
    ("19801000003", "MILE HIGH CHARITY FOUNDATION", "Good Standing", "DNC", "1980-01-01T00:00:00.000", "Kim", "Giver", "", "1 Gift Ln", "Denver", "80202"),
    ("20021000004", "ACME FOREIGN LLC", "Good Standing", "FLLC", "2002-01-01T00:00:00.000", "", "", "CT Corporation System", "2 Elsewhere", "Denver", "80202"),
    ("19901000005", "OLD DISSOLVED CO", "Voluntarily Dissolved", "DPC", "1990-01-01T00:00:00.000", "", "", "", "3 Gone St", "Denver", "80202"),
]

NY_HEADER = ["dos_id", "current_entity_name", "initial_dos_filing_date", "county", "jurisdiction", "entity_type",
             "dos_process_name", "dos_process_address_1", "dos_process_address_2", "dos_process_city",
             "dos_process_state", "dos_process_zip", "chairman_name", "chairman_address_1", "chairman_address_2",
             "chairman_city", "chairman_state", "chairman_zip", "registered_agent_name", "registered_agent_address_1",
             "registered_agent_address_2", "registered_agent_city", "registered_agent_state", "registered_agent_zip",
             "location_name", "location_address_1", "location_address_2", "location_city", "location_state",
             "location_zip"]
NY = [  # id, name, filed, county, type, process addr, city, zip, ceo, location addr, loc city, loc zip
    ("500001", "EMPIRE STATE INSURANCE AGENCY, INC.", "1988-05-10T00:00:00.000", "Albany", "DOMESTIC BUSINESS CORPORATION", "100 State St", "Albany", "12207", "Morgan Fakeperson", "", "", ""),
    ("500002", "BUFFALO EXTERMINATING CORP.", "1979-09-01T00:00:00.000", "Erie", "DOMESTIC BUSINESS CORPORATION", "c/o Law Office, 50 Court St", "Buffalo", "14202", "", "77 Elmwood Ave", "Buffalo", "14201"),
    ("500003", "ST. EXAMPLE CHURCH", "1900-01-01T00:00:00.000", "Erie", "DOMESTIC RELIGIOUS CORPORATION", "1 Church Rd", "Buffalo", "14202", "", "", "", ""),
    ("500004", "BIG FOREIGN CORP", "1995-01-01T00:00:00.000", "New York", "FOREIGN BUSINESS CORPORATION", "1 Wall St", "New York", "10005", "", "", "", ""),
]

CT_HEADER = ["id", "name", "business_type", "status", "sub_status", "accountnumber", "date_registration",
             "billingstreet", "billing_unit", "billingcity", "billingcountry", "billingpostalcode", "billingstate",
             "business_email_address", "citizenship", "formation_place", "woman_owned_organization",
             "veteran_owned_organization", "naics_code", "naics_sub_code", "create_dt"]
CT = [  # id, name, type, status, acct, registered, street, city, zip, citizenship, naics
    ("001A", "NUTMEG PEST CONTROL, LLC", "LLC", "Active", "0700001", "1999-03-01T00:00:00.000", "55 Farmington Ave", "Hartford", "06105", "Domestic", "Exterminating and Pest Control Services (561710)"),
    ("001B", "ELM CITY CLEANING SERVICES, INC.", "Stock", "Active", "0700002", "1995-07-07T00:00:00.000", "200 Orange St", "New Haven", "06510", "Domestic", ""),
    ("001C", "EXAMPLE ALUMNI ASSOCIATION", "Non-Stock", "Active", "0700003", "1960-01-01T00:00:00.000", "1 College St", "New Haven", "06510", "Domestic", ""),
    ("001D", "FORFEITED WIDGETS LLC", "LLC", "Forfeited", "0700004", "2000-01-01T00:00:00.000", "2 Gone Rd", "Hartford", "06105", "Domestic", ""),
    ("001E", "OUT OF STATE STOCK CO", "Stock", "Active", "0700005", "1998-01-01T00:00:00.000", "3 Far Away", "Hartford", "06105", "Foreign", ""),
]

OR_HEADER = ["registry_number", "business_name", "entity_type", "registry_date", "associated_name_type", "first_name",
             "middle_name", "last_name", "suffix", "not_of_record_entity", "entity_of_record_reg_number",
             "entity_of_record_name", "address", "address_continued", "city", "state", "zip", "jurisdiction",
             "business_details"]
OR = [  # reg, name, type, date, role, first, last, owner reg, address, city, zip
    ("800001", "CASCADE PEST CONTROL, INC.", "DOMESTIC BUSINESS CORPORATION", "1993-08-20T10:00:00.000", "PRINCIPAL PLACE OF BUSINESS", "", "", "", "400 SW Morrison St", "Portland", "97204"),
    ("800001", "CASCADE PEST CONTROL, INC.", "DOMESTIC BUSINESS CORPORATION", "1993-08-20T10:00:00.000", "REGISTERED AGENT", "Jordan", "Sampleton", "", "400 SW Morrison St", "Portland", "97204"),
    ("800001", "CASCADE PEST CONTROL, INC.", "DOMESTIC BUSINESS CORPORATION", "1993-08-20T10:00:00.000", "MAILING ADDRESS", "", "", "", "PO Box 1", "Portland", "97204"),
    ("800002", "CASCADE BUG BUSTERS", "ASSUMED BUSINESS NAME", "2005-01-01T00:00:00.000", "PRINCIPAL PLACE OF BUSINESS", "", "", "800001", "400 SW Morrison St", "Portland", "97204"),
    ("800003", "WILLAMETTE JANITORIAL LLC", "DOMESTIC LIMITED LIABILITY COMPANY", "2010-02-02T09:00:00.000", "PRINCIPAL PLACE OF BUSINESS", "", "", "", "1000 Willamette St", "Eugene", "97401"),
    ("800004", "JANE EXAMPLE PAINTING", "ASSUMED BUSINESS NAME", "2015-01-01T00:00:00.000", "AUTHORIZED REPRESENTATIVE", "Jane", "Example", "", "5 Home Ln", "Eugene", "97401"),
    ("800005", "OREGON FOOD HELPERS", "DOMESTIC NONPROFIT CORPORATION", "1985-01-01T00:00:00.000", "PRINCIPAL PLACE OF BUSINESS", "", "", "", "6 Help Way", "Portland", "97204"),
]

TX_HEADER = ["taxpayer_number", "taxpayer_name", "taxpayer_address", "taxpayer_city", "taxpayer_state", "taxpayer_zip",
             "taxpayer_county_code", "taxpayer_organizational_type", "record_type_code", "responsibility_beginning_date",
             "secretary_of_state_sos_or_coa_file_number", "sos_charter_date", "sos_status_date", "sos_status_code",
             "right_to_transact_business_code", "current_exempt_reason_code", "exempt_begin_date", "_621111"]
TX = [  # taxpayer, name, addr, city, zip, county code, org type, sos file, charter, right to transact, exempt, naics
    ("32000000001", "LONE STAR PEST CONTROL LLC", "500 Congress Ave", "Austin", "78701", "227", "CL", "0800000001", "1998-02-03T00:00:00.000", "A", "", "561710"),
    ("32000000002", "ALAMO JANITORIAL SERVICES INC", "100 Alamo Plz", "San Antonio", "78205", "15", "CT", "0800000002", "1985-11-11T00:00:00.000", "N", "", "561720"),
    ("32000000003", "TEXAS EXAMPLE CAMP", "1 Camp Rd", "Austin", "78701", "227", "CN", "0800000003", "1950-01-01T00:00:00.000", "A", "04", "721214"),
    ("32000000004", "FOREIGN CORP OF DELAWARE", "1 Tower", "Austin", "78701", "227", "CF", "0800000004", "1999-01-01T00:00:00.000", "A", "", ""),
    ("32000000005", "NO SOS RECORD LLC", "2 Nowhere", "Austin", "78701", "227", "CL", "", "", "A", "", ""),
]


def _write(name, header, rows):
    with open(HERE / name, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        for r in rows:
            row = dict.fromkeys(header, "")
            row.update(r)
            w.writerow([row[h] for h in header])


def write_states():
    _write("co_sos_sample.csv", CO_HEADER, [
        {"entityid": i, "entityname": n, "entitystatus": st, "entitytype": t, "entityformdate": d,
         "agentfirstname": af, "agentlastname": al, "agentorganizationname": ao, "agentprincipaladdress1": "77 Home St",
         "principaladdress1": a, "principalcity": c, "principalstate": "CO", "principalzipcode": z,
         "jurisdictonofformation": "CO"} for i, n, st, t, d, af, al, ao, a, c, z in CO])
    _write("ny_dos_sample.csv", NY_HEADER, [
        {"dos_id": i, "current_entity_name": n, "initial_dos_filing_date": d, "county": co, "jurisdiction": "New York",
         "entity_type": t, "dos_process_name": n, "dos_process_address_1": pa, "dos_process_city": pc,
         "dos_process_state": "NY", "dos_process_zip": pz, "chairman_name": ceo,
         "chairman_address_1": "88 Private Home Rd" if ceo else "", "location_address_1": la, "location_city": lc,
         "location_state": "NY" if la else "", "location_zip": lz}
        for i, n, d, co, t, pa, pc, pz, ceo, la, lc, lz in NY])
    _write("ct_sots_sample.csv", CT_HEADER, [
        {"id": i, "name": n, "business_type": t, "status": st, "accountnumber": a, "date_registration": d,
         "billingstreet": s, "billingcity": c, "billingstate": "CT", "billingpostalcode": z, "billingcountry": "United States",
         "citizenship": ci, "naics_code": nc, "business_email_address": "owner@example.com",
         "woman_owned_organization": "true", "veteran_owned_organization": "false"}
        for i, n, t, st, a, d, s, c, z, ci, nc in CT])
    _write("or_sos_sample.csv", OR_HEADER, [
        {"registry_number": r, "business_name": n, "entity_type": t, "registry_date": d, "associated_name_type": role,
         "first_name": fn, "last_name": ln, "entity_of_record_reg_number": owner,
         "entity_of_record_name": "CASCADE PEST CONTROL, INC." if owner else "", "address": a, "city": c,
         "state": "OR", "zip": z, "jurisdiction": "OR"} for r, n, t, d, role, fn, ln, owner, a, c, z in OR])
    _write("tx_cpa_sample.csv", TX_HEADER, [
        {"taxpayer_number": tn, "taxpayer_name": n, "taxpayer_address": a, "taxpayer_city": c, "taxpayer_state": "TX",
         "taxpayer_zip": z, "taxpayer_county_code": cc, "taxpayer_organizational_type": ot, "record_type_code": "U",
         "secretary_of_state_sos_or_coa_file_number": sf, "sos_charter_date": ch, "sos_status_code": "A" if sf else "",
         "right_to_transact_business_code": rt, "current_exempt_reason_code": ex, "_621111": nc}
        for tn, n, a, c, z, cc, ot, sf, ch, rt, ex, nc in TX])


def main():
    # RA address = a fake "home" address so tests can assert it never reaches the DB
    _write("ia_sos_sample.csv", SOS_HEADER, [
        {"corp_number": corp, "legal_name": name, "corporation_type": typ, "effective_date": eff,
         "registered_agent": ra, "ra_address_1": "999 Home Street", "ra_city": city, "ra_state": "IA", "ra_zip": zp,
         "ra_latitude": "41.1", "ra_longitude": "-93.1", "ho_address_1": addr, "ho_city": city, "ho_state": "IA",
         "ho_zip": zp, "ho_country": "USA"} for corp, name, typ, eff, ra, addr, city, zp in SOS])
    write_states()
    with open(HERE / "ppp_sample.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(PPP_HEADER)
        for (loan, d, method, name, addr, city, st, zp, amt, county, jobs, naics, btype) in PPP:
            row = dict.fromkeys(PPP_HEADER, "")
            row.update({
                "LoanNumber": loan, "DateApproved": d, "ProcessingMethod": method, "BorrowerName": name,
                "BorrowerAddress": addr, "BorrowerCity": city, "BorrowerState": st, "BorrowerZip": zp,
                "LoanStatus": "Paid in Full", "InitialApprovalAmount": amt, "CurrentApprovalAmount": amt,
                "ServicingLenderName": "Example Community Bank", "BusinessAgeDescription": "Existing or more than 2 years old",
                "ProjectCountyName": county, "ProjectState": st, "JobsReported": str(jobs), "NAICSCode": naics,
                "Race": "Unanswered", "Ethnicity": "Unknown/NotStated", "Gender": "Unanswered",
                "Veteran": "Unanswered", "BusinessType": btype, "OriginatingLender": "Example Community Bank",
                "ForgivenessAmount": amt,
            })
            w.writerow([row[h] for h in PPP_HEADER])
    with open(HERE / "sba_7a_sample.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(SBA_HEADER)
        w.writerows(SBA)


if __name__ == "__main__":
    main()
