"""
Tests for reverse_boards.py. Common Crawl and every board page are replaced with small made-up
samples, so these tests never touch the internet.

Run from the repo's top folder:
    python3 -m unittest discover -s tests -v
"""

# contextlib and io capture what main() prints, so test output stays clean.
import contextlib
import csv
import io
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

# Make the project's scripts importable from the tests folder.
PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_DIR)

import reverse_boards as rb  # noqa: E402

GUID = "e43cbf69-d17f-4270-ab1d-ba0ec7b127d6"

ORGS = [
    {"ein": "000000001", "name": "ARC OF EXAMPLE STATE INC", "state": "TX", "size_tier": "B"},
    {"ein": "000000002", "name": "GOODWILL SOCAL", "state": "CA", "size_tier": "A"},
    {"ein": "000000003", "name": "HOPE HOUSE INC", "state": "MO", "size_tier": "C"},
    {"ein": "000000004", "name": "HOPE HOUSE", "state": "WI", "size_tier": "C"},
    {"ein": "000000005", "name": "RIVER VALLEY FAMILY SERVICES", "state": "OH", "size_tier": "B"},
]


class IndexTests(unittest.TestCase):

    def test_blocks_for_prefixes(self):
        # A small table of contents: each line is the first address of a block.
        lines = [
            "com,acme)/ 2026\tcdx-1.gz\t0\t10\t1",
            "com,bamboohr,aaa)/careers 2026\tcdx-1.gz\t10\t10\t2",
            "com,bamboohr,mmm)/careers 2026\tcdx-1.gz\t20\t10\t3",
            "com,bambu)/ 2026\tcdx-1.gz\t30\t10\t4",
            "com,zzz)/ 2026\tcdx-1.gz\t40\t10\t5",
        ]
        blocks = rb.blocks_for_prefixes(lines, {"bamboohr": ["com,bamboohr,"]})
        # The three blocks that can hold com,bamboohr,... addresses, and not the ones after.
        self.assertEqual(blocks["bamboohr"], [("cdx-1.gz", 0, 10), ("cdx-1.gz", 10, 10), ("cdx-1.gz", 20, 10)])


class NameTests(unittest.TestCase):

    def test_names_on_page(self):
        page = ('<title>Careers | River Valley Family Services</title>'
                '<script>{"hiringOrganization": {"@type": "Organization", "name": "River Valley Family Services"}}'
                '</script><h1>Open Positions</h1>')
        # The search-engine markup comes first; duplicates are dropped.
        self.assertEqual(rb.names_on_page(page),
                         ["River Valley Family Services", "Careers | River Valley Family Services", "Open Positions"])

    def test_matcher(self):
        m = rb.OrgMatcher(ORGS)
        # A title with the name last still matches exactly, through its pieces.
        org, _, how, _ = m.match(["Job Board - Arc of Example State, Inc."])
        self.assertEqual((org["ein"], how), ("000000001", "exact"))
        # No usable name, but the board's ID matches an organization's name without spaces.
        org, _, how, _ = m.match(["Careers"], board_id="goodwill-socal")
        self.assertEqual((org["ein"], how), ("000000002", "board id"))
        # Two organizations share the name: not matched.
        org, _, how, _ = m.match(["Hope House - Job Opportunities"])
        self.assertIsNone(org)
        self.assertTrue(how.startswith("ambiguous"))
        # The hiring system's own name is ignored.
        org, _, _, _ = m.match(["Job Not Found"])
        self.assertIsNone(org)

    def test_combine(self):
        crawl = [
            {"ein": "000000001", "ats_platform": "paylocity", "board_id": GUID,
             "board_url": f"https://recruiting.paylocity.com/Recruiting/Jobs/All/{GUID}", "careers_url": "x"},
            {"ein": "000000005", "ats_platform": "site_page", "board_id": "", "board_url": "",
             "careers_url": "https://rvfs.org/careers"},
            {"ein": "000000003", "ats_platform": "not_found", "board_id": "", "board_url": "", "careers_url": ""},
        ]
        reverse = [
            # The same Paylocity board, found both ways.
            {"matched_ein": "000000001", "ats_platform": "paylocity", "board_id": GUID,
             "board_url": f"https://recruiting.paylocity.com/Recruiting/Jobs/All/{GUID}"},
            # A board only the reverse search found.
            {"matched_ein": "000000002", "ats_platform": "bamboohr", "board_id": "goodwillsocal",
             "board_url": "https://goodwillsocal.bamboohr.com/careers"},
            # An unmatched board is left out.
            {"matched_ein": "", "ats_platform": "bamboohr", "board_id": "acme", "board_url": "https://acme.bamboohr.com/careers"},
        ]
        rows = {r["ein"]: r for r in rb.combine(ORGS, crawl, reverse)}
        self.assertEqual(set(rows), {"000000001", "000000002", "000000005"})
        self.assertEqual(rows["000000001"]["source"], "both")
        self.assertEqual(rows["000000002"]["source"], "reverse search")
        self.assertEqual(rows["000000005"]["source"], "website crawl")


class FullRunTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.paths = {k: os.path.join(self.tmp, k + ".csv") for k in ("orgs", "boards", "reverse", "combined")}
        with open(self.paths["orgs"], "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["ein", "name", "state", "size_tier"])
            w.writeheader()
            w.writerows(ORGS)
        # The website crawl found nothing yet.
        with open(self.paths["boards"], "w") as f:
            f.write("ein,ats_platform,board_id,board_url,careers_url\n")

    def run_main(self):
        # Fake Common Crawl: one crawl, one block per system, holding these addresses.
        addresses = {
            "bamboohr": ["https://rivervalley.bamboohr.com/careers", "https://rivervalley.bamboohr.com/careers/12",
                         "https://acme.bamboohr.com/careers"],
            "paylocity": [f"https://recruiting.paylocity.com/Recruiting/Jobs/All/{GUID}/Arc"],
        }
        pages = {
            "https://rivervalley.bamboohr.com/careers": "<title>River Valley Family Services - Careers</title>",
            "https://acme.bamboohr.com/careers": "<title>Acme Plumbing Supply</title>",
            f"https://recruiting.paylocity.com/Recruiting/Jobs/All/{GUID}": "<title>Arc of Example State Inc - Job Opportunities</title>",
        }
        fake_blocks = lambda crawl, prefixes: {s: [(s, 0, 1)] if s in addresses else [] for s in prefixes}
        with mock.patch.object(rb, "newest_crawls", lambda n: ["CC-TEST"]), \
                mock.patch.object(rb, "find_blocks", fake_blocks), \
                mock.patch.object(rb, "read_block", lambda crawl, block: addresses[block[0]]), \
                mock.patch.object(rb.fb, "fetch_with_fallback", lambda url: (url, pages[url])), \
                mock.patch.dict(rb.SYSTEMS["paylocity"], pause=0), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            rb.main(["--orgs", self.paths["orgs"], "--boards", self.paths["boards"], "--out", self.paths["reverse"],
                     "--combined", self.paths["combined"], "--systems", "bamboohr,paylocity"])
        with open(self.paths["combined"], newline="") as f:
            return {r["ein"]: r for r in csv.DictReader(f)}, out.getvalue()

    def test_full_run(self):
        combined, printed = self.run_main()
        # Two boards matched our list; Acme Plumbing didn't.
        self.assertEqual(set(combined), {"000000001", "000000005"})
        self.assertEqual(combined["000000005"]["board_url"], "https://rivervalley.bamboohr.com/careers")
        self.assertIn("Boards in Common Crawl: 3", printed)

    def test_rerun_skips_fresh_boards(self):
        self.run_main()
        _, printed = self.run_main()
        self.assertIn("to visit now: 0", printed)


if __name__ == "__main__":
    unittest.main()
