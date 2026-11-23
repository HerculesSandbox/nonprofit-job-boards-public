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
# queue passes finished results from the worker threads back to the main loop safely.
import queue
# re is regular expressions: the ATS address patterns.
import re
# socket and ssl name the network errors we catch.
import socket
import ssl
# sys lets us print warnings to the error stream.
import sys
# threading runs many website checks at once, since most of the time is spent waiting on slow
# websites rather than computing.
import threading
# time measures how long the run, each organization and each page download have taken.
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

# Seconds to wait for a website to answer or send its next piece of data.
TIMEOUT = 15

# Most seconds to spend downloading any one page in total. TIMEOUT alone isn't enough: a server
# that sends a few bytes every 14 seconds would never trip it.
PAGE_DEADLINE = 25

# Most seconds to spend on one organization (all its pages together). After this it's recorded as
# timed out and the run moves on, so one stuck website can't hold everything up.
ORG_DEADLINE = 90

# Save progress to boards.csv after this many organizations.
SAVE_EVERY = 500

# Bump this whenever a change would give different answers. Rows written by an older version are
# rechecked on the next run instead of waiting --recheck-days.
FINDER_VERSION = "3"

# Only the first 2 MB of a page are read; that always covers its links.
MAX_PAGE_BYTES = 2_000_000

# Order to check tiers in: biggest employers first, small ones last.
TIER_ORDER = ["A", "B", "funder", "C"]

# Words that mark a careers link, by link text or address. Earlier words are better matches.
CAREER_WORDS = ["career", "jobs", "job openings", "employment", "join our team", "join the team",
                "work with us", "work for us", "work here", "open positions",
                # "opportunities" alone also means partnerships or sponsorships, so only with a job word.
                "job opportunities", "career opportunities", "employment opportunities"]
# Link text that, word for word, means "jobs at this organization". These beat any partial match,
# so a footer "Careers" link wins over a "Career Pathways Program" link higher up the page.
EXACT_CAREER_TEXT = {"careers", "career", "jobs", "employment", "job openings", "current openings",
                     "open positions", "career opportunities", "job opportunities",
                     "employment opportunities", "work with us", "work for us", "join our team",
                     "join us", "join the team", "we're hiring", "now hiring", "careers at"}
# Links containing these are about volunteering, board service, or (common in this sector) job
# programs and services for the people an organization serves, not jobs at the organization.
NOT_CAREER_WORDS = ["volunteer", "board of directors", "board member", "internship-only",
                    "seeker", "program", "service", "training", "pathway", "workforce",
                    "career center", "career-center", "career development", "career-development",
                    "readiness", "resource", "student", "youth", "client", "find a job",
                    "job search", "job-search", "job fair", "job-fair", "career fair",
                    "partner", "sponsor", "bid", "rfp", "grant", "donat", "exhibit"]

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
# Patterns that start with a subdomain use (?<![a-z0-9-]) ("not right after a letter, digit or
# dash") and {1,63} (subdomains are at most 63 characters). Without those, a page with one long
# unbroken string, like minified code, makes the search retry from every character, and the time
# grows with the square of the length: one 2 MB page could freeze the whole run for hours.
# The pattern's groups capture the board ID. Matching ignores capitals.
# "{1}" in the board URL is replaced by the first captured group, "{2}" by the second, and so on.
ATS_PATTERNS: List[Tuple[str, str, str]] = [
    ("paylocity", r"recruiting\.paylocity\.com/recruiting/jobs/(?:all|list)/([0-9a-f-]{36})",
     "https://recruiting.paylocity.com/Recruiting/Jobs/All/{1}"),
    ("adp", r"workforcenow\.adp\.com/[^\"'\s<>]*?cid=([0-9a-f-]{36})",
     "https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html?cid={1}"),
    ("adp", r"myjobs\.adp\.com/([a-z0-9-]+)", "https://myjobs.adp.com/{1}"),
    ("ukg", r"recruiting2?\.ultipro\.com/([a-z0-9]{1,63})/jobboard/([0-9a-f-]{36})",
     "https://recruiting.ultipro.com/{1}/JobBoard/{2}"),
    ("ukg", r"(?<![a-z0-9-])([a-z0-9-]{1,63}\.rec\.pro\.ukg\.net)/([a-z0-9]+)/jobboard/([0-9a-f-]{36})",
     "https://{1}/{2}/JobBoard/{3}"),
    ("paycom", r"paycomonline\.net/v4/ats/web\.php/jobs\?clientkey=([a-z0-9]+)",
     "https://www.paycomonline.net/v4/ats/web.php/jobs?clientkey={1}"),
    ("paycor", r"recruitingbypaycor\.com/career/careerhome\.action\?clientid=([a-z0-9]+)",
     "https://recruitingbypaycor.com/career/CareerHome.action?clientId={1}"),
    ("dayforce", r"jobs\.dayforcehcm\.com/(?:[a-z]{2}-[a-z]{2}/)?([a-z0-9-]+)",
     "https://jobs.dayforcehcm.com/en-US/{1}"),
    ("workday", r"(?<![a-z0-9-])([a-z0-9-]{1,63})\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[a-z]{2}/)?([a-z0-9_-]+)",
     "https://{1}.{2}.myworkdayjobs.com/{3}"),
    ("icims", r"(?<![a-z0-9-])([a-z0-9-]{1,63})\.icims\.com", "https://{1}.icims.com/jobs"),
    ("taleo", r"(?<![a-z0-9-])([a-z0-9-]{1,63})\.taleo\.net", "https://{1}.taleo.net"),
    ("bamboohr", r"(?<![a-z0-9-])([a-z0-9-]{1,63})\.bamboohr\.com/(?:careers|jobs)", "https://{1}.bamboohr.com/careers"),
    ("applicantpro", r"(?<![a-z0-9-])([a-z0-9-]{1,63})\.applicantpro\.com", "https://{1}.applicantpro.com/jobs/"),
    ("jazzhr", r"(?<![a-z0-9-])([a-z0-9-]{1,63})\.applytojob\.com", "https://{1}.applytojob.com/apply"),
    ("isolved", r"(?<![a-z0-9-])([a-z0-9-]{1,63})\.isolvedhire\.com", "https://{1}.isolvedhire.com/jobs/"),
    ("clearcompany", r"(?<![a-z0-9-])([a-z0-9-]{1,63})\.clearcompany\.com/careers", "https://{1}.clearcompany.com/careers"),
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
    ("breezy", r"(?<![a-z0-9-])([a-z0-9-]{1,63})\.breezy\.hr", "https://{1}.breezy.hr"),
    ("recruitee", r"(?<![a-z0-9-])([a-z0-9-]{1,63})\.recruitee\.com", "https://{1}.recruitee.com"),
    ("neogov", r"governmentjobs\.com/careers/([a-z0-9]+)", "https://www.governmentjobs.com/careers/{1}"),
    # A single Paylocity job page names no board, but still tells us the system.
    ("paylocity", r"recruiting\.paylocity\.com/recruiting/jobs/details/(\d+)", ""),
]
# Board IDs that are really parts of the ATS's own website, not a customer's board.
BAD_IDS = {"www", "api", "embed", "careers", "jobs", "app", "help", "support", "static", "assets", "cdn"}

# Column order for boards.csv.
BOARD_COLUMNS = ["ein", "name", "state", "size_tier", "website", "careers_url", "ats_platform",
                 "board_id", "board_url", "method", "checked_at", "error", "finder_version"]

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
    # When this page's time is up, by the computer's steady clock.
    deadline = time.monotonic() + PAGE_DEADLINE
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        chunks: List[bytes] = []
        size = 0
        # Read in 64 KB pieces, stopping at MAX_PAGE_BYTES (giant pages are almost never careers
        # pages) or when the page deadline passes, whichever comes first.
        while size < MAX_PAGE_BYTES:
            if time.monotonic() > deadline:
                raise socket.timeout(f"page took over {PAGE_DEADLINE}s")
            piece = resp.read(65536)
            # An empty piece means the server has sent the whole page.
            if not piece:
                break
            chunks.append(piece)
            size += len(piece)
        # resp.geturl() is where we ended up, which matters for joining relative links.
        return resp.geturl(), b"".join(chunks).decode("utf-8", errors="replace")


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
    # Volunteer, board and client-program links never count.
    if any(bad in hay for bad in NOT_CAREER_WORDS):
        return 0
    # Link text that is exactly "Careers", "Jobs" and the like scores above any partial match.
    # (Only for careers links: on a careers page we're looking for "openings" links instead.)
    if words is CAREER_WORDS and re.sub(r"[^a-z' ]", "", text.lower()).strip() in EXACT_CAREER_TEXT:
        return 100
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
    row["finder_version"] = FINDER_VERSION
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


def timed_out_row(org: Dict[str, str], seconds: float) -> Dict[str, str]:
    """A boards.csv row for an organization that took longer than ORG_DEADLINE."""
    row = {k: "" for k in BOARD_COLUMNS}
    for k in ("ein", "name", "state", "size_tier", "website"):
        row[k] = org.get(k, "")
    row.update(checked_at=datetime.date.today().isoformat(), finder_version=FINDER_VERSION, ats_platform="error",
               error=f"timed out after {seconds:.0f}s")
    return row


def run_checks(todo: List[Dict[str, str]], workers: int, org_deadline: float,
               stop_at: Optional[float], on_result) -> int:
    """
    Check every organization in `todo`, `workers` at a time, calling on_result(row) for each.
    An organization that takes longer than `org_deadline` seconds is recorded as timed out and
    left behind; its thread is a "daemon" thread, which Python doesn't wait for when the script
    ends, so a stuck website can never keep the run going. No new checks start after `stop_at`
    (a time on time.monotonic()'s clock). Returns how many organizations were finished.
    """
    # Finished rows arrive here from the worker threads.
    done: "queue.Queue[Dict[str, str]]" = queue.Queue()
    # Organizations not started yet; reversed so pop() takes them in the original order.
    pending = list(reversed(todo))
    # Organizations being checked right now: EIN -> (start time, organization).
    in_flight: Dict[str, Tuple[float, Dict[str, str]]] = {}
    finished = 0

    def worker(org: Dict[str, str]) -> None:
        # Runs on its own thread: check one organization and hand back the row.
        done.put(check_org(org))

    while True:
        time_up = stop_at is not None and time.monotonic() > stop_at
        # Start new checks while there's room and time.
        while pending and len(in_flight) < workers and not time_up:
            org = pending.pop()
            in_flight[org["ein"]] = (time.monotonic(), org)
            threading.Thread(target=worker, args=(org,), daemon=True).start()
        # Nothing running and nothing more to start (or no time to start it): we're done.
        if not in_flight:
            break
        # Wait up to 1 second for a result, then go round again to check the deadlines.
        try:
            row = done.get(timeout=1)
            # Ignore a result that arrives after its organization was already marked timed out.
            if row["ein"] in in_flight:
                del in_flight[row["ein"]]
                on_result(row)
                finished += 1
        except queue.Empty:
            pass
        # Give up on any organization that has run past its deadline.
        now = time.monotonic()
        for ein, (began, org) in list(in_flight.items()):
            if now - began > org_deadline:
                del in_flight[ein]
                on_result(timed_out_row(org, now - began))
                finished += 1
    return finished


def is_fresh(row: Dict[str, str], today: datetime.date, recheck_days: int) -> bool:
    """True if this organization was checked recently enough to skip. Errors are retried after a week."""
    # Rows from an older version of the finder may be wrong, so they're never fresh.
    if row.get("finder_version") != FINDER_VERSION:
        return False
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
    parser.add_argument("--org-timeout", type=float, default=ORG_DEADLINE,
                        help="Seconds before giving up on one organization.")
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

    # Stop starting new checks once --max-minutes is reached.
    stop_at = started + args.max_minutes * 60 if args.max_minutes is not None else None
    # A one-item list so the function below can update the count (a plain number couldn't be changed from inside it).
    count = [0]

    def save(row: Dict[str, str]) -> None:
        # Store each finished row, and save the file and report progress every SAVE_EVERY rows.
        results[row["ein"]] = row
        count[0] += 1
        if count[0] % SAVE_EVERY == 0:
            write_boards(args.out, results)
            print(f"  checked {count[0]} of {len(todo)} ({(time.monotonic() - started) / 60:.0f} min)")

    checked = run_checks(todo, args.workers, args.org_timeout, stop_at, save)
    if checked < len(todo):
        print(f"Time limit of {args.max_minutes} minutes reached; saving and stopping.")

    write_boards(args.out, results)
    print_summary(results, checked, len(todo) - checked)
    return 0


if __name__ == "__main__":
    sys.exit(main())
