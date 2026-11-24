"""
build_orgs.py

Stage 1 of the nonprofit job-board finder: builds orgs.csv, the master list of nonprofit and
philanthropic organizations that might employ someone in impact, data and evaluation work.

What it does, in order:
  1. Downloads the IRS Exempt Organizations Business Master File (BMF), the IRS's list of every
     tax-exempt organization, and keeps 501(c)(3) organizations in the chosen issue areas
     (NTEE codes) that are big enough to have paid staff.
  2. Downloads the IRS indexes of e-filed Form 990 returns and finds each kept organization's
     most recent return.
  3. Downloads the monthly ZIP files of e-filed returns, reads only the returns we need, and
     pulls out each organization's website, employee count and mission statement.
  4. Writes orgs.csv (one row per organization) plus a cache of what it read from the returns,
     so the next run only reads returns it hasn't seen.

How to run (from the folder this file is in):
    python3 build_orgs.py                 # the full national build (runs on GitHub Actions)
    python3 build_orgs.py --states TX     # just Texas, for a quicker test run
    python3 build_orgs.py --skip-990      # BMF filter only, no websites

Runs on GitHub Actions (.github/workflows/build-orgs.yml), because the IRS site isn't reachable
from Claude's own network.

Only the Python standard library is used. Works on Python 3.8 and newer.
"""

# Lets us write type hints like Optional[str] in a way Python 3.8 accepts.
from __future__ import annotations

# argparse reads command-line options such as --states.
import argparse
# collections.Counter counts things for the summary (organizations per tier, per state).
import collections
# csv reads the IRS files and writes orgs.csv.
import csv
# datetime gives today's year, to pick which years of returns to read.
import datetime
# gzip compresses the return cache, which holds long mission statements.
import gzip
# io turns downloaded bytes into a text stream the csv module can read.
import io
# os builds file paths and deletes downloaded ZIPs once they're read.
import os
# re is regular expressions: pattern matching on text.
import re
# shutil copies a download to disk in chunks, so a large file never sits in memory all at once,
# and finds the system's "unzip" program.
import shutil
# subprocess runs "unzip" for ZIPs whose compression Python can't read (see extract_with_unzip).
import subprocess
# sys lets us print to the error stream for warnings.
import sys
# tempfile gives us a scratch folder for the large ZIP downloads.
import tempfile
# time lets us pause before retrying a failed download.
import time
# urllib makes the web requests; it ships with Python.
import urllib.error
import urllib.request
# xml.etree.ElementTree parses the XML tax returns.
import xml.etree.ElementTree as ET
# zipfile opens the IRS's monthly ZIPs of returns without unpacking them to disk.
import zipfile
# Type hints: they document what a function expects and returns. Python doesn't enforce them.
from typing import Dict, Iterable, Iterator, List, Optional, Set, Tuple


# ---------------------------------------------------------------------------
# SETTINGS: change these to widen or narrow the list.
# ---------------------------------------------------------------------------

# NTEE major groups to keep (the first letter of an organization's NTEE code):
#   B Education, E Health, F Mental health, J Employment, K Food and nutrition,
#   L Housing and shelter, O Youth development, P Human services,
#   S Community improvement, T Philanthropy and grantmaking, V Social science research.
INCLUDE_NTEE_MAJOR = set("BEFJKLOPSTV")

# Codes inside those groups to leave out, matched on the start of the code:
#   B40-B43 colleges and universities (huge employers with their own job systems),
#   E20-E24 hospitals.
EXCLUDE_NTEE_PREFIXES = ("B40", "B41", "B42", "B43", "E20", "E21", "E22", "E24")

# Many organizations have no NTEE code in the IRS file. True keeps them (tagged in the output),
# because a missing code says nothing about what the organization does.
INCLUDE_MISSING_NTEE = True

# IRS "foundation" codes that are never employers we want: 10 = churches, 12 = hospitals.
EXCLUDE_FOUNDATION_CODES = {10, 12}

# IRS foundation codes for private foundations (2, 3, 4). They go on the list as funders.
PRIVATE_FOUNDATION_CODES = {2, 3, 4}

# Size floor for public charities (everything that isn't a private foundation):
# kept if revenue is at least FINAL_REVENUE_FLOOR, or if the 990 shows at least
# FINAL_EMPLOYEE_FLOOR employees. Organizations below PRE_REVENUE_FLOOR are dropped before
# reading any returns, so we don't read hundreds of thousands of returns from tiny groups.
PRE_REVENUE_FLOOR = 500_000
FINAL_REVENUE_FLOOR = 1_000_000
FINAL_EMPLOYEE_FLOOR = 10

# Private foundations have small staffs and little "revenue", so we size them by assets instead.
# Foundations with less than this in assets rarely have staff at all.
FOUNDATION_ASSET_FLOOR = 10_000_000

# Size tiers for public charities, by revenue. They only set the order we build job boards in.
TIER_A_REVENUE = 20_000_000
TIER_B_REVENUE = 5_000_000

# Organizations whose latest IRS filing is older than this many years are dropped as inactive.
MAX_FILING_AGE_YEARS = 3

# The first year of e-filed returns the IRS publishes as XML. Indexes from this year on are read.
FIRST_EFILE_YEAR = 2019

# When an organization's latest return lists no website (or hasn't been posted yet), read up to
# this many of its older returns, newest first, until one does.
MAX_OLDER_RETURNS = 4

# Organizations to leave off the list, by name. They're read from the EXCLUDE_NAMES environment
# variable (in GitHub Actions, a repository secret of that name), so the names never appear in
# this public code. Separate several names with semicolons, e.g. "ACME FOUNDATION; EXAMPLE CORP".
# Each name is matched anywhere in the IRS name, ignoring capitals.
EXCLUDE_NAME_PATTERNS = [re.escape(name.strip().upper())
                         for name in os.environ.get("EXCLUDE_NAMES", "").split(";") if name.strip()]

# Where the IRS publishes its files.
BMF_REGION_URL = "https://www.irs.gov/pub/irs-soi/eo{n}.csv"          # n = 1 to 4, the whole U.S.
BMF_STATE_URL = "https://www.irs.gov/pub/irs-soi/eo_{state}.csv"      # state = two letters, lowercase
EFILE_BASE = "https://apps.irs.gov/pub/epostcard/990/xml/{year}/"
EFILE_INDEX_URL = EFILE_BASE + "index_{year}.csv"
EFILE_DOWNLOADS_PAGE = "https://www.irs.gov/charities-non-profits/form-990-series-downloads"

# Output files, saved next to this script.
HERE = os.path.dirname(os.path.abspath(__file__))
ORGS_CSV = os.path.join(HERE, "orgs.csv")
RETURN_CACHE = os.path.join(HERE, "return_cache.csv.gz")

# Column order for orgs.csv.
ORG_COLUMNS = [
    "ein", "name", "sort_name", "city", "state", "zip", "ntee_code", "org_kind", "size_tier",
    "revenue", "assets", "employees", "website", "tax_period", "return_form", "return_tax_year",
    "group_exemption", "notes",
]

# Column order for the return cache.
CACHE_COLUMNS = ["object_id", "ein", "form", "tax_year", "website", "employees", "mission"]

# A normal browser identity; some government sites refuse requests that don't look like one.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130 Safari/537.36"
)


# ---------------------------------------------------------------------------
# WEB REQUESTS
# ---------------------------------------------------------------------------

def download(url: str, dest: str, tries: int = 3) -> None:
    """Download `url` to the file `dest`, retrying up to `tries` times on network errors."""
    # Try the download up to `tries` times; IRS servers sometimes drop large transfers.
    for attempt in range(1, tries + 1):
        try:
            # Build the request with our browser identity.
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            # A long timeout, because the monthly ZIPs are large.
            with urllib.request.urlopen(req, timeout=300) as resp, open(dest, "wb") as out:
                # Copy in 1 MB chunks, so a multi-gigabyte file never sits in memory.
                shutil.copyfileobj(resp, out, length=1024 * 1024)
            # Success: stop trying.
            return
        except urllib.error.HTTPError:
            # A 404 or 403 won't fix itself on a retry, so pass it straight up.
            raise
        except (urllib.error.URLError, OSError) as err:
            # Network trouble: give up after the last try, otherwise wait and go again.
            if attempt == tries:
                raise
            print(f"  retrying {url} after error: {err}", file=sys.stderr)
            # Wait a little longer after each failure (10s, then 20s).
            time.sleep(10 * attempt)


def fetch_text(url: str) -> str:
    """Download a small page and return it as text."""
    # Same browser identity as download().
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as resp:
        # "replace" swaps any broken characters instead of crashing.
        return resp.read().decode("utf-8", errors="replace")


def url_exists(url: str) -> bool:
    """True if the server answers a HEAD request (headers only, no download) for `url`."""
    # method="HEAD" asks only whether the file is there, without downloading it.
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT}, method="HEAD")
    try:
        with urllib.request.urlopen(req, timeout=60):
            return True
    except (urllib.error.URLError, OSError):
        # Any error (404, timeout) means we treat it as not there.
        return False


# ---------------------------------------------------------------------------
# SMALL HELPERS
# ---------------------------------------------------------------------------

def to_int(value: Optional[str]) -> int:
    """Turn IRS number text ('1234', '-56', '', '15.0') into an int; blank or junk becomes 0."""
    try:
        # float() first, because some files write whole numbers as "15.0".
        return int(float((value or "").strip() or 0))
    except ValueError:
        # Anything that isn't a number counts as 0.
        return 0


def pad_ein(value: str) -> str:
    """EINs are 9 digits; some files drop leading zeros ('10202020' -> '010202020')."""
    # Keep only digits, then pad on the left with zeros to 9 characters.
    digits = re.sub(r"\D", "", value or "")
    return digits.zfill(9) if digits else ""


def clean_key(key: str) -> str:
    """Normalize a column name so 'Object ID', 'OBJECT_ID' and 'ObjectId' all become 'OBJECTID'."""
    # Drop anything that isn't a letter or digit (spaces, underscores, a stray byte-order mark).
    return re.sub(r"[^A-Za-z0-9]", "", key or "").upper()


def clean_website(raw: Optional[str]) -> str:
    """Tidy a website typed on a tax return ('WWW.EXAMPLE.ORG', 'N/A', 'http//x.org') into a URL or ''."""
    # Lowercase and remove spaces, which are never part of a real web address.
    text = re.sub(r"\s+", "", (raw or "").strip().lower())
    # Common "no website" answers, and anything without a dot, mean there's no site.
    if text in {"", "n/a", "na", "none", "no", "0", "nan", "null"} or "." not in text:
        return ""
    # Remove any scheme, including broken ones like "http//" or "https:/", so we can add a clean one.
    text = re.sub(r"^(https?)?:?/*", "", text)
    # Drop a trailing slash so the same site always looks the same.
    text = text.rstrip("/")
    # Email addresses sometimes land in the website box; those aren't websites.
    if "@" in text or not text:
        return ""
    # Put a standard scheme back on the front.
    return "https://" + text


def object_id_from_member(name: str) -> str:
    """Get the return's object ID from a file name inside a ZIP ('2025_TEOS_XML_01A/2025...01_public.xml')."""
    # Look only at the file name, not any folder it sits in.
    base = os.path.basename(name)
    # The object ID is the long run of digits at the start of the file name.
    match = re.match(r"(\d{10,})", base)
    return match.group(1) if match else ""


# ---------------------------------------------------------------------------
# STEP 1: THE BMF (the IRS list of every tax-exempt organization)
# ---------------------------------------------------------------------------

def read_csv_rows(path: str) -> Iterator[Dict[str, str]]:
    """Yield each row of an IRS CSV file as a dict whose keys are normalized with clean_key()."""
    # "utf-8-sig" removes the invisible byte-order mark some IRS files start with;
    # errors="replace" keeps one bad character from stopping the whole file.
    with open(path, encoding="utf-8-sig", errors="replace", newline="") as f:
        # DictReader turns each line into {column name: value}.
        for row in csv.DictReader(f):
            # Re-key the row with normalized column names.
            yield {clean_key(k): (v or "").strip() for k, v in row.items() if k is not None}


def ntee_ok(ntee: str) -> bool:
    """True if this NTEE code is in one of the issue areas we keep."""
    # Uppercase and strip, since codes come in as e.g. 'p20 '.
    code = (ntee or "").strip().upper()
    # No code at all: keep or drop depending on the setting.
    if not code:
        return INCLUDE_MISSING_NTEE
    # Drop the excluded subgroups (universities, hospitals).
    if code.startswith(EXCLUDE_NTEE_PREFIXES):
        return False
    # Keep if the first letter is one of the major groups we want.
    return code[0] in INCLUDE_NTEE_MAJOR


def org_kind(foundation_code: int) -> str:
    """Label an organization 'private_foundation' or 'public_charity' from its IRS foundation code."""
    return "private_foundation" if foundation_code in PRIVATE_FOUNDATION_CODES else "public_charity"


def excluded_by_name(name: str) -> bool:
    """True if the organization's name matches one of EXCLUDE_NAME_PATTERNS."""
    # Compare in capitals, the way the IRS writes names.
    upper = (name or "").upper()
    return any(re.search(pattern, upper) for pattern in EXCLUDE_NAME_PATTERNS)


def bmf_prefilter(row: Dict[str, str], min_tax_period: int) -> Optional[str]:
    """
    Decide whether a BMF row is worth looking at further.
    Returns None to keep it, or a short reason it was dropped (used for the summary counts).
    """
    # Only 501(c)(3) organizations (charities and foundations).
    if to_int(row.get("SUBSECTION")) != 3:
        return "not 501(c)(3)"
    # Leave out churches and hospitals.
    foundation = to_int(row.get("FOUNDATION"))
    if foundation in EXCLUDE_FOUNDATION_CODES:
        return "church or hospital"
    # Keep only our issue areas.
    if not ntee_ok(row.get("NTEECD", "")):
        return "outside issue areas"
    # Drop organizations that haven't filed recently (TAX_PERIOD looks like 202312).
    if to_int(row.get("TAXPERIOD")) < min_tax_period:
        return "no recent filing"
    # Size check: foundations by assets, everyone else by revenue.
    if org_kind(foundation) == "private_foundation":
        if to_int(row.get("ASSETAMT")) < FOUNDATION_ASSET_FLOOR:
            return "too small"
    elif to_int(row.get("REVENUEAMT")) < PRE_REVENUE_FLOOR:
        return "too small"
    # Leave off names listed in the EXCLUDE_NAMES secret.
    if excluded_by_name(row.get("NAME", "")):
        return "excluded by name"
    # Passed every check.
    return None


def size_tier(kind: str, revenue: int) -> str:
    """A, B or C for public charities by revenue; 'funder' for private foundations."""
    if kind == "private_foundation":
        return "funder"
    if revenue >= TIER_A_REVENUE:
        return "A"
    if revenue >= TIER_B_REVENUE:
        return "B"
    return "C"


def load_bmf(paths: Iterable[str], min_tax_period: int) -> Tuple[Dict[str, Dict[str, str]], collections.Counter]:
    """Read BMF files and return ({ein: row} for kept organizations, Counter of drop reasons)."""
    kept: Dict[str, Dict[str, str]] = {}
    dropped: collections.Counter = collections.Counter()
    for path in paths:
        for row in read_csv_rows(path):
            # Decide whether to keep this organization.
            reason = bmf_prefilter(row, min_tax_period)
            if reason:
                # Count why it was dropped, for the summary.
                dropped[reason] += 1
                continue
            # Store it under its padded EIN, the ID every other file joins on.
            kept[pad_ein(row.get("EIN", ""))] = row
    return kept, dropped


# ---------------------------------------------------------------------------
# STEP 2: THE E-FILE INDEXES (which return belongs to which organization)
# ---------------------------------------------------------------------------

def find_column(keys: Iterable[str], *fragments: str) -> Optional[str]:
    """Return the first normalized column name that contains any of `fragments`, or None."""
    for key in keys:
        if any(fragment in key for fragment in fragments):
            return key
    return None


def all_returns(indexes: Iterable[Tuple[int, str]], wanted: Set[str]) -> Dict[str, List[Dict[str, str]]]:
    """
    Read the IRS e-file index files and list every return for each wanted organization,
    newest first. `indexes` is (filing year, file path) pairs.
    Returns {ein: [{"object_id", "tax_period", "return_type", "batch", "index_year", "ein"}, ...]};
    "batch" is the ZIP's name when the index says which ZIP holds the return, otherwise ''.
    """
    found: Dict[str, Dict[str, Dict[str, str]]] = collections.defaultdict(dict)
    for year, path in indexes:
        cols = None
        for row in read_csv_rows(path):
            # Work out the column names once per file, since they vary a little by year.
            if cols is None:
                keys = list(row.keys())
                cols = {
                    "ein": find_column(keys, "EIN"),
                    "object": find_column(keys, "OBJECTID"),
                    "period": find_column(keys, "TAXPERIOD"),
                    "type": find_column(keys, "RETURNTYPE"),
                    "batch": find_column(keys, "BATCH"),
                }
            ein = pad_ein(row.get(cols["ein"] or "", ""))
            # Skip organizations we're not interested in.
            if ein not in wanted:
                continue
            # Only the 990 family we can read: 990, 990-EZ and 990-PF (not 990-T, a business-income form).
            rtype = re.sub(r"[^0-9A-Z]", "", row.get(cols["type"] or "", "").upper())
            if rtype not in {"990", "990EZ", "990PF"}:
                continue
            oid = re.sub(r"\D", "", row.get(cols["object"] or "", ""))
            if not oid:
                continue
            # Keyed by object ID, so a return listed in two indexes is kept once.
            found[ein][oid] = {
                "object_id": oid,
                "ein": ein,
                "tax_period": row.get(cols["period"] or "", ""),
                "return_type": rtype,
                "batch": row.get(cols["batch"] or "", "") if cols["batch"] else "",
                "index_year": str(year),
            }
    # Newest first: latest tax period, and for the same period the higher (later) object ID,
    # which is usually an amended return.
    return {ein: sorted(by_oid.values(), key=lambda c: (to_int(c["tax_period"]), int(c["object_id"])), reverse=True)
            for ein, by_oid in found.items()}


def older_targets(returns: Dict[str, List[Dict[str, str]]], cache: Dict[str, Dict[str, str]]) -> Dict[str, Dict[str, str]]:
    """
    Pick the older returns worth reading: for each organization with no website yet (because its
    latest return lists none, or hasn't been posted), its next MAX_OLDER_RETURNS returns.
    Returns {object_id: return} for the ones not already cached.
    """
    targets: Dict[str, Dict[str, str]] = {}
    for candidates in returns.values():
        # Already have a website from some return? Nothing more to read for this organization.
        if any(cache.get(c["object_id"], {}).get("website") for c in candidates):
            continue
        for c in candidates[1:1 + MAX_OLDER_RETURNS]:
            if c["object_id"] not in cache:
                targets[c["object_id"]] = c
    return targets


# ---------------------------------------------------------------------------
# STEP 3: THE RETURNS THEMSELVES (website, employees, mission)
# ---------------------------------------------------------------------------

def list_zip_urls(year: int) -> List[str]:
    """Find the URLs of all monthly e-file ZIPs for `year`."""
    urls: List[str] = []
    try:
        # First choice: read the IRS downloads page, which links every ZIP.
        page = fetch_text(EFILE_DOWNLOADS_PAGE)
        # Match full or site-relative links to this year's ZIPs.
        pattern = r"(?:https://apps\.irs\.gov)?/pub/epostcard/990/xml/%d/[^\"'\s<>]+?\.zip" % year
        for found in re.findall(pattern, page):
            # Add the host to relative links.
            url = found if found.startswith("http") else "https://apps.irs.gov" + found
            if url not in urls:
                urls.append(url)
    except (urllib.error.URLError, OSError) as err:
        print(f"  couldn't read the downloads page ({err}); guessing ZIP names instead", file=sys.stderr)
    if urls:
        return urls
    # Fallback: the IRS names them like 2025_TEOS_XML_01A.zip; check months 01-12, parts A-F.
    for month in range(1, 13):
        for part in "ABCDEF":
            url = EFILE_BASE.format(year=year) + f"{year}_TEOS_XML_{month:02d}{part}.zip"
            if url_exists(url):
                urls.append(url)
            else:
                # Parts run A, B, C... with no gaps, so the first missing part ends that month.
                break
    return urls


def parse_return_xml(data: bytes) -> Dict[str, str]:
    """Pull form type, tax year, website, employee count and mission out of one return's XML."""
    # Parse the whole return (they're small, usually under a megabyte).
    root = ET.fromstring(data)
    found: Dict[str, str] = {}
    # The tags we want, by their name without the IRS namespace prefix.
    wanted = {
        "ReturnTypeCd": "form",
        "TaxYr": "tax_year",
        "WebsiteAddressTxt": "website",
        "TotalEmployeeCnt": "employees",
        "MissionDesc": "mission",
        "ActivityOrMissionDesc": "mission_alt",
        "PrimaryExemptPurposeTxt": "mission_ez",
    }
    for elem in root.iter():
        # Tags look like "{http://www.irs.gov/efile}WebsiteAddressTxt"; keep the part after "}".
        local = elem.tag.rsplit("}", 1)[-1]
        field = wanted.get(local)
        # Keep the first value we see for each field.
        if field and field not in found and (elem.text or "").strip():
            found[field] = elem.text.strip()
    # Mission: the full 990's mission line first, then its one-line summary, then the 990-EZ purpose.
    mission = found.get("mission") or found.get("mission_alt") or found.get("mission_ez") or ""
    return {
        "form": found.get("form", ""),
        "tax_year": found.get("tax_year", ""),
        "website": clean_website(found.get("website")),
        "employees": str(to_int(found.get("employees"))) if found.get("employees") else "",
        # Collapse runs of spaces and cap the length so the cache stays small.
        "mission": re.sub(r"\s+", " ", mission)[:500],
    }


def store_return(results: Dict[str, Dict[str, str]], oid: str, ein: str, data: bytes) -> None:
    """Parse one return's XML and add it to `results` under its object ID."""
    try:
        parsed = parse_return_xml(data)
    except ET.ParseError as err:
        # One broken file shouldn't stop the build.
        print(f"  skipping unreadable return {oid}: {err}", file=sys.stderr)
        return
    # Remember which organization the return belongs to.
    parsed["object_id"] = oid
    parsed["ein"] = ein
    results[oid] = parsed


def extract_with_unzip(path: str, members: List[str], batch_size: int = 500) -> Iterator[Tuple[str, bytes]]:
    """
    Pull `members` out of the ZIP at `path` with the system's "unzip" program and yield
    (member name, file bytes) for each.
    Needed because some IRS ZIPs use "Deflate64" compression, which Python's zipfile can't read
    but unzip can. Works in batches of `batch_size` names, so no command line gets too long and
    only one batch's files sit on disk at a time.
    """
    # Find the unzip program; GitHub's Linux computers have it installed.
    unzip = shutil.which("unzip")
    if not unzip:
        print("  'unzip' isn't installed, so this ZIP's returns are skipped", file=sys.stderr)
        return
    for i in range(0, len(members), batch_size):
        # The next slice of names, e.g. members 0-499, then 500-999.
        batch = members[i:i + batch_size]
        # A scratch folder for this batch, deleted automatically at the end of the "with".
        with tempfile.TemporaryDirectory() as outdir:
            # -q quiet, -o overwrite without asking, -d where to put the files.
            # The names are passed as-is; unzip would treat [ ] * ? in them as wildcards,
            # but IRS names are only digits, letters, "_", "/" and ".".
            subprocess.run([unzip, "-q", "-o", path, *batch, "-d", outdir], check=False)
            for name in batch:
                # unzip recreates the ZIP's folders, so the file is at outdir/<name>.
                full = os.path.join(outdir, name)
                if os.path.isfile(full):
                    with open(full, "rb") as f:
                        yield name, f.read()


def read_zip(path: str, needed: Dict[str, str]) -> Dict[str, Dict[str, str]]:
    """
    Read the returns we need from one downloaded ZIP.
    `needed` maps object ID -> EIN. Returns {object_id: parsed fields}.
    """
    results: Dict[str, Dict[str, str]] = {}
    # Members Python couldn't decompress; we hand these to unzip afterward.
    fallback: List[str] = []
    with zipfile.ZipFile(path) as zf:
        for info in zf.infolist():
            # Skip folders and anything that isn't one of our returns.
            oid = object_id_from_member(info.filename)
            if not oid or oid not in needed:
                continue
            try:
                data = zf.read(info)
            except NotImplementedError:
                # "That compression method is not supported": usually Deflate64.
                fallback.append(info.filename)
                continue
            store_return(results, oid, needed[oid], data)
    if fallback:
        print(f"    {len(fallback)} returns use compression Python can't read; extracting with unzip")
        for name, data in extract_with_unzip(path, fallback):
            oid = object_id_from_member(name)
            store_return(results, oid, needed[oid], data)
    return results


def load_cache(path: str) -> Dict[str, Dict[str, str]]:
    """Read the return cache from earlier runs: {object_id: fields}. Missing file = empty cache."""
    if not os.path.exists(path):
        return {}
    # "rt" opens the gzip file as text.
    with gzip.open(path, "rt", encoding="utf-8", newline="") as f:
        return {row["object_id"]: row for row in csv.DictReader(f)}


def save_cache(path: str, cache: Dict[str, Dict[str, str]]) -> None:
    """Write the return cache back out, sorted so the file changes as little as possible between runs."""
    with gzip.open(path, "wt", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CACHE_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for oid in sorted(cache):
            writer.writerow(cache[oid])


def fetch_returns(targets: Dict[str, Dict[str, str]], cache: Dict[str, Dict[str, str]],
                  workdir: str, max_zips: Optional[int]) -> int:
    """
    Read the returns in `targets` ({object_id: return}) that aren't already in `cache`, adding
    them to `cache`. Returns how many ZIPs were downloaded.
    """
    # Object IDs we still need, mapped to their organization's EIN.
    needed = {oid: r["ein"] for oid, r in targets.items() if oid not in cache}
    print(f"Returns to read: {len(needed)} ({len(targets) - len(needed)} already cached)")
    if not needed:
        return 0
    # Only the filing years that hold the returns we need, newest first.
    years = sorted({int(targets[oid]["index_year"]) for oid in needed}, reverse=True)
    urls = [url for year in years for url in list_zip_urls(year)]
    # The ZIP names without ".zip", e.g. "2025_TEOS_XML_01A".
    zip_names = {os.path.splitext(os.path.basename(u))[0] for u in urls}
    # If the index said which ZIP holds each return, only those ZIPs are downloaded...
    batches = {targets[oid]["batch"] for oid in needed if targets[oid]["batch"]}
    # Older indexes may not name the ZIP. If any needed return lacks one, read every ZIP for its
    # year, since skipping by name could miss it.
    if any(not targets[oid]["batch"] for oid in needed):
        batches = set()
    # ...but only if those names really match the ZIP names; otherwise we read every ZIP to be safe.
    if batches and not (batches & zip_names):
        print("  index batch names don't match the ZIP names; reading every ZIP", file=sys.stderr)
        batches = set()
    zips_done = 0
    for url in urls:
        # Stop early once everything has been found, or when a test run hits its limit.
        if not needed or (max_zips is not None and zips_done >= max_zips):
            return zips_done
        name = os.path.basename(url)
        # Skip ZIPs the index tells us hold none of our returns.
        if batches and os.path.splitext(name)[0] not in batches:
            continue
        dest = os.path.join(workdir, name)
        print(f"  downloading {name} ...")
        try:
            download(url, dest)
        except (urllib.error.URLError, OSError) as err:
            print(f"  couldn't download {name}: {err}", file=sys.stderr)
            continue
        try:
            found = read_zip(dest, needed)
        except zipfile.BadZipFile as err:
            print(f"  {name} isn't a readable ZIP: {err}", file=sys.stderr)
            found = {}
        finally:
            # Delete the ZIP right away; together they're far bigger than the runner's disk.
            os.remove(dest)
        # Add what we found to the cache and stop looking for those returns.
        cache.update(found)
        for oid in found:
            needed.pop(oid, None)
        zips_done += 1
        print(f"    read {len(found)} returns; {len(needed)} still needed")
    return zips_done


# ---------------------------------------------------------------------------
# STEP 4: PUT IT TOGETHER AND WRITE orgs.csv
# ---------------------------------------------------------------------------

def build_rows(bmf: Dict[str, Dict[str, str]], returns: Dict[str, List[Dict[str, str]]],
               cache: Dict[str, Dict[str, str]]) -> Tuple[List[Dict[str, str]], int]:
    """Combine the BMF with what the returns said; apply the final size floor. Returns (rows, dropped)."""
    rows: List[Dict[str, str]] = []
    dropped_small = 0
    for ein, b in bmf.items():
        kind = org_kind(to_int(b.get("FOUNDATION")))
        revenue = to_int(b.get("REVENUEAMT"))
        # Every return we read for this organization, newest first.
        candidates = returns.get(ein, [])
        infos = [cache[c["object_id"]] for c in candidates if c["object_id"] in cache]
        # Staff count, form and tax year come from the newest return we could read...
        info = infos[0] if infos else {}
        employees = info.get("employees", "")
        # ...and the website from the newest return that lists one.
        site_info = next((i for i in infos if i.get("website")), {})
        # Final floor for public charities: enough revenue, or enough staff on the 990.
        if kind == "public_charity" and revenue < FINAL_REVENUE_FLOOR and to_int(employees) < FINAL_EMPLOYEE_FLOOR:
            dropped_small += 1
            continue
        notes = []
        if not b.get("NTEECD"):
            notes.append("no NTEE code in IRS file")
        if not info:
            notes.append("no e-filed return read")
        elif candidates[0]["object_id"] not in cache:
            # The index lists a newer return the IRS hasn't posted yet.
            notes.append(f"details from {info.get('tax_year', 'an older')} return; latest not yet posted")
        if site_info and site_info is not info:
            notes.append(f"website from {site_info.get('tax_year', 'an older')} return")
        rows.append({
            "ein": ein,
            "name": b.get("NAME", ""),
            "sort_name": b.get("SORTNAME", ""),
            "city": b.get("CITY", ""),
            "state": b.get("STATE", ""),
            # Keep the 5-digit ZIP; the IRS sometimes adds the 4-digit extension.
            "zip": b.get("ZIP", "")[:5],
            "ntee_code": b.get("NTEECD", ""),
            "org_kind": kind,
            "size_tier": size_tier(kind, revenue),
            "revenue": str(revenue),
            "assets": str(to_int(b.get("ASSETAMT"))),
            "employees": employees,
            "website": site_info.get("website", ""),
            "tax_period": b.get("TAXPERIOD", ""),
            "return_form": info.get("form", ""),
            "return_tax_year": info.get("tax_year", ""),
            "group_exemption": b.get("GROUP", "").lstrip("0"),
            "notes": "; ".join(notes),
        })
    # Sort by state, then tier (A, B, C, funder), then biggest first.
    tier_order = {"A": 0, "B": 1, "C": 2, "funder": 3}
    rows.sort(key=lambda r: (r["state"], tier_order[r["size_tier"]], -int(r["revenue"]), r["name"]))
    return rows, dropped_small


def write_rows(path: str, rows: List[Dict[str, str]]) -> None:
    """Write orgs.csv."""
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=ORG_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def print_summary(rows: List[Dict[str, str]], dropped: collections.Counter, dropped_small: int,
                  zips: int) -> None:
    """Print counts for the run's summary page."""
    print()
    print(f"Organizations kept: {len(rows)}")
    tiers = collections.Counter(r["size_tier"] for r in rows)
    print("By tier: " + ", ".join(f"{t} {tiers.get(t, 0)}" for t in ("A", "B", "C", "funder")))
    with_site = sum(1 for r in rows if r["website"])
    if rows:
        print(f"With a website: {with_site} ({100 * with_site // len(rows)}%)")
    print("Top 10 states: " + ", ".join(f"{s} {n}" for s, n in collections.Counter(r["state"] for r in rows).most_common(10)))
    texas = [r for r in rows if r["state"] == "TX"]
    print(f"Texas: {len(texas)}")
    print(f"Dropped at the final size check: {dropped_small}")
    print("Dropped from the BMF: " + ", ".join(f"{k} {v}" for k, v in dropped.most_common()))
    print(f"Return ZIPs downloaded this run: {zips}")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Build orgs.csv from IRS data.")
    parser.add_argument("--states", default="", help="Comma-separated state codes, e.g. TX,OK. Default: all.")
    parser.add_argument("--years", default="", help="Filing years of returns to read, e.g. 2026,2025. Default: every year since 2019.")
    parser.add_argument("--skip-990", action="store_true", help="Skip the returns; no websites or employee counts.")
    parser.add_argument("--max-zips", type=int, default=None, help="Stop after this many return ZIPs (for test runs).")
    parser.add_argument("--out", default=ORGS_CSV, help="Where to write orgs.csv.")
    parser.add_argument("--cache", default=RETURN_CACHE, help="Where to keep the return cache.")
    args = parser.parse_args(argv)

    this_year = datetime.date.today().year
    # Every year of e-filed returns, newest first. Older years supply websites for organizations
    # whose latest return lists none.
    years = [int(y) for y in args.years.split(",") if y.strip()] or list(range(this_year, FIRST_EFILE_YEAR - 1, -1))
    # Oldest tax period that still counts as recent, e.g. 202301 when the setting is 3 years.
    min_tax_period = (this_year - MAX_FILING_AGE_YEARS) * 100 + 1

    with tempfile.TemporaryDirectory() as workdir:
        # STEP 1: download and filter the BMF.
        states = [s.strip().lower() for s in args.states.split(",") if s.strip()]
        urls = [BMF_STATE_URL.format(state=s) for s in states] or [BMF_REGION_URL.format(n=n) for n in range(1, 5)]
        paths = []
        for url in urls:
            dest = os.path.join(workdir, os.path.basename(url))
            print(f"Downloading {url}")
            download(url, dest)
            paths.append(dest)
        bmf, dropped = load_bmf(paths, min_tax_period)
        print(f"BMF organizations passing the first filter: {len(bmf)}")

        returns: Dict[str, List[Dict[str, str]]] = {}
        cache = load_cache(args.cache)
        zips = 0
        if not args.skip_990:
            # STEP 2: the e-file indexes.
            index_paths: List[Tuple[int, str]] = []
            for year in years:
                dest = os.path.join(workdir, f"index_{year}.csv")
                print(f"Downloading the {year} e-file index")
                try:
                    download(EFILE_INDEX_URL.format(year=year), dest)
                    index_paths.append((year, dest))
                except (urllib.error.URLError, OSError) as err:
                    print(f"  couldn't get the {year} index: {err}", file=sys.stderr)
            returns = all_returns(index_paths, set(bmf))
            print(f"Organizations with an e-filed return since {years[-1]}: {len(returns)}")
            # STEP 3: read the returns (only ones not already cached), in two rounds. "finally"
            # saves the cache even if a round crashes, so the next run doesn't repeat finished work.
            try:
                # Round 1: each organization's latest return.
                latest = {c[0]["object_id"]: c[0] for c in returns.values()}
                print("Round 1: latest returns")
                zips = fetch_returns(latest, cache, workdir, args.max_zips)
                # Round 2: older returns, only for organizations still without a website.
                older = older_targets(returns, cache)
                print(f"Round 2: older returns for organizations still without a website")
                zips += fetch_returns(older, cache, workdir, args.max_zips)
            finally:
                save_cache(args.cache, cache)

    # STEP 4: combine and write.
    rows, dropped_small = build_rows(bmf, returns, cache)
    write_rows(args.out, rows)
    print_summary(rows, dropped, dropped_small, zips)
    return 0


if __name__ == "__main__":
    sys.exit(main())
