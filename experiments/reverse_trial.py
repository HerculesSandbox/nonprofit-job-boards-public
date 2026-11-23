"""
experiments/reverse_trial.py

A one-off trial of the "reverse search": instead of starting from each organization's website,
start from the hiring systems themselves. Common Crawl (a free public archive of billions of web
pages, crawled every month or two) keeps a searchable index of every address it crawled, so we
can ask it for every Paylocity board and every BambooHR careers site it saw. Then we read each
board's organization name and match it against orgs.csv.

The trial answers three questions before we build this properly:
  1. How many boards does Common Crawl know about for each system?
  2. Can we read an organization name from each board?
  3. How many of those names match an organization on our list, and how many of those are
     boards the website crawl (find_boards.py) hasn't found?

How to run (from the repo's top folder, with orgs.csv and boards.csv present):
    python3 experiments/reverse_trial.py

Runs on GitHub Actions (.github/workflows/reverse-trial.yml). Standard library only.
"""

# Lets us write type hints in a way Python 3.8 accepts.
from __future__ import annotations

import argparse
import collections
# concurrent.futures visits many boards at once on separate threads.
import concurrent.futures
import csv
# difflib.SequenceMatcher scores how similar two strings are, from 0.0 to 1.0.
import difflib
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Dict, Iterator, List, Optional, Tuple

# Make find_boards.py (one folder up) importable, to reuse its page fetching and ATS patterns.
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
import find_boards as fb  # noqa: E402


# ---------------------------------------------------------------------------
# SETTINGS
# ---------------------------------------------------------------------------

# Common Crawl's list of crawls, newest first, each with the address of its index server.
COLLINFO_URL = "https://index.commoncrawl.org/collinfo.json"

# How many of the newest crawls to search. Each crawl covers a different slice of the web,
# so two crawls find more boards than one.
CRAWLS_TO_SEARCH = 2

# What to ask each crawl's index for, per hiring system (keys match find_boards' ATS names):
#   url:       the address to look up.
#   matchType: "prefix" = any address starting with url; "domain" = any address on that
#              domain or its subdomains.
#   filter:    optional; "~url:<pattern>" keeps only addresses matching a regular expression.
SYSTEMS = {
    "paylocity": {"url": "recruiting.paylocity.com/recruiting/jobs/all/", "matchType": "prefix"},
    "bamboohr": {"url": "bamboohr.com", "matchType": "domain", "filter": "~url:.*\\.bamboohr\\.com/careers.*"},
}

# Most index pages to read per crawl and system (each page holds up to a few thousand addresses).
MAX_INDEX_PAGES = 30

# Most boards per system to visit for their names (keeps the trial under an hour).
MAX_BOARDS_TO_VISIT = 3000

# Words that say nothing about which organization a name is, removed before comparing names.
NAME_NOISE = {"inc", "incorporated", "the", "of", "and", "corp", "corporation", "co", "company",
              "llc", "ltd", "foundation", "fund", "assn", "association", "org", "organization",
              "careers", "career", "jobs", "job", "openings", "current", "opportunities",
              "employment", "home", "page", "at", "in", "for", "us", "a", "an"}

# How similar two cleaned names must be to count as a match (1.0 = identical).
MATCH_THRESHOLD = 0.92


# ---------------------------------------------------------------------------
# COMMON CRAWL INDEX
# ---------------------------------------------------------------------------

def get_json_lines(url: str, tries: int = 4) -> List[dict]:
    """Fetch a URL whose response is one JSON object per line. Retries, since the index is busy."""
    for attempt in range(1, tries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": fb.USER_AGENT})
            with urllib.request.urlopen(req, timeout=120) as resp:
                text = resp.read().decode("utf-8", errors="replace")
            # Each non-blank line is one JSON record.
            return [json.loads(line) for line in text.splitlines() if line.strip()]
        except urllib.error.HTTPError as err:
            # 404 here means "no results for this page", which is a normal answer.
            if err.code == 404:
                return []
            if attempt == tries:
                raise
        except (urllib.error.URLError, OSError, ValueError):
            if attempt == tries:
                raise
        # The index asks clients to slow down when busy; wait longer after each failure.
        time.sleep(15 * attempt)
    return []


def newest_crawls(n: int) -> List[Dict[str, str]]:
    """The n most recent crawls, as {"id", "cdx-api"} dicts."""
    req = urllib.request.Request(COLLINFO_URL, headers={"User-Agent": fb.USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as resp:
        crawls = json.loads(resp.read().decode("utf-8"))
    return crawls[:n]


def index_addresses(api: str, pattern: Dict[str, str], max_pages: int) -> Iterator[str]:
    """Yield every address in one crawl's index that matches `pattern`, page by page."""
    base = {"url": pattern["url"], "matchType": pattern["matchType"], "output": "json", "fl": "url"}
    if pattern.get("filter"):
        base["filter"] = pattern["filter"]
    # First ask how many pages of results there are.
    info = get_json_lines(api + "?" + urllib.parse.urlencode(dict(base, showNumPages="true")))
    pages = int(info[0].get("pages", 0)) if info else 0
    print(f"    {pages} index pages")
    for page in range(min(pages, max_pages)):
        for record in get_json_lines(api + "?" + urllib.parse.urlencode(dict(base, page=page))):
            yield record.get("url", "")
        # Be polite to a free service.
        time.sleep(1)


# ---------------------------------------------------------------------------
# BOARD NAMES
# ---------------------------------------------------------------------------

def board_name(board_url: str) -> Dict[str, str]:
    """Visit a board and pull out the organization name it shows. Returns what it found."""
    try:
        _, page = fb.fetch_with_fallback(board_url)
    except fb.NETWORK_ERRORS as err:
        return {"board_url": board_url, "error": str(err)[:120]}
    found = {"board_url": board_url}
    # Several places a board may name its organization; the matcher tries them in this order.
    patterns = {
        "company_json": r'"(?:CompanyName|companyName|company_name)"\s*:\s*"([^"]{2,120})"',
        "og_site_name": r'<meta[^>]+property=["\']og:site_name["\'][^>]+content=["\']([^"\']{2,120})',
        "title": r"<title[^>]*>([^<]{2,200})</title>",
        "h1": r"<h1[^>]*>\s*([^<]{2,120})</h1>",
    }
    for key, pattern in patterns.items():
        match = re.search(pattern, page, flags=re.IGNORECASE)
        if match:
            # Turn "&amp;" into "&" and squeeze spaces.
            found[key] = re.sub(r"\s+", " ", html.unescape(match.group(1))).strip()
    return found


def clean_name(name: str) -> str:
    """Reduce a name to its distinctive words: 'The Arc of Texas, Inc. | Careers' -> 'arc texas'."""
    # Keep only the part before a separator like " | " or " - " (titles often add "Careers").
    name = re.split(r"\s[|\-–—:]\s", name or "")[0]
    words = re.findall(r"[a-z0-9]+", name.lower().replace("&", " and "))
    return " ".join(w for w in words if w not in NAME_NOISE)


# ---------------------------------------------------------------------------
# MATCHING TO orgs.csv
# ---------------------------------------------------------------------------

class OrgMatcher:
    """Finds the organization in orgs.csv whose name best matches a board's name."""

    def __init__(self, orgs: List[Dict[str, str]]) -> None:
        # Exact lookups: cleaned name -> organizations with that cleaned name.
        self.exact: Dict[str, List[Dict[str, str]]] = collections.defaultdict(list)
        # For near matches: first word -> (cleaned name, organization), to avoid comparing
        # every board against all 90,000 names.
        self.by_first_word: Dict[str, List[Tuple[str, Dict[str, str]]]] = collections.defaultdict(list)
        for org in orgs:
            cleaned = clean_name(org["name"])
            if not cleaned:
                continue
            self.exact[cleaned].append(org)
            self.by_first_word[cleaned.split()[0]].append((cleaned, org))

    def match(self, name: str) -> Tuple[Optional[Dict[str, str]], float, str]:
        """Return (best organization or None, similarity 0-1, how it matched)."""
        cleaned = clean_name(name)
        if not cleaned:
            return None, 0.0, ""
        # An exact match on the cleaned name, if only one organization has it.
        hits = self.exact.get(cleaned, [])
        if len(hits) == 1:
            return hits[0], 1.0, "exact"
        if len(hits) > 1:
            # Several organizations share the name (common for chapters); too ambiguous to trust.
            return None, 1.0, f"ambiguous ({len(hits)} orgs)"
        # Otherwise the most similar name starting with the same word.
        best, best_score = None, 0.0
        for other, org in self.by_first_word.get(cleaned.split()[0], []):
            score = difflib.SequenceMatcher(None, cleaned, other).ratio()
            if score > best_score:
                best, best_score = org, score
        if best is not None and best_score >= MATCH_THRESHOLD:
            return best, best_score, "near"
        return None, best_score, ""


# ---------------------------------------------------------------------------
# THE TRIAL
# ---------------------------------------------------------------------------

def read_csv(path: str) -> List[Dict[str, str]]:
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Trial the Common Crawl reverse search.")
    parser.add_argument("--orgs", default=os.path.join(ROOT, "orgs.csv"))
    parser.add_argument("--boards", default=os.path.join(ROOT, "boards.csv"))
    parser.add_argument("--out", default=os.path.join(ROOT, "trial_boards.csv"))
    args = parser.parse_args(argv)

    orgs = read_csv(args.orgs)
    known = read_csv(args.boards) if os.path.exists(args.boards) else []
    # Boards the website crawl already found, by URL, and the organizations that have one.
    known_urls = {r["board_url"].lower() for r in known if r["board_url"]}
    eins_with_board = {r["ein"] for r in known if r["board_url"]}
    matcher = OrgMatcher(orgs)
    crawls = newest_crawls(CRAWLS_TO_SEARCH)
    print("Crawls searched: " + ", ".join(c["id"] for c in crawls))

    out_rows: List[Dict[str, str]] = []
    summary: List[str] = []
    for system, pattern in SYSTEMS.items():
        print(f"\n{system}: searching the index")
        boards: Dict[str, str] = {}
        for crawl in crawls:
            print(f"  {crawl['id']}")
            try:
                for address in index_addresses(crawl["cdx-api"], pattern, MAX_INDEX_PAGES):
                    # Reuse find_boards' patterns to turn each address into a clean board URL.
                    found = fb.detect_ats(address)
                    if found and found["ats_platform"] == system and found["board_url"]:
                        boards[found["board_url"].lower()] = found["board_url"]
            except (urllib.error.URLError, OSError, ValueError) as err:
                print(f"    index search failed: {err}", file=sys.stderr)
        print(f"  distinct boards in Common Crawl: {len(boards)}")
        to_visit = list(boards.values())[:MAX_BOARDS_TO_VISIT]
        print(f"  visiting {len(to_visit)} boards for their names")
        with concurrent.futures.ThreadPoolExecutor(max_workers=20) as pool:
            visited = list(pool.map(board_name, to_visit))

        named = matched = new = ambiguous = 0
        for v in visited:
            # Try the name sources in order of how reliable they usually are.
            name = v.get("company_json") or v.get("og_site_name") or v.get("title") or v.get("h1") or ""
            org, score, how = matcher.match(name) if name else (None, 0.0, "")
            named += bool(name)
            ambiguous += how.startswith("ambiguous")
            is_new = bool(org) and v["board_url"].lower() not in known_urls and org["ein"] not in eins_with_board
            matched += bool(org)
            new += is_new
            out_rows.append({
                "system": system, "board_url": v["board_url"], "name_found": name,
                "matched_ein": org["ein"] if org else "", "matched_name": org["name"] if org else "",
                "matched_state": org["state"] if org else "", "score": f"{score:.2f}", "how": how,
                "new_board": "yes" if is_new else "", "error": v.get("error", ""),
            })
        line = (f"{system}: {len(boards)} boards in Common Crawl; visited {len(to_visit)}; "
                f"name found {named}; matched to our list {matched}; ambiguous {ambiguous}; "
                f"new (website crawl hadn't found) {new}")
        print("  " + line)
        summary.append(line)

    with open(args.out, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(out_rows[0].keys()) if out_rows else ["system"])
        writer.writeheader()
        writer.writerows(out_rows)
    print("\nTRIAL SUMMARY")
    for line in summary:
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
