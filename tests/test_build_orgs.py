"""
Tests for build_orgs.py. Every IRS file is replaced with a small made-up sample shaped like
the real one, so these tests never touch the internet.

Run from the repo's top folder:
    python3 -m unittest discover -s tests -v
"""

# Lets us write type hints like List[dict] in a way Python 3.8 accepts.
from __future__ import annotations

# contextlib and io capture what main() prints, so test output stays clean.
import contextlib
import io
# csv reads back the orgs.csv that main() writes.
import csv
# os and sys find the project folder.
import os
import shutil
import sys
# tempfile gives each test its own throwaway folder.
import tempfile
# unittest is Python's built-in test framework; mock swaps out the real downloads.
import unittest
from unittest import mock
# zipfile builds a sample ZIP of returns.
import zipfile

# The project folder is one level up from this tests folder.
PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Put it first on the import path so "import build_orgs" finds it.
sys.path.insert(0, PROJECT_DIR)

import build_orgs as bo  # noqa: E402  (import has to come after the sys.path line)


# ---------------------------------------------------------------------------
# SAMPLE DATA, shaped like the IRS files
# ---------------------------------------------------------------------------

# A BMF file: the same columns as the real eo_xx.csv files.
BMF_CSV = """EIN,NAME,ICO,STREET,CITY,STATE,ZIP,GROUP,SUBSECTION,AFFILIATION,CLASSIFICATION,RULING,DEDUCTIBILITY,FOUNDATION,ACTIVITY,ORGANIZATION,STATUS,TAX_PERIOD,ASSET_CD,INCOME_CD,FILING_REQ_CD,PF_FILING_REQ_CD,ACCT_PD,ASSET_AMT,INCOME_AMT,REVENUE_AMT,NTEE_CD,SORT_NAME
010000001,EARLY LEARNING ALLIANCE,,1 MAIN ST,SPRINGFIELD,TX,12301-1234,0000,03,3,1000,199001,1,15,000000000,1,01,202506,5,5,01,0,06,3000000,6000000,6000000,B21,
010000002,FAMILY STABILITY CENTER,,2 MAIN ST,DALLAS,TX,75201,0000,03,3,1000,199001,1,15,000000000,1,01,202506,5,5,01,0,06,900000,700000,700000,P40,
010000003,TINY PANTRY,,3 MAIN ST,WACO,TX,76701,0000,03,3,1000,199001,1,15,000000000,1,01,202506,1,1,01,0,06,10000,90000,90000,K31,
010000004,STATE UNIVERSITY,,4 MAIN ST,SPRINGFIELD,TX,12312,0000,03,3,1000,199001,1,15,000000000,1,01,202506,9,9,01,0,06,9000000000,900000000,900000000,B43,
010000005,EXCLUDED EXAMPLE ORG,,5 MAIN ST,SPRINGFIELD,TX,12352,0000,03,3,1000,199001,1,15,000000000,1,01,202506,7,7,01,0,06,40000000,30000000,30000000,T70,
010000006,BIG FAMILY FOUNDATION,,6 MAIN ST,HOUSTON,TX,77002,0000,03,3,1000,199001,1,04,000000000,1,01,202512,8,7,01,1,12,80000000,5000000,5000000,T20,
010000007,SOCIAL CLUB,,7 MAIN ST,SPRINGFIELD,TX,12301,0000,07,3,1000,199001,2,00,000000000,1,01,202506,5,5,01,0,06,3000000,3000000,3000000,N50,
010000008,OLD SHELTER,,8 MAIN ST,SPRINGFIELD,TX,12301,0000,03,3,1000,199001,1,15,000000000,1,01,201806,5,5,01,0,06,3000000,3000000,3000000,L41,
010000009,MYSTERY NONPROFIT,,9 MAIN ST,EL PASO,TX,79901,0000,03,3,1000,199001,1,16,000000000,1,01,202506,6,6,01,0,06,25000000,25000000,25000000,,
"""

# An e-file index, in the column style the IRS uses (with a batch column, as newer indexes have).
INDEX_CSV = """RETURN_ID,FILING_TYPE,EIN,TAX_PERIOD,SUB_DATE,TAXPAYER_NAME,RETURN_TYPE,DLN,OBJECT_ID,XML_BATCH_ID
1,EFILE,010000001,202406,2025-05-01,EARLY LEARNING ALLIANCE,990,1,202511111111111111,2025_TEOS_XML_01A
2,EFILE,010000001,202506,2026-05-01,EARLY LEARNING ALLIANCE,990,2,202622222222222222,2025_TEOS_XML_01A
3,EFILE,10000002,202506,2026-05-01,FAMILY STABILITY CENTER,990,3,202633333333333333,2025_TEOS_XML_01A
4,EFILE,010000002,202506,2026-05-01,FAMILY STABILITY CENTER,990T,4,202644444444444444,2025_TEOS_XML_01A
5,EFILE,010000006,202512,2026-05-01,BIG FAMILY FOUNDATION,990PF,5,202655555555555555,2025_TEOS_XML_01A
"""

# A full 990 return, trimmed to the tags we read, with the IRS namespace like the real files.
RETURN_990 = b"""<?xml version="1.0" encoding="utf-8"?>
<Return xmlns="http://www.irs.gov/efile" returnVersion="2024v5.0">
  <ReturnHeader><ReturnTypeCd>990</ReturnTypeCd><TaxYr>2025</TaxYr></ReturnHeader>
  <ReturnData><IRS990>
    <WebsiteAddressTxt>WWW.EARLYLEARNING.ORG</WebsiteAddressTxt>
    <ActivityOrMissionDesc>Kindergarten readiness for every child.</ActivityOrMissionDesc>
    <TotalEmployeeCnt>85</TotalEmployeeCnt>
    <MissionDesc>To make sure   every child arrives at kindergarten ready to learn.</MissionDesc>
  </IRS990></ReturnData>
</Return>"""

# A small organization's return that lists enough staff to pass the final size floor.
RETURN_SMALL = b"""<?xml version="1.0" encoding="utf-8"?>
<Return xmlns="http://www.irs.gov/efile"><ReturnHeader><ReturnTypeCd>990</ReturnTypeCd><TaxYr>2025</TaxYr></ReturnHeader>
<ReturnData><IRS990><WebsiteAddressTxt>N/A</WebsiteAddressTxt><TotalEmployeeCnt>14</TotalEmployeeCnt></IRS990></ReturnData></Return>"""

# A private foundation's 990-PF.
RETURN_PF = b"""<?xml version="1.0" encoding="utf-8"?>
<Return xmlns="http://www.irs.gov/efile"><ReturnHeader><ReturnTypeCd>990PF</ReturnTypeCd><TaxYr>2025</TaxYr></ReturnHeader>
<ReturnData><IRS990PF><WebsiteAddressTxt>http//bigfamily.org/</WebsiteAddressTxt></IRS990PF></ReturnData></Return>"""


class HelperTests(unittest.TestCase):
    """The small helper functions."""

    def test_clean_website(self):
        # Capitals and no scheme -> a standard https address.
        self.assertEqual(bo.clean_website("WWW.EXAMPLE.ORG"), "https://www.example.org")
        # A broken scheme and trailing slash are fixed.
        self.assertEqual(bo.clean_website("http//bigfamily.org/"), "https://bigfamily.org")
        # "No website" answers and email addresses become blank.
        for junk in ["N/A", "none", "0", "", None, "info@x.org", "nowebsite"]:
            self.assertEqual(bo.clean_website(junk), "", junk)

    def test_pad_ein(self):
        # A dropped leading zero is restored.
        self.assertEqual(bo.pad_ein("10000002"), "010000002")
        # Dashes are removed.
        self.assertEqual(bo.pad_ein("74-1618608"), "741618608")

    def test_ntee_ok(self):
        # Our issue areas are kept; universities, hospitals and other fields are not.
        self.assertTrue(bo.ntee_ok("P40"))
        self.assertTrue(bo.ntee_ok("t70"))
        self.assertFalse(bo.ntee_ok("B43"))
        self.assertFalse(bo.ntee_ok("E22"))
        self.assertFalse(bo.ntee_ok("A51"))

    def test_object_id_from_member(self):
        # Works with or without a folder in the ZIP's file name.
        self.assertEqual(bo.object_id_from_member("2025_TEOS_XML_01A/202511111111111111_public.xml"),
                         "202511111111111111")
        self.assertEqual(bo.object_id_from_member("202511111111111111_public.xml"), "202511111111111111")
        # A folder entry has no object ID.
        self.assertEqual(bo.object_id_from_member("2025_TEOS_XML_01A/"), "")

    def test_size_tier(self):
        self.assertEqual(bo.size_tier("public_charity", 25_000_000), "A")
        self.assertEqual(bo.size_tier("public_charity", 6_000_000), "B")
        self.assertEqual(bo.size_tier("public_charity", 1_500_000), "C")
        self.assertEqual(bo.size_tier("private_foundation", 1), "funder")

    def test_parse_return_xml(self):
        fields = bo.parse_return_xml(RETURN_990)
        self.assertEqual(fields["form"], "990")
        self.assertEqual(fields["tax_year"], "2025")
        self.assertEqual(fields["website"], "https://www.earlylearning.org")
        self.assertEqual(fields["employees"], "85")
        # The mission line wins over the one-line summary, with extra spaces collapsed.
        self.assertEqual(fields["mission"], "To make sure every child arrives at kindergarten ready to learn.")


class FullRunTest(unittest.TestCase):
    """Runs main() end to end with every download replaced by a local sample file."""

    def setUp(self):
        # A throwaway folder for the sample files and outputs.
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        # Build a sample ZIP holding the three returns, inside a folder as the IRS ZIPs are.
        self.zip_path = os.path.join(self.tmp, "sample.zip")
        with zipfile.ZipFile(self.zip_path, "w") as zf:
            zf.writestr("2025_TEOS_XML_01A/202622222222222222_public.xml", RETURN_990)
            zf.writestr("2025_TEOS_XML_01A/202633333333333333_public.xml", RETURN_SMALL)
            zf.writestr("2025_TEOS_XML_01A/202655555555555555_public.xml", RETURN_PF)
            # A return we didn't ask for, which must be ignored.
            zf.writestr("2025_TEOS_XML_01A/209999999999999999_public.xml", RETURN_990)

    def fake_download(self, url, dest, tries=3):
        """Stand-in for build_orgs.download: writes the matching sample to `dest`."""
        if url.endswith("eo_tx.csv"):
            with open(dest, "w") as f:
                f.write(BMF_CSV)
        elif "index_2026" in url:
            with open(dest, "w") as f:
                f.write(INDEX_CSV)
        elif "index_" in url:
            # Pretend last year's index is missing, to check the script carries on.
            raise bo.urllib.error.URLError("not found")
        elif url.endswith(".zip"):
            shutil.copy(self.zip_path, dest)
        else:
            raise AssertionError("unexpected download: " + url)

    def run_main(self, *extra):
        out = os.path.join(self.tmp, "orgs.csv")
        cache = os.path.join(self.tmp, "cache.csv.gz")
        zip_url = bo.EFILE_BASE.format(year=2026) + "2025_TEOS_XML_01A.zip"
        # Swap in the fake download, a fixed ZIP list and the exclusion list; capture the printout.
        with mock.patch.object(bo, "download", self.fake_download), \
                mock.patch.object(bo, "EXCLUDE_NAME_PATTERNS", ["EXCLUDED EXAMPLE ORG"]), \
                mock.patch.object(bo, "list_zip_urls", lambda year: [zip_url] if year == 2026 else []), \
                contextlib.redirect_stdout(io.StringIO()) as printed, \
                contextlib.redirect_stderr(io.StringIO()):
            bo.main(["--states", "TX", "--years", "2026,2025", "--out", out, "--cache", cache, *extra])
        with open(out, newline="") as f:
            rows = {r["ein"]: r for r in csv.DictReader(f)}
        return rows, printed.getvalue()

    def test_full_run(self):
        rows, printed = self.run_main()
        # Kept: the $6M early-learning group, the small center with 14 staff, the big foundation,
        # and the organization with no NTEE code.
        self.assertEqual(set(rows), {"010000001", "010000002", "010000006", "010000009"})
        # Website, staff count and the latest return (2025, not 2024) for the first organization.
        first = rows["010000001"]
        self.assertEqual(first["website"], "https://www.earlylearning.org")
        self.assertEqual(first["employees"], "85")
        self.assertEqual(first["return_tax_year"], "2025")
        self.assertEqual(first["size_tier"], "B")
        self.assertEqual(first["zip"], "12301")
        # The small center passed the floor on staff count, and its "N/A" website is blank.
        self.assertEqual(rows["010000002"]["employees"], "14")
        self.assertEqual(rows["010000002"]["website"], "")
        # The foundation is a funder, with its broken website fixed.
        self.assertEqual(rows["010000006"]["size_tier"], "funder")
        self.assertEqual(rows["010000006"]["website"], "https://bigfamily.org")
        # The organization with no NTEE code and no return is kept, with notes saying so.
        self.assertIn("no NTEE code", rows["010000009"]["notes"])
        self.assertIn("no recent e-filed return", rows["010000009"]["notes"])
        self.assertIn("Organizations kept: 4", printed)

    def test_second_run_uses_cache(self):
        # The first run reads the ZIP; the second finds everything in the cache and downloads none.
        self.run_main()
        rows, printed = self.run_main()
        self.assertIn("Return ZIPs downloaded this run: 0", printed)
        self.assertEqual(rows["010000001"]["website"], "https://www.earlylearning.org")

    def test_unsupported_compression_falls_back_to_unzip(self):
        # Make Python's ZIP reader fail the way it does on Deflate64 files, for every return.
        def refuse(*args, **kwargs):
            raise NotImplementedError("That compression method is not supported")
        with mock.patch.object(bo.zipfile.ZipFile, "read", refuse):
            rows, printed = self.run_main()
        # The system's unzip program read them instead, so the results are the same.
        self.assertIn("extracting with unzip", printed)
        self.assertEqual(rows["010000001"]["website"], "https://www.earlylearning.org")
        self.assertEqual(rows["010000006"]["website"], "https://bigfamily.org")

    def test_skip_990(self):
        rows, _ = self.run_main("--skip-990")
        # Without returns, the small center can't show its staff count, so it drops below the floor.
        self.assertNotIn("010000002", rows)
        self.assertEqual(rows["010000001"]["website"], "")


if __name__ == "__main__":
    unittest.main()
