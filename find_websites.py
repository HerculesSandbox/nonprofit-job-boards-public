"""
Stage 1b: find websites for organizations whose tax returns didn't list one.

Input:  orgs.csv from stage 1 (build_orgs.py).
Output: websites.csv, one row per U.S. organization with no website in orgs.csv:
        the website we found (or blank), where it came from, and when we looked.
        find_boards.py reads it and uses these websites when orgs.csv has none.

Two free sources, no sign-up needed:
1. Wikidata, the free database behind Wikipedia. Many nonprofits there have their tax ID
   (EIN, Wikidata property P1297) and official website (property P856). One query gets them all.
2. Guessing the domain from the name: "FOOD BANK OF NORTH ALABAMA" -> foodbanknorthalabama.org,
   foodbankofnorthalabama.org, and so on. A guess only counts if the site's title or heading
   names the organization (and, for short names, the page mentions its city or ZIP code),
   so parked domains and other organizations' sites are rejected.

Standard library only; works on Python 3.8+. Run it from GitHub Actions; the sites it
reaches aren't reachable from Claude's network.

    python find_websites.py --max-minutes 150
"""

# Lets type hints like "list[str]" work on Python 3.8.
from __future__ import annotations

# argparse reads command-line options like --max-minutes.
import argparse
# collections.Counter tallies the summary.
import collections
# csv reads orgs.csv and writes websites.csv.
import csv
# datetime stamps each row with the day it was checked.
import datetime
# html.unescape turns "&amp;" in page text back into "&".
import html
# json reads Wikidata's answer.
import json
# os builds file paths and checks whether files exist.
import os
# re finds titles and headings in pages and cleans up names.
import re
# time measures how long the run has taken.
import time
# urllib fetches Wikidata's answer; urllib.parse builds addresses.
import urllib.error
import urllib.parse
import urllib.request
# Type hints: say what kind of data each function takes and returns.
from typing import Dict, Iterable, List, Optional, Tuple

# The job-board finder already knows how to fetch pages politely, retry other spellings of an
# address, and run many checks at once with deadlines. Reuse all of that.
import find_boards as fb

# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

# The folder this script is in, so default file paths work from anywhere.
HERE = os.path.dirname(os.path.abspath(__file__))

# Bump this when the guessing rules change, so every organization is guessed again.
WEBSITES_VERSION = "2"

# Look again at organizations we found nothing for after this many days.
RECHECK_DAYS = 90

# How many organizations to check at once, and how long one may take in total.
WORKERS = 40
ORG_DEADLINE = 120

# Save progress every this many organizations, so a stopped run loses little.
SAVE_EVERY = 500

# Wikidata's query service, and the query: every item with both an EIN and an official website.
WIKIDATA_URL = "https://query.wikidata.org/sparql"
WIKIDATA_QUERY = "SELECT ?ein ?site WHERE { ?item wdt:P1297 ?ein ; wdt:P856 ?site . }"
# Wikidata asks every program to say who it is and where to find out more.
WIKIDATA_AGENT = "nonprofit-job-boards/1.0 (https://github.com/HerculesSandbox/nonprofit-job-boards-public)"

# Domain endings to try, most likely first. Nonprofits mostly use .org.
ENDINGS = [".org", ".com"]

# Guesses longer than this are rarely anyone's real domain, so they aren't tried.
MAX_LABEL_LENGTH = 40
# Guesses shorter than this are too generic ("arc.org" belongs to someone else).
MIN_LABEL_LENGTH = 6

# The IRS abbreviates long words in names; spell them out before guessing.
ABBREVIATIONS = {
    "assn": "association", "assoc": "association", "fdn": "foundation", "fndn": "foundation",
    "ctr": "center", "cntr": "center", "svcs": "services", "svc": "service", "hosp": "hospital",
    "univ": "university", "intl": "international", "natl": "national", "amer": "american",
    "dept": "department", "mgmt": "management", "dev": "development", "hlth": "health",
}
# Legal endings, left off domains ("Example Inc" uses example.org, not exampleinc.org).
LEGAL_ENDINGS = {"inc", "incorporated", "corp", "corporation", "co", "company", "ltd", "llc",
                 "pc", "pllc", "nfp", "dba"}
# Small words that domains often leave out ("foodbanknorthalabama.org").
SMALL_WORDS = {"of", "the", "and", "for", "in", "at", "a", "an", "on", "to"}
# Words that don't tell organizations apart: "Johnson Family Foundation" has one telling word,
# "johnson". They still have to appear on the page, but don't count toward MIN_TELLING_WORDS.
GENERIC_WORDS = {"family", "foundation", "foundations", "fund", "trust", "charitable", "charity",
                 "charities", "memorial"}
# A name with at least this many telling words is distinctive enough on its own; shorter names
# also need the page to mention the organization's city or ZIP code.
MIN_TELLING_WORDS = 3

# Directory, social-media and data sites. Some guessed domains forward to an organization's page
# on one of these, which names the organization but isn't its website.
BLOCKED_HOSTS = re.compile(
    r"(?:^|\.)(?:propublica\.org|guidestar\.org|candid\.org|charitynavigator\.org|causeiq\.com|"
    r"greatnonprofits\.org|idealist\.org|facebook\.com|instagram\.com|linkedin\.com|twitter\.com|"
    r"x\.com|youtube\.com|yelp\.com|wikipedia\.org|opencorporates\.com|bizapedia\.com|"
    r"dnb\.com|irs\.gov|google\.com|sites\.google\.com|godaddysites\.com)$", re.IGNORECASE)

# Words that mark a trust run by a bank ("SMITH JOHN TR UW"): no staff and no website to find.
TRUST_MARKERS = {"tr", "uw", "ua", "ttee", "trst", "est"}

# Words that show a domain is parked (bought and unused) or for sale.
PARKED = re.compile(
    r"domain (?:is )?for sale|buy this domain|this domain (?:may be|is) for sale|parked free|"
    r"domain parking|hugedomains|sedoparking|dan\.com|afternic|godaddy\.com/domainsearch|"
    r"future home of|coming soon|website is under construction", re.IGNORECASE)

# The page's title, its social-media title and site name, and its main headings: the places a
# website names its owner. (?s) lets "." match line breaks inside a heading.
HEAD_PATTERNS = [
    re.compile(r"(?s)<title[^>]*>(.*?)</title>", re.IGNORECASE),
    re.compile(r"<meta[^>]+property=[\"']og:(?:site_name|title)[\"'][^>]+content=[\"']([^\"']*)", re.IGNORECASE),
    re.compile(r"<meta[^>]+content=[\"']([^\"']*)[\"'][^>]+property=[\"']og:(?:site_name|title)", re.IGNORECASE),
    re.compile(r"(?s)<h1[^>]*>(.*?)</h1>", re.IGNORECASE),
]

# The 50 states, DC and the territories: organizations with other "states" are abroad.
US_STATES = set(
    "AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ "
    "NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY PR GU VI AS MP".split())

# websites.csv columns.
COLUMNS = ["ein", "name", "city", "state", "size_tier", "website", "source", "evidence", "tried",
           "checked_at", "version", "error"]


# ---------------------------------------------------------------------------
# Source 1: Wikidata
# ---------------------------------------------------------------------------

def ein_digits(text: str) -> str:
    """An EIN as 9 digits: "74-2394040" -> "742394040". Empty if it isn't one."""
    digits = re.sub(r"\D", "", text or "")
    return digits if len(digits) == 9 else ""


def parse_wikidata(answer: dict) -> Dict[str, str]:
    """Turn Wikidata's JSON answer into {ein: website}. Keeps the first website per EIN."""
    sites: Dict[str, str] = {}
    # SPARQL answers list "bindings": one dict per result row, each value under "value".
    for binding in answer.get("results", {}).get("bindings", []):
        ein = ein_digits(binding.get("ein", {}).get("value", ""))
        site = binding.get("site", {}).get("value", "").strip()
        if ein and site.startswith(("http://", "https://")) and ein not in sites:
            sites[ein] = site
    return sites


def fetch_wikidata() -> Dict[str, str]:
    """Ask Wikidata for every EIN with a website. Returns {} if Wikidata can't be reached."""
    # The query goes in the address; "format=json" asks for JSON instead of XML.
    url = WIKIDATA_URL + "?" + urllib.parse.urlencode({"query": WIKIDATA_QUERY, "format": "json"})
    req = urllib.request.Request(url, headers={"User-Agent": WIKIDATA_AGENT,
                                               "Accept": "application/sparql-results+json"})
    # Two tries: the query service is busy at times and answers "503" or times out.
    for attempt in range(2):
        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                return parse_wikidata(json.load(resp))
        except (urllib.error.URLError, OSError, ValueError) as err:
            print(f"Wikidata attempt {attempt + 1} failed: {err}")
            time.sleep(30)
    return {}


# ---------------------------------------------------------------------------
# Source 2: guessing the domain from the name
# ---------------------------------------------------------------------------

def name_words(name: str) -> List[str]:
    """
    An organization's name as lowercase words, abbreviations spelled out and legal endings dropped:
    "FOOD BANK OF N ALABAMA INC" -> ["food", "bank", "of", "n", "alabama"].
    """
    # "&" is "and" in a domain ("boysandgirlsclub").
    text = (name or "").lower().replace("&", " and ")
    # Apostrophes vanish rather than splitting a word: "children's" -> "childrens".
    text = re.sub(r"['’]", "", text)
    words = re.findall(r"[a-z0-9]+", text)
    words = [ABBREVIATIONS.get(w, w) for w in words]
    return [w for w in words if w not in LEGAL_ENDINGS]


def domain_guesses(name: str) -> List[str]:
    """
    Domains to try for a name, most likely first, e.g. "FOOD BANK OF NORTH ALABAMA" ->
    foodbanknorthalabama.org, foodbankofnorthalabama.org, food-bank-north-alabama.org, then .com.
    Empty for bank-run trusts, which have no website to find.
    """
    words = name_words(name)
    if not words or TRUST_MARKERS & set(words):
        return []
    # The name without small words, which domains usually drop.
    core = [w for w in words if w not in SMALL_WORDS]
    labels = ["".join(core), "".join(words)]
    # Middle initials are usually left out: "henry b tippie foundation" -> "henrytippiefoundation".
    # Only middle ones: "a e m family foundation" without its initials is just "familyfoundation".
    if len(core[0]) > 1:
        labels.append("".join(w for w in core if len(w) > 1))
    if len(core) >= 2:
        # Some organizations use hyphens between the words.
        labels.append("-".join(core))
    # Drop repeats, too-short and too-long labels, keeping the order.
    labels = [lab for lab in dict.fromkeys(labels) if MIN_LABEL_LENGTH <= len(lab) <= MAX_LABEL_LENGTH]
    # Every label with .org first, then every label with .com.
    return [lab + ending for ending in ENDINGS for lab in labels]


def head_text(page: str) -> str:
    """The text of a page's title, social-media title and site name, and main headings, lowercased."""
    pieces = []
    for pattern in HEAD_PATTERNS:
        pieces += pattern.findall(page[:300_000])
    # Remove any HTML tags inside a heading, and turn "&amp;" back into "&".
    text = " ".join(re.sub(r"<[^>]+>", " ", p) for p in pieces)
    return html.unescape(text).lower()


def squash(text: str) -> str:
    """Letters and digits only: "Food Bank of N. Alabama" -> "foodbankofnalabama"."""
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def mentions_place(page: str, org: Dict[str, str]) -> bool:
    """True if the page mentions the organization's city or its 5-digit ZIP code."""
    text = page.lower()
    city = (org.get("city") or "").lower().strip()
    zip5 = (org.get("zip") or "")[:5]
    return bool((city and city in text) or (zip5 and zip5 in text))


def page_names_org(page: str, org: Dict[str, str], domain: str) -> str:
    """
    Whether a fetched homepage belongs to this organization. Returns the evidence:
    "name+place" (the name and the city or ZIP), "name" (the name alone, enough for a long,
    distinctive name), or "" (not this organization). The page must:
    - not be a parked or for-sale domain;
    - show every main word of the name in its title or headings (with the guessed domain itself
      removed, since parked pages often just repeat it);
    - and, for names with fewer than MIN_TELLING_WORDS telling words, which other organizations
      share, mention the organization's city or ZIP code.
    """
    if PARKED.search(page[:300_000]):
        return ""
    core = [w for w in name_words(org.get("name", "")) if w not in SMALL_WORDS]
    if not core:
        return ""
    # Take out the domain as written ("foodbanknorthalabama.org"), so a parked page whose title
    # is just the domain doesn't count as naming the organization. Then squash, so "FoodBank"
    # in a title still contains "food" and "bank".
    head = squash(head_text(page).replace(domain.lower(), " "))
    if not all(w in head for w in core):
        return ""
    if mentions_place(page, org):
        return "name+place"
    # Telling words: not generic, and not single letters (initials).
    telling = [w for w in core if w not in GENERIC_WORDS and len(w) > 1]
    return "name" if len(telling) >= MIN_TELLING_WORDS else ""


def home_address(url: str) -> str:
    """Just the scheme and host of an address: "https://www.x.org:443/home" -> "https://www.x.org"."""
    parts = urllib.parse.urlsplit(url)
    # .hostname drops a port number (":443") and lowercases the host.
    return f"{parts.scheme}://{parts.hostname or ''}"


def host_key(url: str) -> str:
    """A website's host without "www.", for telling whether two addresses are the same site."""
    host = urllib.parse.urlsplit(url if "//" in url else "//" + url).hostname or ""
    return host[4:] if host.startswith("www.") else host


def guess_website(org: Dict[str, str]) -> Dict[str, str]:
    """Try domain guesses for one organization. Returns a websites.csv row."""
    row = new_row(org)
    tried = []
    for domain in domain_guesses(org.get("name", "")):
        tried.append(domain)
        try:
            # fetch_home also tries "www." and http, and broken certificates.
            final_url, page = fb.fetch_home("https://" + domain)
        except fb.NETWORK_ERRORS:
            # Most guesses don't exist at all; that fails quickly.
            continue
        # A guess that forwards to a directory or social-media page isn't the organization's site.
        if BLOCKED_HOSTS.search(urllib.parse.urlsplit(final_url).hostname or ""):
            continue
        evidence = page_names_org(page, org, domain)
        if evidence:
            row.update(website=home_address(final_url), source="guess", evidence=evidence)
            break
    row["tried"] = " ".join(tried)
    return row


# ---------------------------------------------------------------------------
# Rows and the run
# ---------------------------------------------------------------------------

def new_row(org: Dict[str, str]) -> Dict[str, str]:
    """A websites.csv row for an organization, with nothing found yet."""
    row = {k: "" for k in COLUMNS}
    for k in ("ein", "name", "city", "state", "size_tier"):
        row[k] = org.get(k, "")
    row["checked_at"] = datetime.date.today().isoformat()
    row["version"] = WEBSITES_VERSION
    return row


def timed_out_row(org: Dict[str, str], seconds: float) -> Dict[str, str]:
    """The row for an organization whose guesses took too long."""
    row = new_row(org)
    row["error"] = f"timed out after {seconds:.0f}s"
    return row


def is_fresh(row: Dict[str, str], today: datetime.date) -> bool:
    """True if this organization was guessed recently enough, with the current rules, to skip."""
    if row.get("version") != WEBSITES_VERSION:
        return False
    # A website found by guessing stays found; nothing-found is retried after RECHECK_DAYS.
    if row.get("website"):
        return True
    try:
        checked = datetime.date.fromisoformat(row.get("checked_at", ""))
    except ValueError:
        return False
    return (today - checked).days < RECHECK_DAYS


def needs_website(org: Dict[str, str]) -> bool:
    """True for a U.S. organization whose tax return listed no website."""
    return not org.get("website") and org.get("state", "") in US_STATES


def read_csv(path: str) -> List[Dict[str, str]]:
    """Read a CSV into a list of dicts; a missing file gives an empty list."""
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def write_websites(path: str, rows: Iterable[Dict[str, str]]) -> None:
    """Write websites.csv, sorted by tier and name, through a temporary file so it's never half-written."""
    order = {t: i for i, t in enumerate(fb.TIER_ORDER)}
    rows = sorted(rows, key=lambda r: (order.get(r.get("size_tier", ""), 9), r.get("name", "")))
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    # Replacing in one step means a crash mid-write leaves the old file intact.
    os.replace(tmp, path)


def sort_key(org: Dict[str, str]) -> Tuple[int, float]:
    """Biggest tiers first; within a tier, largest revenue or assets (for foundations) first."""
    order = {t: i for i, t in enumerate(fb.TIER_ORDER)}
    size = max(float(org.get("revenue") or 0), float(org.get("assets") or 0))
    return order.get(org.get("size_tier", ""), 9), -size


def drop_shared_guesses(rows: Dict[str, Dict[str, str]], known: Dict[str, int]) -> int:
    """
    A website is one organization's. When a guessed site is also claimed by another organization
    (another guess, or a website listed in orgs.csv), keep it only for a single guess whose page
    also gave that organization's city or ZIP code; clear it for the rest.
    `known` counts orgs.csv's websites by host. Returns how many guesses were cleared.
    """
    # Which guessed rows claim each site.
    claims: Dict[str, List[Dict[str, str]]] = collections.defaultdict(list)
    for row in rows.values():
        if row.get("source") == "guess" and row.get("website"):
            claims[host_key(row["website"])].append(row)
    cleared = 0
    for host, claimants in claims.items():
        if len(claimants) + known.get(host, 0) < 2:
            continue
        with_place = [r for r in claimants if r.get("evidence") == "name+place"]
        # A single claimant with the city or ZIP keeps it, unless orgs.csv already gives the site
        # to someone else.
        keep = with_place[0] if len(with_place) == 1 and not known.get(host) else None
        for r in claimants:
            if r is not keep:
                r.update(website="", source="", evidence="", error=f"site also claimed by another org: {host}")
                cleared += 1
    return cleared


def print_summary(rows: Dict[str, Dict[str, str]], checked: int) -> None:
    """Print what was found, by source and tier."""
    found = [r for r in rows.values() if r.get("website")]
    print(f"\nChecked this run: {checked}")
    print(f"Organizations needing a website: {len(rows)}; found: {len(found)}")
    print("Found by source:", dict(collections.Counter(r["source"] for r in found)))
    by_tier = collections.Counter(r["size_tier"] for r in rows.values())
    found_tier = collections.Counter(r["size_tier"] for r in found)
    for tier in fb.TIER_ORDER:
        print(f"  tier {tier}: {found_tier[tier]} of {by_tier[tier]}")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Find websites for organizations whose returns listed none.")
    parser.add_argument("--orgs", default=os.path.join(HERE, "orgs.csv"), help="Input: orgs.csv from stage 1.")
    parser.add_argument("--out", default=os.path.join(HERE, "websites.csv"), help="Output (and progress file).")
    parser.add_argument("--max-minutes", type=float, default=None, help="Stop starting guesses after this many minutes.")
    parser.add_argument("--limit", type=int, default=None, help="Guess for at most this many organizations.")
    parser.add_argument("--workers", type=int, default=WORKERS, help="How many organizations to check at once.")
    parser.add_argument("--no-wikidata", action="store_true", help="Skip the Wikidata lookup.")
    args = parser.parse_args(argv)

    started = time.monotonic()
    today = datetime.date.today()

    # The organizations that need a website, and what earlier runs found for them.
    orgs = sorted((o for o in read_csv(args.orgs) if needs_website(o)), key=sort_key)
    earlier = {r["ein"]: r for r in read_csv(args.out)}
    # Keep only rows for organizations still on the list (an org that now lists a website drops off).
    rows = {o["ein"]: earlier[o["ein"]] for o in orgs if o["ein"] in earlier}
    print(f"U.S. organizations with no website in orgs.csv: {len(orgs)}")

    # 1. Wikidata, every run: it's one query, and it overrides guesses, since people chose those sites.
    wikidata = {} if args.no_wikidata else fetch_wikidata()
    print(f"Wikidata organizations with a website: {len(wikidata)}")
    from_wikidata = 0
    for o in orgs:
        site = wikidata.get(ein_digits(o["ein"]))
        if site:
            row = rows.get(o["ein"]) or new_row(o)
            row.update(website=site, source="wikidata", error="")
            rows[o["ein"]] = row
            from_wikidata += 1
    print(f"  of them, organizations on our list: {from_wikidata}")

    # 2. Guesses for the rest that weren't checked recently.
    todo = [o for o in orgs if rows.get(o["ein"], {}).get("source") != "wikidata"
            and not (o["ein"] in rows and is_fresh(rows[o["ein"]], today))]
    if args.limit is not None:
        todo = todo[:args.limit]
    print(f"Organizations to guess for now: {len(todo)}")

    stop_at = started + args.max_minutes * 60 if args.max_minutes is not None else None
    # A one-item list so save() below can update the count.
    count = [0]

    def save(row: Dict[str, str]) -> None:
        # Store each finished row, and save the file every SAVE_EVERY rows.
        rows[row["ein"]] = row
        count[0] += 1
        if count[0] % SAVE_EVERY == 0:
            write_websites(args.out, rows.values())
            print(f"  guessed for {count[0]} of {len(todo)} ({(time.monotonic() - started) / 60:.0f} min)")

    # The finder's thread runner, with this script's check and timeout functions.
    checked = fb.run_checks(todo, args.workers, ORG_DEADLINE, stop_at, save,
                            check=guess_website, timeout_row=timed_out_row)
    if checked < len(todo):
        print(f"Time limit of {args.max_minutes} minutes reached; saving and stopping.")
    # Hosts of the websites orgs.csv already gives to organizations.
    known = collections.Counter(host_key(o["website"]) for o in read_csv(args.orgs) if o.get("website"))
    print(f"Guesses cleared because another organization claims the site: {drop_shared_guesses(rows, known)}")
    write_websites(args.out, rows.values())
    print_summary(rows, checked)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
