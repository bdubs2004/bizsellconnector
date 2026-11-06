"""Regenerates the fictional sample CSVs used by tests and `dealfeed demo`.

Every business, person and lender here is made up. Headers mimic the real
files (PPP and 7(a) headers follow the SBA FOIA data dictionaries; the Iowa
SOS headers are a best guess and the ingest accepts several spellings).

    python tests/fixtures/make_fixtures.py
"""
import csv
from pathlib import Path

HERE = Path(__file__).parent

SOS_HEADER = ["Corp Number", "Legal Name", "Corporation Type", "Modifier", "Effective Date", "Registered Agent",
              "RA Address 1", "RA Address 2", "RA City", "RA State", "RA Zip",
              "Home Office Address 1", "Home Office Address 2", "Home Office City", "Home Office State",
              "Home Office Zip"]

#        corp,     name,                                    type,          eff date,     RA,                       addr,                       city,           zip
SOS = [
    ("100001", "Hawkeye Pest Solutions, Inc.",            "Domestic Profit", "03/15/1988", "Dale Fictional",        "1200 Ingersoll Avenue",     "Des Moines",   "50309"),
    ("100002", "Prairie Bug & Termite Control L.L.C.",    "Domestic LLC",    "06/01/1996", "Marla Example",         "415 Lincoln Way",           "Ames",         "50010"),
    ("100003", "Cedar Valley Janitorial Services Co",     "Domestic Profit", "09/22/1992", "Gene Placeholder",      "88 1st Avenue SE",          "Cedar Rapids", "52401"),
    ("100004", "River City Cleaning Services LLC",        "Domestic LLC",    "02/10/2023", "Pat Sample",            "2100 E River Drive",        "Davenport",    "52803"),
    ("100005", "Heartland Insurance Agency, Inc.",        "Domestic Profit", "11/30/1979", "CT Corporation System", "301 SW Oralabor Road",      "Ankeny",       "50023"),
    ("100006", "Northwoods Insurance Group LLC",          "Domestic LLC",    "04/04/2008", "Lee Testcase",          "19 N Federal Avenue",       "Mason City",   "50401"),
    ("100007", "Sunrise Pest Management LLC",             "Domestic LLC",    "07/19/2001", "Robin Notreal",         "730 S Gilbert Street",      "Iowa City",    "52240"),
    ("100008", "Quad City Lawn & Landscape Inc",          "Domestic Profit", "05/05/1999", "Chris Madeup",          "2400 Spruce Hills Drive",   "Bettendorf",   "52722"),
    ("100009", "Midwest Widget Holdings LLC",             "Domestic LLC",    "01/12/2015", "Northwest Registered Agent LLC", "500 Main Street",  "Dubuque",      "52001"),
    ("100010", "Golden Plains Auto Repair LLC",           "Domestic LLC",    "08/08/1990", "Sam Imaginary",         "1515 W 4th Street",         "Waterloo",     "50702"),
    ("100011", "Old Mill Exterminating Company",          "Domestic Profit", "10/10/1985", "Terry Fakename",        "220 Commercial Street",     "Waterloo",     "50701"),
    ("100012", "Townsquare Tax Service LLC",              "Domestic LLC",    "03/03/2012", "Jo Exampleton",         "12 Central Avenue",         "Fort Dodge",   "50501"),
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


def main():
    with open(HERE / "ia_sos_sample.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(SOS_HEADER)
        for corp, name, typ, eff, ra, addr, city, zp in SOS:
            # RA address = a fake "home" address so tests can assert it never reaches the DB
            w.writerow([corp, name, typ, "Domestic", eff, ra, "999 Home Street", "", city, "IA", zp,
                        addr, "", city, "IA", zp])
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
