"""
Tests for find_websites.py. They use made-up organizations and fake websites only,
and never touch the internet.

    python3 -m unittest discover -s tests -v
"""

# csv writes the sample orgs.csv; os and tempfile give each test its own folder.
import csv
import os
import sys
import tempfile
import unittest
import urllib.error
# mock swaps the real page fetcher and Wikidata lookup for fakes.
from unittest import mock

# Let the tests import the scripts from the folder above this one.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import find_boards as fb  # noqa: E402  (imported after the path change on purpose)
import find_websites as fw  # noqa: E402

# Fake homepages, by address.
SITES = {
    # The real site: its title names the organization and the page gives its city.
    "https://springfieldfoodbank.org": "<title>Springfield Food Bank | Feeding Springfield</title>"
                                       "<p>123 Main St, Springfield, IL 62701</p>",
    # A parked domain whose title is just the domain.
    "https://exampleyouthcenter.org": "<title>exampleyouthcenter.org</title><p>This domain is for sale!</p>",
    # A real site with the same name, in another city (two main words, so the city must match).
    "https://riverhouse.org": "<title>River House</title><p>Visit us in Portland, OR</p>",
    # A site found only under .com, with hyphens.
    "https://www.lakeside-arts-education-council.com": "<h1>Lakeside Arts &amp; Education Council</h1>",
    # A family foundation's site with no city: "family" and "foundation" don't tell orgs apart.
    "https://examplefamilyfoundation.org": "<title>Example Family Foundation</title>",
    # A guessed domain that forwards to the organization's page on a directory site.
    "https://springfieldseniorcenter.org": "<title>Springfield Senior Center - Nonprofit Explorer</title>"
                                           "<p>Springfield, IL</p>",
}

# Where a fake domain forwards to: (final address, page).
FORWARDS = {"https://springfieldseniorcenter.org": "https://projects.propublica.org/nonprofits/organizations/1"}

# Sample organizations, as orgs.csv rows.
ORGS = [
    {"ein": "000000001", "name": "SPRINGFIELD FOOD BANK INC", "city": "SPRINGFIELD", "state": "IL",
     "zip": "62701-1234", "size_tier": "A", "revenue": "5000000", "assets": "", "website": ""},
    {"ein": "000000002", "name": "EXAMPLE YOUTH CENTER", "city": "SPRINGFIELD", "state": "IL",
     "zip": "62702", "size_tier": "B", "revenue": "2000000", "assets": "", "website": ""},
    {"ein": "000000003", "name": "RIVER HOUSE", "city": "SPRINGFIELD", "state": "IL",
     "zip": "62703", "size_tier": "C", "revenue": "1000000", "assets": "", "website": ""},
    {"ein": "000000004", "name": "LAKESIDE ARTS & EDUCATION COUNCIL", "city": "SPRINGFIELD", "state": "IL",
     "zip": "62704", "size_tier": "C", "revenue": "900000", "assets": "", "website": ""},
    # Found on Wikidata.
    {"ein": "000000005", "name": "EXAMPLE ARTS FDN", "city": "SPRINGFIELD", "state": "IL",
     "zip": "62705", "size_tier": "funder", "revenue": "", "assets": "50000000", "website": ""},
    # Already has a website: not on the list.
    {"ein": "000000006", "name": "HAS A SITE", "city": "SPRINGFIELD", "state": "IL",
     "zip": "62706", "size_tier": "A", "revenue": "9000000", "assets": "", "website": "https://hasasite.org"},
    # Abroad: not on the list.
    {"ein": "000000007", "name": "OVERSEAS SCHOOL", "city": "MILAN", "state": "",
     "zip": "", "size_tier": "A", "revenue": "9000000", "assets": "", "website": ""},
]


def fake_fetch_home(url):
    """Stand-in for find_boards.fetch_home: serves SITES (with or without www.), or fails like a missing domain."""
    for address in (url, url.replace("https://", "https://www.")):
        if address in SITES:
            return FORWARDS.get(address, address + "/"), SITES[address]
    raise urllib.error.URLError("Name or service not known")


class GuessTests(unittest.TestCase):

    def test_domain_guesses(self):
        self.assertEqual(fw.domain_guesses("FOOD BANK OF N ALABAMA INC")[:3],
                         ["foodbanknalabama.org", "foodbankofnalabama.org", "foodbankalabama.org"])
        # .com guesses come after every .org guess.
        guesses = fw.domain_guesses("SPRINGFIELD FOOD BANK")
        self.assertEqual(guesses, ["springfieldfoodbank.org", "springfield-food-bank.org",
                                   "springfieldfoodbank.com", "springfield-food-bank.com"])
        # Abbreviations are spelled out.
        self.assertEqual(fw.domain_guesses("EXAMPLE ARTS FDN")[0], "exampleartsfoundation.org")
        # Bank-run trusts get no guesses.
        self.assertEqual(fw.domain_guesses("SMITH JOHN TR UW"), [])

    def test_guess_website(self):
        with mock.patch.object(fb, "fetch_home", fake_fetch_home):
            found = {o["ein"]: fw.guess_website(o) for o in ORGS[:4]}
        self.assertEqual(found["000000001"]["website"], "https://springfieldfoodbank.org")
        self.assertEqual(found["000000001"]["source"], "guess")
        # Parked domain: rejected.
        self.assertEqual(found["000000002"]["website"], "")
        # Same short name in another city: rejected.
        self.assertEqual(found["000000003"]["website"], "")
        # Four telling words, so no city needed; found under .com with hyphens and www.
        self.assertEqual(found["000000004"]["website"], "https://www.lakeside-arts-education-council.com")
        self.assertEqual(found["000000004"]["evidence"], "name")
        self.assertEqual(found["000000001"]["evidence"], "name+place")

    def test_generic_names_need_a_place(self):
        org = dict(ORGS[0], ein="000000010", name="EXAMPLE FAMILY FOUNDATION")
        with mock.patch.object(fb, "fetch_home", fake_fetch_home):
            self.assertEqual(fw.guess_website(org)["website"], "")

    def test_directory_pages_are_rejected(self):
        org = dict(ORGS[0], ein="000000011", name="SPRINGFIELD SENIOR CENTER")
        with mock.patch.object(fb, "fetch_home", fake_fetch_home):
            self.assertEqual(fw.guess_website(org)["website"], "")

    def test_middle_initials_only(self):
        self.assertIn("henrytippiefoundation.org", fw.domain_guesses("HENRY B TIPPIE FOUNDATION"))
        self.assertNotIn("familyfoundation.org", fw.domain_guesses("A E M FAMILY FOUNDATION"))

    def test_shared_sites_go_to_one_org(self):
        rows = {
            "1": {"source": "guess", "website": "https://www.smithfund.org", "evidence": "name+place"},
            "2": {"source": "guess", "website": "https://smithfund.org", "evidence": "name"},
            # Claims a site orgs.csv already gives to another organization.
            "3": {"source": "guess", "website": "https://parentorg.org", "evidence": "name+place"},
        }
        cleared = fw.drop_shared_guesses(rows, {"parentorg.org": 1})
        self.assertEqual(cleared, 2)
        self.assertEqual(rows["1"]["website"], "https://www.smithfund.org")
        self.assertEqual(rows["2"]["website"], "")
        self.assertEqual(rows["3"]["website"], "")

    def test_parse_wikidata(self):
        answer = {"results": {"bindings": [
            {"ein": {"value": "00-0000005"}, "site": {"value": "https://exampleartsfoundation.org/"}},
            {"ein": {"value": "not an ein"}, "site": {"value": "https://x.org"}},
        ]}}
        self.assertEqual(fw.parse_wikidata(answer), {"000000005": "https://exampleartsfoundation.org/"})


class FullRunTest(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.orgs = os.path.join(self.dir, "orgs.csv")
        self.out = os.path.join(self.dir, "websites.csv")
        with open(self.orgs, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(ORGS[0]))
            w.writeheader()
            w.writerows(ORGS)

    def run_main(self):
        wikidata = {"000000005": "https://exampleartsfoundation.org/"}
        with mock.patch.object(fb, "fetch_home", fake_fetch_home), \
                mock.patch.object(fw, "fetch_wikidata", lambda: wikidata), \
                mock.patch("builtins.print"):
            fw.main(["--orgs", self.orgs, "--out", self.out, "--workers", "2"])
        with open(self.out, newline="") as f:
            return {r["ein"]: r for r in csv.DictReader(f)}

    def test_full_run(self):
        rows = self.run_main()
        # Only U.S. organizations with no website are listed.
        self.assertEqual(sorted(rows), ["000000001", "000000002", "000000003", "000000004", "000000005"])
        self.assertEqual(rows["000000005"]["source"], "wikidata")
        self.assertEqual(rows["000000001"]["source"], "guess")
        # A second run skips everything it already checked.
        with mock.patch.object(fw, "guess_website", side_effect=AssertionError("should not guess")):
            self.run_main()

    def test_finder_uses_found_websites(self):
        # find_boards fills in a website from websites.csv when orgs.csv has none.
        self.run_main()
        boards = os.path.join(self.dir, "boards.csv")
        checked = []
        with mock.patch.object(fb, "check_org", lambda o: checked.append(o) or fb.timed_out_row(o, 1)), \
                mock.patch("builtins.print"):
            fb.main(["--orgs", self.orgs, "--out", boards, "--websites", self.out, "--workers", "2"])
        sites = {o["ein"]: o["website"] for o in checked}
        self.assertEqual(sites["000000001"], "https://springfieldfoodbank.org")
        self.assertEqual(sites["000000002"], "")


if __name__ == "__main__":
    unittest.main()
