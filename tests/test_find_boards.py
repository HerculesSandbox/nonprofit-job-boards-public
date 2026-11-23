"""
Tests for find_boards.py. Every website is replaced with a small made-up page, so these tests
never touch the internet.

Run from the repo's top folder:
    python3 -m unittest discover -s tests -v
"""

# Lets us write type hints in a way Python 3.8 accepts.
from __future__ import annotations

# contextlib and io capture what main() prints, so test output stays clean.
import contextlib
import io
# csv writes the sample orgs.csv and reads back boards.csv.
import csv
import os
import shutil
import sys
import tempfile
import unittest
import urllib.error
from unittest import mock

# Make "import find_boards" work from the tests folder.
PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_DIR)

import find_boards as fb  # noqa: E402


# ---------------------------------------------------------------------------
# SAMPLE WEBSITES: address -> page HTML. Anything not listed is "not found".
# ---------------------------------------------------------------------------

PAYLOCITY_GUID = "e43cbf69-d17f-4270-ab1d-ba0ec7b127d6"

SITES = {
    # 1. Homepage -> "Careers" link -> careers page linking to Paylocity.
    "https://alpha.org": '<a href="/about">About</a> <a href="/volunteer">Volunteer opportunities</a>'
                         ' <a href="/about/careers/">Careers</a>',
    "https://alpha.org/about/careers/": '<p>Join us.</p><a href="https://recruiting.paylocity.com/'
                                        f'recruiting/jobs/All/{PAYLOCITY_GUID}/Alpha">View Open Positions</a>',
    # 2. A Greenhouse embed script right on the homepage.
    "https://beta.org": '<script src="https://boards.greenhouse.io/embed/job_board/js?for=betaorg"></script>',
    # 3. No careers link on the homepage, but /careers exists and embeds a Workday board.
    "https://gamma.org": "<p>Welcome</p>",
    "https://gamma.org/careers": '<h1>Careers</h1><iframe src="https://gamma.wd1.myworkdayjobs.com/en-US/'
                                 'GammaCareers"></iframe>',
    # 4. Careers page with jobs listed on the site itself.
    "https://delta.org": '<a href="/jobs">Jobs</a>',
    "https://delta.org/jobs": "<h2>Program Director</h2><p>Email your resume.</p>",
    # 5. Careers page -> "Current openings" link -> a page that embeds BambooHR.
    "https://epsilon.org": '<a href="/work-with-us">Work with us</a>',
    "https://epsilon.org/work-with-us": '<a href="/openings">See current openings</a>',
    "https://epsilon.org/openings": '<script src="https://epsilon.bamboohr.com/js/embed.js"></script>'
                                    '<a href="https://epsilon.bamboohr.com/careers">Jobs</a>',
    # 6. A careers link that goes straight to UKG, no fetch needed.
    "https://zeta.org": '<a href="https://recruiting.ultipro.com/ZET1000ZETA/JobBoard/'
                        '11111111-2222-3333-4444-555555555555">Employment</a>',
    # 7. A site with no careers page anywhere.
    "https://eta.org": "<p>Donate today</p>",
}


def fake_fetch(url):
    """Stand-in for find_boards.fetch_page: serves SITES, or fails like a missing page."""
    # Ignore a trailing slash difference only for homepages.
    key = url if url in SITES else url.rstrip("/")
    if key not in SITES:
        raise urllib.error.URLError("404")
    return url, SITES[key]


class DetectTests(unittest.TestCase):
    """Recognizing each ATS from its addresses."""

    def check(self, text, platform, board_id):
        found = fb.detect_ats(text)
        self.assertIsNotNone(found, text)
        self.assertEqual(found["ats_platform"], platform)
        self.assertEqual(found["board_id"], board_id)

    def test_patterns(self):
        self.check(f"https://recruiting.paylocity.com/Recruiting/Jobs/All/{PAYLOCITY_GUID}/x", "paylocity", PAYLOCITY_GUID)
        self.check("https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html?cid="
                   "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee&ccId=1", "adp", "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
        self.check("https://www.paycomonline.net/v4/ats/web.php/jobs?clientkey=ABC123", "paycom", "ABC123")
        self.check("https://acme.wd5.myworkdayjobs.com/en-US/External", "workday", "acme/wd5/External")
        self.check("https://jobs.lever.co/acme-org/123", "lever", "acme-org")
        self.check("https://acme.applytojob.com/apply", "jazzhr", "acme")
        self.check("https://careers-acme.icims.com/jobs", "icims", "careers-acme")

    def test_ats_own_pages_are_not_boards(self):
        # www.icims.com is iCIMS's own site, not a customer's board.
        self.assertIsNone(fb.detect_ats("https://www.icims.com/about"))

    def test_platform_only_match(self):
        # A single Paylocity job page tells us the platform but not the board.
        found = fb.detect_ats("https://recruiting.paylocity.com/Recruiting/Jobs/Details/4478135")
        self.assertEqual(found, {"ats_platform": "paylocity", "board_id": "", "board_url": ""})


class FullRunTest(unittest.TestCase):
    """Runs main() over the sample websites."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.orgs = os.path.join(self.tmp, "orgs.csv")
        self.out = os.path.join(self.tmp, "boards.csv")
        names = ["alpha", "beta", "gamma", "delta", "epsilon", "zeta", "eta"]
        with open(self.orgs, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["ein", "name", "state", "size_tier", "revenue", "website"])
            w.writeheader()
            for i, n in enumerate(names, start=1):
                w.writerow({"ein": f"00000000{i}", "name": n.upper(), "state": "TX", "size_tier": "A",
                            "revenue": str(1000 - i), "website": f"https://{n}.org"})
            # One organization without a website, in tier C.
            w.writerow({"ein": "000000009", "name": "NO SITE", "state": "TX", "size_tier": "C",
                        "revenue": "5", "website": ""})

    def run_main(self, *extra):
        with mock.patch.object(fb, "fetch_page", fake_fetch), contextlib.redirect_stdout(io.StringIO()) as out:
            fb.main(["--orgs", self.orgs, "--out", self.out, "--workers", "3", *extra])
        with open(self.out, newline="") as f:
            return {r["name"]: r for r in csv.DictReader(f)}, out.getvalue()

    def test_full_run(self):
        rows, printed = self.run_main()
        self.assertEqual(rows["ALPHA"]["ats_platform"], "paylocity")
        self.assertEqual(rows["ALPHA"]["board_url"], f"https://recruiting.paylocity.com/Recruiting/Jobs/All/{PAYLOCITY_GUID}")
        self.assertEqual(rows["ALPHA"]["careers_url"], "https://alpha.org/about/careers/")
        self.assertEqual(rows["BETA"]["ats_platform"], "greenhouse")
        self.assertEqual(rows["BETA"]["board_id"], "betaorg")
        self.assertEqual(rows["BETA"]["method"], "homepage")
        self.assertEqual(rows["GAMMA"]["ats_platform"], "workday")
        self.assertEqual(rows["GAMMA"]["method"], "common path")
        self.assertEqual(rows["DELTA"]["ats_platform"], "site_page")
        self.assertEqual(rows["DELTA"]["careers_url"], "https://delta.org/jobs")
        self.assertEqual(rows["EPSILON"]["ats_platform"], "bamboohr")
        self.assertIn("openings link", rows["EPSILON"]["method"])
        self.assertEqual(rows["ZETA"]["ats_platform"], "ukg")
        self.assertEqual(rows["ETA"]["ats_platform"], "not_found")
        self.assertEqual(rows["NO SITE"]["method"], "no website")
        self.assertIn("Checked this run: 8", printed)

    def test_rerun_skips_fresh_rows(self):
        self.run_main()
        _, printed = self.run_main()
        # Everything was checked today, so nothing is checked again.
        self.assertIn("to check now: 0", printed)

    def test_stuck_website_times_out(self):
        # GAMMA's website never answers: its check blocks until the test ends.
        release = fb.threading.Event()
        real_check = fb.check_org

        def slow_check(org):
            if org["name"] == "GAMMA":
                release.wait(10)
            return real_check(org)

        self.addCleanup(release.set)
        with mock.patch.object(fb, "check_org", slow_check):
            rows, _ = self.run_main("--org-timeout", "1")
        # GAMMA is recorded as timed out; everyone else finished normally.
        self.assertEqual(rows["GAMMA"]["ats_platform"], "error")
        self.assertIn("timed out", rows["GAMMA"]["error"])
        self.assertEqual(rows["ALPHA"]["ats_platform"], "paylocity")

    def test_tiers_and_limit(self):
        rows, _ = self.run_main("--tiers", "A", "--limit", "2")
        # Only the two biggest tier-A organizations (by revenue) were checked.
        self.assertEqual(set(rows), {"ALPHA", "BETA"})


if __name__ == "__main__":
    unittest.main()
