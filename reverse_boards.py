"""
reverse_boards.py

Stage 2b of the nonprofit job-board finder: the "reverse search". find_boards.py starts from each
organization's website; this starts from the hiring systems themselves.

Common Crawl (a free public archive of billions of web pages, crawled every month or two)
publishes a sorted index of every address it crawled. For each hiring system (applicant
tracking system, ATS) in SYSTEMS, this script:
  1. Reads the slice of that index holding the system's board addresses, for the newest crawls.
  2. Visits each board, at a pace the system tolerates, and reads the organization name it shows.
  3. Matches that name to an organization in orgs.csv.
Then it merges its matches with find_boards.py's results into job_boards.csv: one row per
organization per board (or careers page), saying which method found it.

Outputs, both saved as it goes:
  reverse_boards.csv  every board found in Common Crawl, with its name and match (the progress file)
  job_boards.csv      the combined list stage 3 reads jobs from

How to run (from the folder this file is in, with orgs.csv and boards.csv present):
    python3 reverse_boards.py
    python3 reverse_boards.py --systems bamboohr --max-minutes 10    # a quick test

Runs on GitHub Actions (.github/workflows/reverse-boards.yml). Standard library only; Python 3.8+.
"""

# Lets us write type hints like Optional[str] in a way Python 3.8 accepts.
from __future__ import annotations

# argparse reads command-line options such as --systems.
import argparse
# collections.defaultdict and Counter build lookup tables and summary counts.
import collections
# concurrent.futures downloads several index slices at once.
import concurrent.futures
# csv reads orgs.csv and boards.csv and writes our outputs.
import csv
# datetime stamps each visit, so reruns know what's fresh.
import datetime
# difflib.SequenceMatcher scores how similar two names are, from 0.0 to 1.0.
import difflib
# gzip unpacks the index slices, which are stored compressed.
import gzip
# html turns "&amp;" style escapes back into normal characters.
import html
# json reads Common Crawl's crawl list and index lines, and Greenhouse's board API.
import json
# os builds file paths.
import os
# re is regular expressions: address and name patterns.
import re
# sys lets us print warnings to the error stream.
import sys
# threading and time pace the board visits and enforce --max-minutes.
import threading
import time
# urllib makes the web requests.
import urllib.error
import urllib.parse
import urllib.request
# Type hints: they document what a function expects and returns. Python doesn't enforce them.
from typing import Dict, Iterable, List, Optional, Tuple

# Reuse the job-board finder's page fetching, network-error list and ATS address patterns.
import find_boards as fb


# ---------------------------------------------------------------------------
# SETTINGS
# ---------------------------------------------------------------------------

# Common Crawl's list of crawls, newest first.
COLLINFO_URL = "https://index.commoncrawl.org/collinfo.json"

# Common Crawl's download server, which serves the raw index files. We read those directly,
# because its search server (index.commoncrawl.org) times out under load.
DATA_URL = "https://data.commoncrawl.org/cc-index/collections/{crawl}/indexes/{file}"

# How many of the newest crawls to read. Each covers a different slice of the web.
CRAWLS_TO_SEARCH = 3

# Most index slices ("blocks", about 3,000 addresses each) per system per crawl. Big systems
# like Workday and iCIMS hold many corporate job pages, so this keeps their share bounded.
MAX_BLOCKS = 300

# Revisit a board after this many days, to pick up name changes and boards that went away.
REVISIT_DAYS = 90

# Bump this when a change means boards should be visited again (a new address format, a new way
# to read names). Boards visited by an older version are revisited on the next run.
VISIT_VERSION = "2"

# Hiring systems to search. Keys match find_boards' ATS names, so its patterns turn each address
# into a clean board URL.
#   surts:   where the system's addresses start in "SURT" form, the reversed-domain order Common
#            Crawl sorts its index by (www.example.com/jobs -> "com,example)/jobs"; "www." is
#            dropped). Everything starting with one of these sits together in the index.
#   keep:    a regular expression an address must match to count (drops help pages and so on).
#   workers: boards to visit at the same time; pause: seconds to wait before each visit.
#            Paylocity answers "429 Too Many Requests" when visited faster.
SYSTEMS: Dict[str, Dict] = {
    "paylocity": {"surts": ["com,paylocity,recruiting)/recruiting/jobs/all/"],
                  "keep": r"/recruiting/jobs/all/", "workers": 3, "pause": 0.5},
    "adp": {"surts": ["com,adp,workforcenow)/mascsr/default/mdf/recruitment/", "com,adp,myjobs)/"],
            "keep": r"cid=|myjobs\.adp\.com/", "workers": 3, "pause": 0.5},
    "paycom": {"surts": ["net,paycomonline)/v4/ats/web.php/jobs"],
               "keep": r"clientkey=", "workers": 3, "pause": 0.5},
    "ukg": {"surts": ["com,ultipro,recruiting)/", "com,ultipro,recruiting2)/", "net,ukg,pro,rec,"],
            "keep": r"/jobboard/", "workers": 3, "pause": 0.5},
    "workday": {"surts": ["com,myworkdayjobs,"], "keep": r"myworkdayjobs\.com/", "workers": 5, "pause": 0.2},
    "icims": {"surts": ["com,icims,"], "keep": r"\.icims\.com/", "workers": 5, "pause": 0.2},
    "bamboohr": {"surts": ["com,bamboohr,"], "keep": r"\.bamboohr\.com/(?:careers|jobs)",
                 "workers": 10, "pause": 0.0},
    "applicantpro": {"surts": ["com,applicantpro,"], "keep": r"\.applicantpro\.com", "workers": 5, "pause": 0.2},
    "jazzhr": {"surts": ["com,applytojob,"], "keep": r"\.applytojob\.com", "workers": 5, "pause": 0.2},
    "isolved": {"surts": ["com,isolvedhire,"], "keep": r"\.isolvedhire\.com", "workers": 5, "pause": 0.2},
    "dayforce": {"surts": ["com,dayforcehcm,jobs)/"], "keep": r"jobs\.dayforcehcm\.com/", "workers": 3, "pause": 0.5},
    "paycor": {"surts": ["com,recruitingbypaycor)/career/"], "keep": r"clientid=", "workers": 3, "pause": 0.5},
    "greenhouse": {"surts": ["io,greenhouse,boards)/", "io,greenhouse,job-boards)/"],
                   "keep": r"greenhouse\.io/", "workers": 5, "pause": 0.2},
    "lever": {"surts": ["co,lever,jobs)/"], "keep": r"jobs\.lever\.co/", "workers": 5, "pause": 0.2},
    "ashby": {"surts": ["com,ashbyhq,jobs)/"], "keep": r"jobs\.ashbyhq\.com/", "workers": 5, "pause": 0.2},
}

# Words that say nothing about which organization a name is, removed before comparing names.
# "Foundation", "fund", "association" and "center" are NOT in this list: they tell a company
# ("Acme") apart from its corporate foundation ("Acme Foundation"), both of which can be on the list.
NAME_NOISE = {"inc", "incorporated", "the", "of", "and", "corp", "corporation", "co", "company",
              "llc", "ltd", "org", "careers", "career", "jobs", "job", "openings", "current",
              "opportunities", "page", "employment", "home", "at", "in", "for", "us", "a", "an",
              "join", "team", "our", "work", "with", "board", "portal", "search", "applicant",
              "recruiting", "recruitment", "hiring", "positions", "open", "listings", "welcome", "to",
              # The hiring systems' own names, which appear in many page titles.
              "paylocity", "adp", "paycom", "ukg", "ultipro", "workday", "icims", "bamboohr",
              "applicantpro", "jazzhr", "isolved", "dayforce", "paycor", "greenhouse", "lever", "ashby"}

# Only legal endings are ignored when comparing a board ID with a name ("woundedwarriorproject"
# matches "WOUNDED WARRIOR PROJECT INC", but "accenture" doesn't match "ACCENTURE FOUNDATION INC").
LEGAL_ENDINGS = {"inc", "incorporated", "the", "llc", "ltd", "co", "corp", "corporation"}

# Board IDs shorter than this are too generic to match on ("hope", "arc").
MIN_BOARD_ID_LENGTH = 8

# Workday "site" names that are really Workday's own pages.
NOT_WORKDAY_SITES = {"wday", "robots", "robots.txt", "sitemap", "sitemap.xml", "login", "favicon.ico"}

# Words that mark a name as the hiring system's own, not the organization's.
SYSTEM_WORDS = {"paylocity", "adp", "workforce now", "paycom", "ukg", "ultipro", "workday",
                "icims", "bamboohr", "applicantpro", "jazzhr", "isolved", "dayforce", "paycor",
                "greenhouse", "lever", "ashby", "job not found", "page not found", "error",
                "recruitment", "careers", "career center", "icims careers portal",
                "workday is currently unavailable."}

# How similar two cleaned names must be to count as a near match (1.0 = identical).
MATCH_THRESHOLD = 0.92

HERE = os.path.dirname(os.path.abspath(__file__))

# Column orders for the two outputs.
REVERSE_COLUMNS = ["board_url", "ats_platform", "board_id", "crawl", "name_found", "matched_ein",
                   "matched_name", "matched_state", "score", "how", "visited_at", "error", "visit_version"]
JOB_BOARD_COLUMNS = ["ein", "name", "state", "size_tier", "ats_platform", "board_id", "board_url",
                     "careers_url", "source"]


# ---------------------------------------------------------------------------
# STEP 1: COMMON CRAWL'S INDEX
# ---------------------------------------------------------------------------

def newest_crawls(n: int) -> List[str]:
    """The IDs of the n most recent crawls, e.g. ["CC-MAIN-2026-47", ...]."""
    req = urllib.request.Request(COLLINFO_URL, headers={"User-Agent": fb.USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return [c["id"] for c in json.loads(resp.read().decode("utf-8"))[:n]]


def blocks_for_prefixes(lines: Iterable[str], prefixes: Dict[str, List[str]]) -> Dict[str, List[Tuple[str, int, int]]]:
    """
    Given the lines of a crawl's table of contents (cluster.idx), return the index blocks that may
    hold each system's addresses: {system: [(file, offset, length), ...]}.

    Each line is the first address of one block, then its file, byte offset and length, in sorted
    order. A block can hold addresses starting with a prefix only if the block starts before the
    end of the prefix's range and the next block starts at or after the prefix.
    """
    blocks: Dict[str, List[Tuple[str, int, int]]] = {name: [] for name in prefixes}
    previous = None  # (first address, file, offset, length) of the block before this line
    for line in lines:
        parts = line.rstrip("\n").split("\t")
        if len(parts) < 4:
            continue
        # The first field is "<address> <timestamp>"; we only need the address.
        key = parts[0].split(" ")[0]
        if previous is not None:
            for name, starts in prefixes.items():
                # "￿" sorts after every character, so prefix + it marks the range's end.
                hit = any(previous[0] <= p + "￿" and key >= p for p in starts)
                if hit and len(blocks[name]) < MAX_BLOCKS:
                    blocks[name].append(previous[1:])
        previous = (key, parts[1], int(parts[2]), int(parts[3]))
    return blocks


def find_blocks(crawl: str, prefixes: Dict[str, List[str]]) -> Dict[str, List[Tuple[str, int, int]]]:
    """Download a crawl's cluster.idx line by line and pick out each system's blocks."""
    req = urllib.request.Request(DATA_URL.format(crawl=crawl, file="cluster.idx"),
                                 headers={"User-Agent": fb.USER_AGENT})
    with urllib.request.urlopen(req, timeout=300) as resp:
        # A generator: each line is decoded as it arrives, so the file never sits in memory whole.
        return blocks_for_prefixes((raw.decode("utf-8", errors="replace") for raw in resp), prefixes)


def read_block(crawl: str, block: Tuple[str, int, int], tries: int = 3) -> List[str]:
    """Download one index block with an HTTP range request and return the addresses in it."""
    file, offset, length = block
    text = ""
    for attempt in range(1, tries + 1):
        try:
            # "Range: bytes=A-B" asks the server for just those bytes of the file.
            req = urllib.request.Request(DATA_URL.format(crawl=crawl, file=file), headers={
                "User-Agent": fb.USER_AGENT, "Range": f"bytes={offset}-{offset + length - 1}"})
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


def boards_in_crawls(crawls: List[str], systems: List[str]) -> Dict[str, Dict[str, str]]:
    """Every distinct board Common Crawl holds for `systems`: {board_url lowercased: board info}."""
    boards: Dict[str, Dict[str, str]] = {}
    for crawl in crawls:
        try:
            blocks = find_blocks(crawl, {s: SYSTEMS[s]["surts"] for s in systems})
        except (urllib.error.URLError, OSError) as err:
            print(f"  {crawl}: couldn't read its table of contents: {err}", file=sys.stderr)
            continue
        for system in systems:
            # Download this system's blocks 8 at a time; each is a small file.
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                lists = list(pool.map(lambda b: read_block(crawl, b), blocks[system]))
            before = len(boards)
            for address in (a for addresses in lists for a in addresses):
                if not re.search(SYSTEMS[system]["keep"], address, flags=re.IGNORECASE):
                    continue
                # find_boards' patterns turn an address into a clean board URL and ID.
                found = fb.detect_ats(address)
                if found and found["ats_platform"] == system and found["board_url"]:
                    # Workday addresses that aren't boards: its data API (/wday/...), robots.txt, sitemaps.
                    if system == "workday" and found["board_id"].lower().rsplit("/", 1)[-1] in NOT_WORKDAY_SITES:
                        continue
                    key = found["board_url"].lower()
                    # Keep the first sighting; later crawls only add boards we haven't seen.
                    boards.setdefault(key, dict(found, crawl=crawl))
            print(f"  {crawl} {system}: {len(blocks[system])} blocks, {len(boards) - before} new boards")
    return boards


# ---------------------------------------------------------------------------
# STEP 2: READING EACH BOARD'S ORGANIZATION NAME
# ---------------------------------------------------------------------------

# Places a board page may name its organization, most reliable first.
NAME_PATTERNS = [
    # Job listings marked up for search engines (schema.org JobPosting) name the employer here.
    ("hiring_org", r'"hiringOrganization"\s*:\s*\{[^{}]*?"name"\s*:\s*"([^"]{2,120})"'),
    ("company_json", r'"(?:CompanyName|companyName|company_name|clientName)"\s*:\s*"([^"]{2,120})"'),
    ("og_site_name", r'<meta[^>]+property=["\']og:site_name["\'][^>]+content=["\']([^"\']{2,120})'),
    ("title", r"<title[^>]*>([^<]{2,200})</title>"),
    ("h1", r"<h1[^>]*>\s*([^<]{2,120})</h1>"),
]


def names_on_page(page: str) -> List[str]:
    """Every candidate organization name on a board page, most reliable first, duplicates removed."""
    names: List[str] = []
    for _, pattern in NAME_PATTERNS:
        for match in re.finditer(pattern, page, flags=re.IGNORECASE):
            # Undo "&amp;"-style escapes, including JSON's "&", and squeeze spaces.
            text = html.unescape(match.group(1)).replace("\\u0026", "&")
            text = re.sub(r"\s+", " ", text).strip()
            if text and text not in names:
                names.append(text)
            # One name per source is enough; a page may repeat hiringOrganization on every job.
            break
    return names


# Raw responses saved for systems whose name location is still unknown (adp_samples.txt), so a
# person can read them and point the name pattern at the right field. A few per run is enough.
SAMPLES: Dict[str, List[str]] = collections.defaultdict(list)
MAX_SAMPLES = 15


def note_sample(system: str, url: str, text: str) -> None:
    """Keep the first few raw responses for `system`."""
    if len(SAMPLES[system]) < MAX_SAMPLES:
        SAMPLES[system].append(f"=== {url}\n{text[:3000]}\n")


def visit_board(board: Dict[str, str]) -> Dict[str, str]:
    """Visit one board and return its candidate names (joined by ' || ') or an error."""
    url = board["board_url"]
    try:
        if board["ats_platform"] == "adp" and "cid=" in url:
            # ADP's board page draws its name with JavaScript. Its public job feed may hold it.
            cid = re.search(r"cid=([0-9a-f-]{36})", url, re.I).group(1)
            feed = ("https://workforcenow.adp.com/mascsr/default/careercenter/public/events/staffing/"
                    f"v1/job-requisitions?cid={cid}&lang=en_US&locale=en_US&$top=1")
            _, text = fb.fetch_with_fallback(feed)
            note_sample("adp", feed, text)
            names = names_on_page(text)
        elif board["ats_platform"] == "greenhouse":
            # Greenhouse has a public API that returns the board's name directly.
            api = "https://boards-api.greenhouse.io/v1/boards/" + board["board_id"]
            _, text = fb.fetch_with_fallback(api)
            names = [json.loads(text).get("name", "")]
        else:
            _, page = fb.fetch_with_fallback(url)
            names = names_on_page(page)
        return {"names": " || ".join(n for n in names if n), "error": ""}
    except Exception as err:  # noqa: BLE001  any failure is recorded for this board, never fatal
        return {"names": "", "error": f"{type(err).__name__}: {err}"[:160]}


# ---------------------------------------------------------------------------
# STEP 3: MATCHING NAMES TO orgs.csv
# ---------------------------------------------------------------------------

def name_pieces(name: str) -> List[str]:
    """
    Split a page title into its pieces: "Careers | Acme Family Services - Job Board" ->
    ["Careers", "Acme Family Services", "Job Board"]. Titles put the organization first or last,
    so every piece gets a chance to match.
    """
    return [p.strip() for p in re.split(r"\s[|\-–—:]\s|\s\|\s?|\|", name or "") if p.strip()]


def clean_name(name: str) -> str:
    """Reduce a name to its distinctive words: 'The Arc of Texas, Inc.' -> 'arc texas'."""
    words = re.findall(r"[a-z0-9]+", (name or "").lower().replace("&", " and "))
    return " ".join(w for w in words if w not in NAME_NOISE)


class OrgMatcher:
    """Finds the organization in orgs.csv whose name best matches a board's name."""

    def __init__(self, orgs: List[Dict[str, str]]) -> None:
        # Exact lookups: cleaned name -> organizations with that cleaned name.
        self.exact: Dict[str, List[Dict[str, str]]] = collections.defaultdict(list)
        # The same names with spaces removed, to match board IDs like "goodwillsocal".
        self.squashed: Dict[str, List[Dict[str, str]]] = collections.defaultdict(list)
        # For near matches: first word -> [(cleaned name, organization)], so a board is compared
        # with a few hundred names instead of all 90,000.
        self.by_first_word: Dict[str, List[Tuple[str, Dict[str, str]]]] = collections.defaultdict(list)
        for org in orgs:
            cleaned = clean_name(org["name"])
            if not cleaned:
                continue
            self.exact[cleaned].append(org)
            # The whole name minus legal endings, without spaces, for board-ID matches.
            words = re.findall(r"[a-z0-9]+", org["name"].lower().replace("&", " and "))
            self.squashed["".join(w for w in words if w not in LEGAL_ENDINGS)].append(org)
            self.by_first_word[cleaned.split()[0]].append((cleaned, org))

    def match(self, names: List[str], board_id: str = "") -> Tuple[Optional[Dict[str, str]], float, str, str]:
        """
        Try each candidate name (and each piece of it), then the board's ID.
        Returns (organization or None, similarity 0-1, how it matched, the name that matched).
        """
        pieces = []
        for name in names:
            # Skip names that are really the hiring system's own ("Paylocity", "Page not found").
            if name.lower().strip() in SYSTEM_WORDS:
                continue
            # The whole name first, then each piece of it.
            for piece in [name] + name_pieces(name):
                cleaned = clean_name(piece)
                if cleaned and cleaned not in [p[0] for p in pieces]:
                    pieces.append((cleaned, piece))
        ambiguous = ""
        # 1. An exact match on any piece, if exactly one organization has that name.
        for cleaned, piece in pieces:
            hits = self.exact.get(cleaned, [])
            if len(hits) == 1:
                return hits[0], 1.0, "exact", piece
            if len(hits) > 1 and not ambiguous:
                # Several organizations share the name (common for chapters): too risky to pick one.
                ambiguous = f"ambiguous ({len(hits)} orgs)"
        # 2. The board's ID with spaces and dashes removed ("goodwill-socal" -> "goodwillsocal").
        slug = re.sub(r"[^a-z0-9]", "", (board_id or "").split("/")[0].lower())
        if len(slug) >= MIN_BOARD_ID_LENGTH and len(self.squashed.get(slug, [])) == 1:
            return self.squashed[slug][0], 1.0, "board id", slug
        if ambiguous:
            return None, 1.0, ambiguous, ""
        # 3. The most similar name starting with the same word.
        best, best_score, best_piece = None, 0.0, ""
        for cleaned, piece in pieces:
            for other, org in self.by_first_word.get(cleaned.split()[0], []):
                score = difflib.SequenceMatcher(None, cleaned, other).ratio()
                if score > best_score:
                    best, best_score, best_piece = org, score, piece
        if best is not None and best_score >= MATCH_THRESHOLD:
            return best, best_score, "near", best_piece
        return None, best_score, "", ""


# ---------------------------------------------------------------------------
# STEP 4: VISITING BOARDS (paced per system) AND SAVING
# ---------------------------------------------------------------------------

def read_csv(path: str) -> List[Dict[str, str]]:
    """Read a CSV into a list of dicts; a missing file gives an empty list."""
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def write_csv(path: str, columns: List[str], rows: Iterable[Dict[str, str]]) -> None:
    """Write rows to a CSV through a temporary file, so a crash never leaves half a file."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    os.replace(tmp, path)


def is_fresh(row: Dict[str, str], today: datetime.date) -> bool:
    """True if this board was visited within REVISIT_DAYS (failed visits are retried after a week)."""
    # Visits by an older version of this script are always redone.
    if row.get("visit_version") != VISIT_VERSION:
        return False
    try:
        visited = datetime.date.fromisoformat(row.get("visited_at", ""))
    except ValueError:
        return False
    days = 7 if row.get("error") else REVISIT_DAYS
    return (today - visited).days < days


def visit_all(todo: List[Dict[str, str]], matcher: OrgMatcher, results: Dict[str, Dict[str, str]],
              stop_at: Optional[float], save) -> int:
    """
    Visit every board in `todo`, each system at its own pace, all systems at the same time.
    Stores a result row per board in `results` and calls save() every 200 boards.
    Stops starting new visits after `stop_at`. Returns how many boards were visited.
    """
    by_system: Dict[str, List[Dict[str, str]]] = collections.defaultdict(list)
    for board in todo:
        by_system[board["ats_platform"]].append(board)
    lock = threading.Lock()      # only one thread updates `results` at a time
    count = [0]                  # a list so the inner function can change it

    def work(system: str, queue: List[Dict[str, str]]) -> None:
        # Runs on its own thread: pull boards off this system's list, one at a time.
        while True:
            if stop_at is not None and time.monotonic() > stop_at:
                return
            with lock:
                if not queue:
                    return
                board = queue.pop()
            time.sleep(SYSTEMS[system]["pause"])
            seen = visit_board(board)
            names = [n for n in seen["names"].split(" || ") if n]
            org, score, how, _ = matcher.match(names, board.get("board_id", ""))
            row = {"board_url": board["board_url"], "ats_platform": system,
                   "board_id": board.get("board_id", ""), "crawl": board.get("crawl", ""),
                   "name_found": seen["names"][:300], "matched_ein": org["ein"] if org else "",
                   "matched_name": org["name"] if org else "", "matched_state": org["state"] if org else "",
                   "score": f"{score:.2f}", "how": how,
                   "visited_at": datetime.date.today().isoformat(), "error": seen["error"],
                   "visit_version": VISIT_VERSION}
            with lock:
                results[board["board_url"].lower()] = row
                count[0] += 1
                if count[0] % 200 == 0:
                    save()

    threads = []
    for system, queue in by_system.items():
        # Each system gets its own small group of threads, so a slow one doesn't hold up the rest.
        for _ in range(SYSTEMS[system]["workers"]):
            t = threading.Thread(target=work, args=(system, queue), daemon=True)
            t.start()
            threads.append(t)
    # Wait for every thread. A visit can't take longer than find_boards' page limits allow.
    for t in threads:
        t.join()
    return count[0]


# ---------------------------------------------------------------------------
# STEP 5: THE COMBINED LIST
# ---------------------------------------------------------------------------

def combine(orgs: List[Dict[str, str]], crawl_rows: List[Dict[str, str]],
            reverse_rows: Iterable[Dict[str, str]]) -> List[Dict[str, str]]:
    """
    Merge the website crawl (boards.csv) and the reverse search into one row per organization
    per board or careers page, with source "website crawl", "reverse search" or "both".
    """
    by_ein = {o["ein"]: o for o in orgs}
    merged: Dict[Tuple[str, str], Dict[str, str]] = {}

    def add(ein: str, platform: str, board_id: str, board_url: str, careers_url: str, source: str) -> None:
        org = by_ein.get(ein)
        if not org:
            return
        # The same board found both ways becomes one row marked "both".
        key = (ein, (board_url or careers_url).lower().rstrip("/"))
        if key in merged:
            if merged[key]["source"] != source:
                merged[key]["source"] = "both"
            return
        merged[key] = {"ein": ein, "name": org["name"], "state": org["state"], "size_tier": org["size_tier"],
                       "ats_platform": platform, "board_id": board_id, "board_url": board_url,
                       "careers_url": careers_url, "source": source}

    for r in crawl_rows:
        # A hiring-system board, or jobs posted on the organization's own site.
        if r.get("board_url") or r.get("ats_platform") == "site_page":
            add(r["ein"], r["ats_platform"], r.get("board_id", ""), r.get("board_url", ""),
                r.get("careers_url", ""), "website crawl")
    for r in reverse_rows:
        if r.get("matched_ein"):
            add(r["matched_ein"], r["ats_platform"], r["board_id"], r["board_url"], "", "reverse search")
    order = {"A": 0, "B": 1, "funder": 2, "C": 3}
    return sorted(merged.values(), key=lambda r: (order.get(r["size_tier"], 9), r["state"], r["name"]))


# ---------------------------------------------------------------------------
# THE RUN
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Find job boards by searching Common Crawl.")
    parser.add_argument("--orgs", default=os.path.join(HERE, "orgs.csv"))
    parser.add_argument("--boards", default=os.path.join(HERE, "boards.csv"), help="find_boards.py's output.")
    parser.add_argument("--out", default=os.path.join(HERE, "reverse_boards.csv"))
    parser.add_argument("--combined", default=os.path.join(HERE, "job_boards.csv"))
    parser.add_argument("--systems", default="", help="Comma-separated systems, e.g. paylocity,adp. Default: all.")
    parser.add_argument("--max-minutes", type=float, default=None, help="Stop starting visits after this long.")
    args = parser.parse_args(argv)

    started = time.monotonic()
    stop_at = started + args.max_minutes * 60 if args.max_minutes is not None else None
    systems = [s.strip() for s in args.systems.split(",") if s.strip()] or list(SYSTEMS)
    orgs = read_csv(args.orgs)
    matcher = OrgMatcher(orgs)
    # Earlier visits, so reruns only visit new or stale boards.
    results = {r["board_url"].lower(): r for r in read_csv(args.out)}
    # Re-match every saved board with the current rules. Matching is fast and uses only the names
    # we already saved, so improved rules apply to every board without visiting it again.
    for row in results.values():
        names = [n for n in row.get("name_found", "").split(" || ") if n]
        org, score, how, _ = matcher.match(names, row.get("board_id", ""))
        row.update(matched_ein=org["ein"] if org else "", matched_name=org["name"] if org else "",
                   matched_state=org["state"] if org else "", score=f"{score:.2f}", how=how)

    # 1. Every board Common Crawl holds for these systems.
    crawls = newest_crawls(CRAWLS_TO_SEARCH)
    print("Crawls: " + ", ".join(crawls))
    boards = boards_in_crawls(crawls, systems)
    today = datetime.date.today()
    todo = [b for key, b in boards.items() if not (key in results and is_fresh(results[key], today))]
    print(f"Boards in Common Crawl: {len(boards)}; to visit now: {len(todo)}")

    # 2-3. Visit, name and match, saving as we go.
    def save() -> None:
        write_csv(args.out, REVERSE_COLUMNS, results.values())

    visited = visit_all(todo, matcher, results, stop_at, save)
    save()

    # Raw samples for systems still being worked out (see note_sample).
    for system, texts in SAMPLES.items():
        with open(os.path.join(HERE, f"{system}_samples.txt"), "w", encoding="utf-8") as f:
            f.writelines(texts)

    # 4. The combined list.
    combined = combine(orgs, read_csv(args.boards), results.values())
    write_csv(args.combined, JOB_BOARD_COLUMNS, combined)

    # Summary.
    print(f"\nVisited this run: {visited}; still to visit: {len(todo) - visited}")
    rows = list(results.values())
    print(f"Boards visited so far: {len(rows)}; with a name: {sum(1 for r in rows if r['name_found'])}; "
          f"matched to our list: {sum(1 for r in rows if r['matched_ein'])}")
    for system in systems:
        rs = [r for r in rows if r["ats_platform"] == system]
        if rs:
            print(f"  {system}: {len(rs)} visited, {sum(1 for r in rs if r['name_found'])} named, "
                  f"{sum(1 for r in rs if r['matched_ein'])} matched, {sum(1 for r in rs if r['error'])} errors")
    sources = collections.Counter(r["source"] for r in combined)
    with_board = {r["ein"] for r in combined if r["board_url"]}
    print(f"job_boards.csv: {len(combined)} rows ({dict(sources)}); "
          f"organizations with a hiring-system board: {len(with_board)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
