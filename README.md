# nonprofit-jobs

Finds job boards at nonprofit and philanthropic organizations for impact, data and evaluation
roles, built in stages:

1. **Organization list** (`build_orgs.py`, this stage): every 501(c)(3) in the chosen issue areas
   that's big enough to have paid staff, with its website from its latest e-filed Form 990.
2. **Job boards** (`find_boards.py`): each organization's careers page and the hiring system
   behind it (Paylocity, ADP, UKG, Workday, Greenhouse and others).
3. **Jobs** (later): pull open roles from every board and rank them.

## Stage 1: `build_orgs.py`

| Step | Source | What it adds |
| --- | --- | --- |
| 1 | IRS Exempt Organizations Business Master File | Every tax-exempt organization; filtered to 501(c)(3)s in the chosen NTEE codes, filed in the last 3 years, above the size floor |
| 2 | IRS e-file indexes | Each organization's most recent e-filed 990, 990-EZ or 990-PF |
| 3 | IRS e-file XML returns | Website, employee count, mission statement |

**Filters** (all in the SETTINGS block at the top of the script):
- Issue areas: NTEE groups B, E, F, J, K, L, O, P, S, T and V, minus universities (B40-B43) and
  hospitals (E20-E24). Organizations with no NTEE code are kept and noted.
- Size: public charities with $1M+ revenue, or 10+ employees on their 990. Private foundations
  with $10M+ in assets (they size by assets, not revenue).
- Left out: churches, hospitals, and any names listed in the `EXCLUDE_NAMES` secret
  (semicolon-separated; set under Settings → Secrets and variables → Actions).

**Output: `orgs.csv`**, one row per organization: EIN, name, city, state, ZIP, NTEE code,
public charity or private foundation, size tier (A $20M+, B $5M-20M, C under $5M, or funder),
revenue, assets, employees, website, and notes.

## Running it

On GitHub: **Actions** tab → **Build organization list** → **Run workflow**. Leave the boxes blank
for the whole U.S., or enter `TX` and `2` for a quick Texas test. The result lands on the branch
`results/orgs-main`: open `orgs.csv` there, or download it.

Locally (the IRS site must be reachable):

```
python3 build_orgs.py --states TX
```

Tests (sample data, no internet):

```
python3 -m unittest discover -s tests -v
```

## Stage 1b: `find_websites.py`

For U.S. organizations whose tax returns listed no website:

1. **Wikidata**, the free database behind Wikipedia: one query returns every organization recorded
   there with both a tax ID (EIN) and an official website.
2. **Domain guesses** from the name, such as `springfieldfoodbank.org`, `springfield-food-bank.org`,
   then `.com`. A guess counts only if the site's title or main heading names the organization,
   and for one- or two-word names the page also gives its city or ZIP code. Parked and for-sale
   domains are rejected.

**Output: `websites.csv`**: the website found (or blank) and its source for each organization.
The job-board finder uses these websites when `orgs.csv` has none.

Runs daily at 05:47 UTC and from the **Find websites** button in the Actions tab. Organizations with
nothing found are tried again after 90 days. Results land on the branch `results/websites-main`.

## Stage 2: `find_boards.py`

For each organization in `orgs.csv` with a website, biggest tiers first:

1. Reads the homepage, retrying with and without `www.` and over http or https if it fails, and
   without the certificate check if the site's certificate is broken. Looks there for a link to,
   or an embed from, a known applicant tracking system (ATS).
2. Otherwise looks for a careers page, in this order: up to three careers links on the homepage;
   common addresses (`/careers`, `/jobs`, `/join-our-team` and others); the site's sitemap.
   A guessed address only counts if its title or heading is about jobs.
3. On each careers page, follows up to three "current openings" links.

Each organization gets 60 seconds of searching. An organization is checked again when its website
in `orgs.csv` changes.

**Output: `boards.csv`**: careers page, ATS name, board ID and a clean board URL for each
organization. `site_page` means jobs are posted on the organization's own site; `not_found` and
`error` mean nothing was found or the site couldn't be reached.

Runs daily and from the **Find job boards** button in the Actions tab. Each run stops
at 4 hours and saves its progress; organizations are rechecked every 30 days. Results land on the
branch `results/boards-main`.

## Stage 2b: `reverse_boards.py`

Finds boards the website crawl misses by starting from the hiring systems. It reads the slices of
Common Crawl's public index that hold Paylocity, ADP, Paycom, UKG, Workday, iCIMS, BambooHR,
ApplicantPro, JazzHR, isolved, Dayforce, Paycor, Greenhouse, Lever and Ashby board addresses,
visits each board for the organization name it shows, and matches that name to `orgs.csv`
(names shared by several organizations are left unmatched rather than guessed).

**Outputs:** `reverse_boards.csv` (every board found and its match) and **`job_boards.csv`**, the
combined list: one row per organization per board or careers page, with `source` set to
`website crawl`, `reverse search` or `both`.

Runs daily after the website crawl. Results land on the branch `results/reverse-main`.
