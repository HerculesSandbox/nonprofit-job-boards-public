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
# gzip unpacks the index slices, which are stored compressed.
import gzip
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

# Common Crawl's list of crawls, newest first.
COLLINFO_URL = "https://index.commoncrawl.org/collinfo.json"

# Common Crawl's download server, which serves the raw index files. We read those directly,
# because the search server (index.commoncrawl.org) times out under load.
DATA_URL = "https://data.commoncrawl.org/cc-index/collections/{crawl}/indexes/{file}"

# How many of the newest crawls to search. Each crawl covers a different slice of the web,
# so two crawls find more boards than one.
CRAWLS_TO_SEARCH = 3

# What to look for, per hiring system (keys match find_boards' ATS names):
#   surt: the start of the address in "SURT" form, the reversed-domain order Common Crawl sorts
#         its index by (www.example.com/jobs -> "com,example,www)/jobs"). Every address starting
#         with this sits together in the index, so we only read that slice.
#   keep: a regular expression the address must also match.
SYSTEMS = {
    "paylocity": {"surt": "com,paylocity,recruiting)/recruiting/jobs/all/", "keep": r"/recruiting/jobs/all/",
                  # Paylocity answers "429 Too Many Requests" at 20 visits at a time, so go slowly.
                  "workers": 3, "pause": 0.5, "max_visits": 1500},
}
# BambooHR was measured in the first successful trial: 5,686 boards; of 3,000 visited, 198 matched
# an organization on the list and 160 of those were new. Re-add it here to measure it again.

# Most index slices ("blocks", about 3,000 addresses each) to read per crawl and system.
MAX_BLOCKS = 300

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

def newest_crawls(n: int) -> List[Dict[str, str]]:
    """The n most recent crawls, as {"id", ...} dicts."""
    req = urllib.request.Request(COLLINFO_URL, headers={"User-Agent": fb.USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as resp:
        crawls = json.loads(resp.read().decode("utf-8"))
    return crawls[:n]


def find_blocks(crawl: str, prefixes: Dict[str, str]) -> Dict[str, List[Tuple[str, int, int]]]:
    """
    Read a crawl's table of contents (cluster.idx) and return, for each system, the index blocks
    that may hold addresses starting with its SURT prefix: {system: [(file, offset, length), ...]}.

    Each line of cluster.idx is the first address of one block, then its file, byte offset and
    length. The lines are in sorted order, so a block can hold our addresses only if it starts
    before the end of our prefix range and the next block starts after its beginning.
    """
    url = DATA_URL.format(crawl=crawl, file="cluster.idx")
    req = urllib.request.Request(url, headers={"User-Agent": fb.USER_AGENT})
    blocks: Dict[str, List[Tuple[str, int, int]]] = {name: [] for name in prefixes}
    previous = None  # (first address, file, offset, length) of the block before this line
    with urllib.request.urlopen(req, timeout=300) as resp:
        # Read the file line by line as it downloads, never holding all of it in memory.
        for raw in resp:
            parts = raw.decode("utf-8", errors="replace").rstrip("\n").split("\t")
            if len(parts) < 4:
                continue
            # The first field is "<address> <timestamp>"; we only need the address.
            key = parts[0].split(" ")[0]
            current = (key, parts[1], int(parts[2]), int(parts[3]))
            if previous is not None:
                for name, prefix in prefixes.items():
                    # "\uffff" sorts after every character, so prefix + it marks the range's end.
                    if previous[0] <= prefix + "\uffff" and key >= prefix and len(blocks[name]) < MAX_BLOCKS:
                        blocks[name].append(previous[1:])
            previous = current
    return blocks


def read_block(crawl: str, block: Tuple[str, int, int], tries: int = 3) -> List[str]:
    """Download one index block with a range request, unpack it, and return its addresses."""
    file, offset, length = block
    url = DATA_URL.format(crawl=crawl, file=file)
    for attempt in range(1, tries + 1):
        try:
            # "Range: bytes=A-B" asks for just those bytes of the file.
            req = urllib.request.Request(url, headers={"User-Agent": fb.USER_AGENT,
                                                       "Range": f"bytes={offset}-{offset + length - 1}"})
            with urllib.request.urlopen(req, timeout=120) as resp:
                text = gzip.decompress(resp.read()).decode("utf-8", errors="replace")
            break
        except (urllib.error.URLError, OSError, EOFError):
            if attempt == tries:
                return []
            time.sleep(10 * attempt)
    addresses = []
    for line in text.splitlines():
        # Each line is "<surt> <timestamp> {json}"; the JSON holds the real address.
        brace = line.find("{")
        if brace != -1:
            try:
                addresses.append(json.loads(line[brace:]).get("url", ""))
            except ValueError:
                continue
    return addresses


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
    # For each crawl, which index blocks to read for each system (one pass over cluster.idx).
    blocks_by_crawl: Dict[str, Dict[str, List[Tuple[str, int, int]]]] = {}
    for crawl in crawls:
        try:
            blocks_by_crawl[crawl["id"]] = find_blocks(crawl["id"], {k: v["surt"] for k, v in SYSTEMS.items()})
            print(f"  {crawl['id']}: " + ", ".join(f"{k} {len(v)} blocks" for k, v in blocks_by_crawl[crawl["id"]].items()))
        except (urllib.error.URLError, OSError) as err:
            print(f"  {crawl['id']}: couldn't read cluster.idx: {err}", file=sys.stderr)

    out_rows: List[Dict[str, str]] = []
    summary: List[str] = []
    for system, pattern in SYSTEMS.items():
        print(f"\n{system}: searching the index")
        boards: Dict[str, str] = {}
        for crawl_id, by_system in blocks_by_crawl.items():
            blocks = by_system.get(system, [])
            # Read the blocks 8 at a time; each is a small download.
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                lists = list(pool.map(lambda b: read_block(crawl_id, b), blocks))
            seen = 0
            for addresses in lists:
                for address in addresses:
                    if not re.search(pattern["keep"], address, flags=re.IGNORECASE):
                        continue
                    seen += 1
                    # Reuse find_boards' patterns to turn each address into a clean board URL.
                    found = fb.detect_ats(address)
                    if found and found["ats_platform"] == system and found["board_url"]:
                        boards[found["board_url"].lower()] = found["board_url"]
            print(f"  {crawl_id}: {len(blocks)} blocks, {seen} matching addresses")
        print(f"  distinct boards in Common Crawl: {len(boards)}")
        to_visit = list(boards.values())[:pattern.get("max_visits", MAX_BOARDS_TO_VISIT)]
        print(f"  visiting {len(to_visit)} boards for their names")
        pause = pattern.get("pause", 0)

        def visit(url: str) -> Dict[str, str]:
            # Wait a moment before each visit when this system asks us to slow down.
            time.sleep(pause)
            return board_name(url)

        with concurrent.futures.ThreadPoolExecutor(max_workers=pattern.get("workers", 20)) as pool:
            visited = list(pool.map(visit, to_visit))

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
