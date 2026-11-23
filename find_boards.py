"""
find_boards.py

Stage 2 of the nonprofit job-board finder: for each organization in orgs.csv, finds where it
posts jobs and writes boards.csv.

For each organization with a website, in order of size tier (A, B, funders, then C):
  1. Fetch the homepage and look for a known applicant tracking system (ATS): a link to, or an
     embed from, Paylocity, ADP, UKG, Paycom, Workday, Greenhouse and others (see ATS_PATTERNS).
  2. If none, find the careers link ("Careers", "Jobs", "Employment", "Join our team"...), fetch
     that page and look again. If the homepage has no careers link, try /careers and /jobs.
  3. If the careers page still shows no ATS, follow one "view openings"-style link from it.
  4. Record the result: the ATS and its board ID, "site_page" when jobs are posted on the
     organization's own site, or "not_found".

Results are saved as it goes, so a run that hits its time limit (--max-minutes) picks up where
it left off. Organizations checked in the last --recheck-days days are skipped.

How to run (from the folder this file is in):
    python3 find_boards.py                          # everything, as time allows
    python3 find_boards.py --tiers A --limit 200    # a quick test on 200 big organizations

Runs on GitHub Actions (.github/workflows/find-boards.yml). Standard library only; Python 3.8+.
"""

# Lets us write type hints like Optional[str] in a way Python 3.8 accepts.
from __future__ import annotations

# argparse reads command-line options such as --tiers.
import argparse
# collections.Counter counts results for the summary.
import collections
# concurrent.futures runs many website checks at once on separate threads, since most of the
# time is spent waiting on slow websites rather than computing.
import concurrent.futures
# csv reads orgs.csv and reads and writes boards.csv.
import csv
# datetime stamps each check, so reruns know what's fresh.
import datetime
# html turns "&amp;" style escapes in links back into normal characters.
import html
# html.parser walks a page's tags to collect its links.
from html.parser import HTMLParser
# os builds file paths and checks whether boards.csv exists.
import os
# re is regular expressions: the ATS address patterns.
import re
# socket and ssl name the network errors we catch.
import socket
import ssl
# sys lets us print warnings to the error stream.
import sys
# time measures how long the run has taken, for --max-minutes.
import time
# urllib makes the web requests and joins relative links onto a page's address.
import urllib.error
import urllib.parse
import urllib.request
# Type hints: they document what a function expects and returns. Python doesn't enforce them.
from typing import Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# SETTINGS
# ---------------------------------------------------------------------------

# How many websites to check at the same time. Each check mostly waits on the network,
# so many threads speed things up a lot without overloading anything.
WORKERS = 40

# Seconds to wait for any one web page before giving up on it.
TIMEOUT = 15

# Only the first 2 MB of a page are read; that always covers its links.
MAX_PAGE_BYTES = 2_000_000

# Order to check tiers in: biggest employers first, small ones last.
TIER_ORDER = ["A", "B", "funder", "C"]

# Words that mark a careers link, by link text or address. Earlier words are better matches.
CAREER_WORDS = ["career", "jobs", "job openings", "employment", "join our team", "join the team",
                "work with us", "work for us", "work here", "open positions", "opportunities"]
# Links containing these are about volunteering or board service, not paid jobs.
NOT_CAREER_WORDS = ["volunteer", "board of directors", "board member", "internship-only"]

# Words that mark a link to the actual job list on a careers page.
OPENINGS_WORDS = ["current openings", "open positions", "view openings", "view jobs", "view all jobs",
                  "job openings", "see openings", "apply", "openings", "positions", "job board"]

# Common careers addresses to try when the homepage has no careers link.
COMMON_PATHS = ["/careers", "/jobs", "/employment"]

# A normal browser identity; many sites refuse requests that don't look like one.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130 Safari/537.36"
)

# Known applicant tracking systems: (name, address pattern, how to build a clean board URL).
# The pattern's groups capture the board ID. Matching ignores capitals.
# "{1}" in the board URL is replaced by the first captured group, "{2}" by the second, and so on.
ATS_PATTERNS: List[Tuple[str, str, str]] = [
    ("paylocity", r"recruiting\.paylocity\.com/recruiting/jobs/(?:all|list)/([0-9a-f-]{36})",
     "https://recruiting.paylocity.com/Recruiting/Jobs/All/{1}"),
    ("adp", r"workforcenow\.adp\.com/[^\"'\s<>]*?cid=([0-9a-f-]{36})",
     "https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html?cid={1}"),
    ("adp", r"myjobs\.adp\.com/([a-z0-9-]+)", "https://myjobs.adp.com/{1}"),
    ("ukg", r"recruiting2?\.ultipro\.com/([a-z0-9]+)/jobboard/([0-9a-f-]{36})",
     "https://recruiting.ultipro.com/{1}/JobBoard/{2}"),
    ("ukg", r"([a-z0-9-]+\.rec\.pro\.ukg\.net)/([a-z0-9]+)/jobboard/([0-9a-f-]{36})",
     "https://{1}/{2}/JobBoard/{3}"),
    ("paycom", r"paycomonline\.net/v4/ats/web\.php/jobs\?clientkey=([a-z0-9]+)",
     "https://www.paycomonline.net/v4/ats/web.php/jobs?clientkey={1}"),
    ("paycor", r"recruitingbypaycor\.com/career/careerhome\.action\?clientid=([a-z0-9]+)",
     "https://recruitingbypaycor.com/career/CareerHome.action?clientId={1}"),
    ("dayforce", r"jobs\.dayforcehcm\.com/(?:[a-z]{2}-[a-z]{2}/)?([a-z0-9-]+)",
     "https://jobs.dayforcehcm.com/en-US/{1}"),
    ("workday", r"([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[a-z]{2}/)?([a-z0-9_-]+)",
     "https://{1}.{2}.myworkdayjobs.com/{3}"),
    ("icims", r"(careers-[a-z0-9-]+|[a-z0-9-]+)\.icims\.com", "https://{1}.icims.com/jobs"),
    ("taleo", r"([a-z0-9-]+)\.taleo\.net", "https://{1}.taleo.net"),
    ("bamboohr", r"([a-z0-9-]+)\.bamboohr\.com/(?:careers|jobs)", "https://{1}.bamboohr.com/careers"),
    ("applicantpro", r"([a-z0-9-]+)\.applicantpro\.com", "https://{1}.applicantpro.com/jobs/"),
    ("jazzhr", r"([a-z0-9-]+)\.applytojob\.com", "https://{1}.applytojob.com/apply"),
    ("isolved", r"([a-z0-9-]+)\.isolvedhire\.com", "https://{1}.isolvedhire.com/jobs/"),
    ("clearcompany", r"([a-z0-9-]+)\.clearcompany\.com/careers", "https://{1}.clearcompany.com/careers"),
    # Greenhouse's embed script names the board after "for=", e.g. .../embed/job_board/js?for=acme.
    ("greenhouse", r"greenhouse\.io/embed/job_board(?:/js)?\?for=([a-z0-9]+)", "https://boards.greenhouse.io/{1}"),
    ("greenhouse", r"(?:boards|job-boards)\.greenhouse\.io/([a-z0-9]+)",
     "https://boards.greenhouse.io/{1}"),
    ("greenhouse", r"boards-api\.greenhouse\.io/v1/boards/([a-z0-9]+)", "https://boards.greenhouse.io/{1}"),
    ("lever", r"jobs\.lever\.co/([a-z0-9-]+)", "https://jobs.lever.co/{1}"),
    ("ashby", r"jobs\.ashbyhq\.com/([a-z0-9_.-]+)", "https://jobs.ashbyhq.com/{1}"),
    ("smartrecruiters", r"(?:jobs|careers)\.smartrecruiters\.com/([a-z0-9]+)",
     "https://careers.smartrecruiters.com/{1}"),
    ("workable", r"apply\.workable\.com/([a-z0-9-]+)", "https://apply.workable.com/{1}"),
    ("rippling", r"ats\.rippling\.com/([a-z0-9-]+)", "https://ats.rippling.com/{1}/jobs"),
    ("breezy", r"([a-z0-9-]+)\.breezy\.hr", "https://{1}.breezy.hr"),
    ("recruitee", r"([a-z0-9-]+)\.recruitee\.com", "https://{1}.recruitee.com"),
    ("neogov", r"governmentjobs\.com/careers/([a-z0-9]+)", "https://www.governmentjobs.com/careers/{1}"),
    # A single Paylocity job page names no board, but still tells us the system.
    ("paylocity", r"recruiting\.paylocity\.com/recruiting/jobs/details/(\d+)", ""),
]
# Board IDs that are really parts of the ATS's own website, not a customer's board.
BAD_IDS = {"www", "api", "embed", "careers", "jobs", "app", "help", "support", "static", "assets", "cdn"}

# Column order for boards.csv.
BOARD_COLUMNS = ["ein", "name", "state", "size_tier", "website", "careers_url", "ats_platform",
                 "board_id", "board_url", "method", "checked_at", "error"]

HERE = os.path.dirname(os.path.abspath(__file__))


# ---------------------------------------------------------------------------
# WEB PAGES
# ---------------------------------------------------------------------------

# Network errors that mean "this page couldn't be read", which we record and move past.
NETWORK_ERRORS = (urllib.error.URLError, socket.timeout, ssl.SSLError, ConnectionError, OSError,
                  ValueError, UnicodeError)


def fetch_page(url: str) -> Tuple[str, str]:
    """Download a page. Returns (its final address after any redirects, its text)."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,*/*"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        # Read at most MAX_PAGE_BYTES; giant pages are almost never careers pages.
        data = resp.read(MAX_PAGE_BYTES)
        # resp.geturl() is where we ended up, which matters for joining relative links.
        return resp.geturl(), data.decode("utf-8", errors="replace")


def fetch_with_fallback(url: str) -> Tuple[str, str]:
    """fetch_page, but if an https site has a broken security certificate, retry over http."""
    try:
        return fetch_page(url)
    except urllib.error.URLError as err:
        # Small nonprofits' certificates are often expired; the http version usually still works.
        if url.startswith("https://") and isinstance(getattr(err, "reason", None), ssl.SSLError):
            return fetch_page("http://" + url[len("https://"):])
        raise


class LinkCollector(HTMLParser):
    """Collects every <a href="...">text</a> on a page as (address, text) pairs."""

    def __init__(self) -> None:
        super().__init__()
        self.links: List[Tuple[str, str]] = []
        # The link we're inside right now, and the text seen so far within it.
        self._href: Optional[str] = None
        self._text: List[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            # Start a new link; attrs is a list of (name, value) pairs.
            self._href = dict(attrs).get("href") or ""
            self._text = []
        elif self._href is not None and tag == "img":
            # An image link's alt text counts as its words ("Careers" buttons are often images).
            self._text.append(dict(attrs).get("alt") or "")

    def handle_data(self, data):
        # Words between <a> and </a> become the link's text.
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._href is not None:
            # Close the link: tidy the text and store the pair.
            text = re.sub(r"\s+", " ", " ".join(self._text)).strip()
            self.links.append((self._href, text))
            self._href = None


def page_links(base_url: str, page: str) -> List[Tuple[str, str]]:
    """All links on a page as (absolute address, link text)."""
    collector = LinkCollector()
    try:
        collector.feed(page)
    except Exception:  # noqa: BLE001  a badly broken page still gives us whatever links came before
        pass
    out = []
    for href, text in collector.links:
        # Skip email, phone and in-page links.
        if not href or href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        # Turn "/careers" into "https://example.org/careers".
        out.append((urllib.parse.urljoin(base_url, html.unescape(href.strip())), text))
    return out


def same_site(url_a: str, url_b: str) -> bool:
    """True if two addresses are on the same website (ignoring a leading 'www.')."""
    def host(u: str) -> str:
        return urllib.parse.urlparse(u).netloc.lower().split(":")[0].replace("www.", "", 1)
    return host(url_a) == host(url_b)


# ---------------------------------------------------------------------------
# FINDING THE ATS AND THE CAREERS PAGE
# ---------------------------------------------------------------------------

def detect_ats(text: str) -> Optional[Dict[str, str]]:
    """
    Look for a known ATS address anywhere in `text` (a page's full HTML, so embedded scripts
    and iframes count too). Returns {"ats_platform", "board_id", "board_url"} or None.
    Patterns that name a board win over ones that only name the platform.
    """
    platform_only = None
    for name, pattern, board_url in ATS_PATTERNS:
        for match in re.finditer(pattern, text, flags=re.IGNORECASE):
            groups = [g for g in match.groups()]
            # Skip matches whose "ID" is really part of the ATS's own site, like www.icims.com.
            if groups and groups[0].lower() in BAD_IDS:
                continue
            if not board_url:
                # This pattern only tells us the platform; remember it in case nothing better turns up.
                platform_only = platform_only or {"ats_platform": name, "board_id": "", "board_url": ""}
                break
            # Fill {1}, {2}... in the board URL with the captured groups.
            url = board_url
            for i, value in enumerate(groups, start=1):
                url = url.replace("{%d}" % i, value)
            return {"ats_platform": name, "board_id": "/".join(groups), "board_url": url}
    return platform_only


def score_link(url: str, text: str, words: List[str]) -> int:
    """How strongly a link looks like what we want: 0 = not at all; higher = earlier word in `words`."""
    hay = (text + " " + urllib.parse.urlparse(url).path).lower()
    # Volunteer and board links never count.
    if any(bad in hay for bad in NOT_CAREER_WORDS):
        return 0
    for rank, word in enumerate(words):
        if word in hay:
            return len(words) - rank
    return 0


def best_links(base_url: str, page: str, words: List[str], limit: int = 2) -> List[str]:
    """The `limit` best-matching links on a page for `words`, best first, no duplicates."""
    scored = []
    for url, text in page_links(base_url, page):
        s = score_link(url, text, words)
        if s:
            scored.append((s, url))
    # Highest score first; sorted() keeps page order among equal scores.
    out: List[str] = []
    for _, url in sorted(scored, key=lambda pair: -pair[0]):
        # Drop "#section" endings so the same page isn't counted twice.
        url = url.split("#")[0]
        if url not in out:
            out.append(url)
        if len(out) == limit:
            break
    return out


def check_org(org: Dict[str, str]) -> Dict[str, str]:
    """Find one organization's job board. Returns a row for boards.csv."""
    # Start from the organization's details; the rest is filled in below.
    row = {k: "" for k in BOARD_COLUMNS}
    for k in ("ein", "name", "state", "size_tier", "website"):
        row[k] = org.get(k, "")
    row["checked_at"] = datetime.date.today().isoformat()
    if not row["website"]:
        row["ats_platform"], row["method"] = "not_found", "no website"
        return row
    try:
        # 1. The homepage.
        home_url, home = fetch_with_fallback(row["website"])
        found = detect_ats(home)
        if found:
            row.update(found, careers_url=home_url, method="homepage")
            return row
        # 2. Careers links on the homepage; if there are none, the usual careers addresses.
        candidates = best_links(home_url, home, CAREER_WORDS)
        method = "careers link"
        if not candidates:
            candidates = [urllib.parse.urljoin(home_url, path) for path in COMMON_PATHS]
            method = "common path"
        for url in candidates:
            # A careers link that goes straight to an ATS needs no fetch at all.
            found = detect_ats(url)
            if found:
                row.update(found, careers_url=url, method=method)
                return row
            try:
                careers_url, careers = fetch_with_fallback(url)
            except NETWORK_ERRORS:
                # A missing /careers page is normal; try the next candidate.
                continue
            # A common path only counts if the page really talks about jobs.
            if method == "common path" and not re.search(r"\b(careers?|jobs?|employment)\b", careers, re.I):
                continue
            found = detect_ats(careers)
            if found:
                row.update(found, careers_url=careers_url, method=method)
                return row
            # 3. One more hop: a "view openings" link on the careers page.
            for openings in best_links(careers_url, careers, OPENINGS_WORDS, limit=1):
                found = detect_ats(openings)
                if not found:
                    try:
                        _, page = fetch_with_fallback(openings)
                        found = detect_ats(page)
                    except NETWORK_ERRORS:
                        found = None
                if found:
                    row.update(found, careers_url=careers_url, method=method + " + openings link")
                    return row
            # A careers page on the organization's own site with no ATS: jobs are posted right there.
            if same_site(careers_url, home_url) or method == "common path":
                row.update(ats_platform="site_page", careers_url=careers_url, method=method)
                return row
        row.update(ats_platform="not_found", method="no careers page found")
    except NETWORK_ERRORS as err:
        # The homepage itself couldn't be read (site down, domain expired, blocked).
        row.update(ats_platform="error", error=str(err)[:200])
    except Exception as err:  # noqa: BLE001  one strange site must never stop the whole run
        row.update(ats_platform="error", error=f"{type(err).__name__}: {err}"[:200])
    return row


# ---------------------------------------------------------------------------
# THE RUN
# ---------------------------------------------------------------------------

def read_csv(path: str) -> List[Dict[str, str]]:
    """Read a CSV into a list of dicts; a missing file gives an empty list."""
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def write_boards(path: str, rows: Dict[str, Dict[str, str]]) -> None:
    """Write boards.csv, sorted by tier and then name, so the file is easy to browse."""
    order = {t: i for i, t in enumerate(TIER_ORDER)}
    ordered = sorted(rows.values(), key=lambda r: (order.get(r["size_tier"], 9), r["state"], r["name"]))
    # Write to a temporary file first, then swap it in, so a crash never leaves half a file.
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=BOARD_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(ordered)
    os.replace(tmp, path)


def is_fresh(row: Dict[str, str], today: datetime.date, recheck_days: int) -> bool:
    """True if this organization was checked recently enough to skip. Errors are retried after a week."""
    try:
        checked = datetime.date.fromisoformat(row.get("checked_at", ""))
    except ValueError:
        return False
    days = 7 if row.get("ats_platform") == "error" else recheck_days
    return (today - checked).days < days


def print_summary(rows: Dict[str, Dict[str, str]], checked_now: int, remaining: int) -> None:
    """Print counts for the run's summary page."""
    print()
    print(f"Checked this run: {checked_now}; still to check: {remaining}")
    print(f"Organizations in boards.csv: {len(rows)}")
    platforms = collections.Counter(r["ats_platform"] for r in rows.values())
    print("By result: " + ", ".join(f"{k} {v}" for k, v in platforms.most_common()))
    with_board = [r for r in rows.values() if r["board_url"]]
    print(f"With a readable ATS board: {len(with_board)}")
    for tier in TIER_ORDER:
        tier_rows = [r for r in rows.values() if r["size_tier"] == tier]
        if tier_rows:
            boards = sum(1 for r in tier_rows if r["board_url"])
            pages = sum(1 for r in tier_rows if r["ats_platform"] == "site_page")
            print(f"  tier {tier}: {len(tier_rows)} checked, {boards} ATS boards, {pages} site pages")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Find each organization's job board.")
    parser.add_argument("--orgs", default=os.path.join(HERE, "orgs.csv"), help="Input: orgs.csv from stage 1.")
    parser.add_argument("--out", default=os.path.join(HERE, "boards.csv"), help="Output (and progress file).")
    parser.add_argument("--tiers", default="", help="Comma-separated tiers to check, e.g. A,B. Default: all.")
    parser.add_argument("--limit", type=int, default=None, help="Check at most this many organizations.")
    parser.add_argument("--max-minutes", type=float, default=None, help="Stop and save after this many minutes.")
    parser.add_argument("--recheck-days", type=int, default=30, help="Skip organizations checked this recently.")
    parser.add_argument("--workers", type=int, default=WORKERS, help="How many websites to check at once.")
    args = parser.parse_args(argv)

    started = time.monotonic()
    today = datetime.date.today()
    tiers = [t.strip() for t in args.tiers.split(",") if t.strip()] or TIER_ORDER

    # Earlier results, keyed by EIN, so reruns continue where the last one stopped.
    results = {r["ein"]: r for r in read_csv(args.out)}
    orgs = [o for o in read_csv(args.orgs) if o.get("size_tier") in tiers]
    # Biggest tiers first; within a tier, biggest revenue first.
    order = {t: i for i, t in enumerate(TIER_ORDER)}
    orgs.sort(key=lambda o: (order.get(o["size_tier"], 9), -int(o.get("revenue") or 0)))
    todo = [o for o in orgs if not (o["ein"] in results and is_fresh(results[o["ein"]], today, args.recheck_days))]
    if args.limit is not None:
        todo = todo[:args.limit]
    print(f"Organizations in the chosen tiers: {len(orgs)}; to check now: {len(todo)}")

    checked = 0
    # Work in chunks so progress is saved often and the time limit is checked between chunks.
    chunk_size = args.workers * 10
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        for start in range(0, len(todo), chunk_size):
            if args.max_minutes is not None and (time.monotonic() - started) / 60 > args.max_minutes:
                print(f"Time limit of {args.max_minutes} minutes reached; saving and stopping.")
                break
            chunk = todo[start:start + chunk_size]
            # pool.map runs check_org on every organization in the chunk, `workers` at a time.
            for row in pool.map(check_org, chunk):
                results[row["ein"]] = row
            checked += len(chunk)
            write_boards(args.out, results)
            print(f"  checked {checked} of {len(todo)} ({(time.monotonic() - started) / 60:.0f} min)")

    write_boards(args.out, results)
    print_summary(results, checked, len(todo) - checked)
    return 0


if __name__ == "__main__":
    sys.exit(main())
