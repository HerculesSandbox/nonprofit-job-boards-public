"""
Tests for experiments/reverse_trial.py: the name cleaning and matching. No internet.

Run from the repo's top folder:
    python3 -m unittest discover -s tests -v
"""

import os
import sys
import unittest

# Make experiments/reverse_trial.py importable.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "experiments"))
sys.path.insert(0, ROOT)

import reverse_trial as rt  # noqa: E402


class NameTests(unittest.TestCase):

    def test_clean_name(self):
        # Legal suffixes, "the", page words and anything after " | " are dropped.
        self.assertEqual(rt.clean_name("The Arc of Texas, Inc. | Careers"), "arc texas")
        self.assertEqual(rt.clean_name("Careers at Family Services & Housing"), "family services housing")
        self.assertEqual(rt.clean_name(""), "")

    def test_matcher(self):
        orgs = [
            {"ein": "1", "name": "ARC OF TEXAS INC", "state": "TX"},
            {"ein": "2", "name": "BIG BROTHERS BIG SISTERS OF CENTRAL OHIO", "state": "OH"},
            # Two organizations with the same cleaned name: a match would be a coin flip.
            {"ein": "3", "name": "HOPE HOUSE INC", "state": "MO"},
            {"ein": "4", "name": "HOPE HOUSE", "state": "WI"},
        ]
        m = rt.OrgMatcher(orgs)
        org, score, how = m.match("The Arc of Texas - Job Openings")
        self.assertEqual((org["ein"], how), ("1", "exact"))
        # A small spelling difference still matches, as a near match.
        org, score, how = m.match("Big Brothers Big Sisters of Central Ohio Inc.")
        self.assertEqual(org["ein"], "2")
        org, score, how = m.match("Hope House | Careers")
        self.assertIsNone(org)
        self.assertTrue(how.startswith("ambiguous"))
        # Unrelated names don't match.
        org, _, _ = m.match("Acme Plumbing Supply")
        self.assertIsNone(org)


if __name__ == "__main__":
    unittest.main()
