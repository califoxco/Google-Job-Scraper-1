# job-google-scrape

A small, local Google scraper for finding fresh job leads on ATS job boards
(Greenhouse, Lever, Ashby, Workday, …). It runs a configurable Google search
per ATS site, dedupes the results into a SQLite database, and gives you a web
UI to tune the search and browse leads. Built to be driven by a scheduled task
that runs every few hours.

```
keywords + freshness + ATS sites  ->  Google search (ScrapingBee | Playwright)  ->  SQLite leads
                          (config UI)                                          (deduped by URL)
```

## Fetch providers

Two interchangeable backends, picked in the UI or via `"provider"` in
`config.json`:

- **`scrapingbee`** (recommended) — calls the [ScrapingBee Google Search
  API](https://www.scrapingbee.com/documentation/google/), which uses managed
  proxies and returns clean structured JSON. **Never gets blocked**, so it's the
  right choice for an unattended schedule. Needs an API key (set
  `scrapingbee_api_key` in `config.json`, or the `SCRAPINGBEE_API_KEY` env var,
  which takes precedence). Each query spends ScrapingBee credits.
- **`playwright`** — drives a **real headless Chromium** locally (no API cost).
  Uses a **persistent browser profile** (`.browser-profile/`) and randomized
  delays. Free, but scraping `google.com/search` violates Google's ToS and a
  single home IP **will** hit CAPTCHAs on a schedule. If blocked, solve one
  challenge by hand: `python -m jobscrape.warmup` (opens a real window; solve it,
  close it, and the saved session unblocks later headless runs).

## Setup

Requires Python 3.10+.

```powershell
cd C:\Users\alex7\Projects\job-google-scrape
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
playwright install chromium      # only needed for the playwright provider
```

Then set your ScrapingBee key — either in the UI's **Fetch provider** panel
(saved to `config.json`) or as an env var:

```powershell
$env:SCRAPINGBEE_API_KEY = "your-key"
```

## Configure (the UI)

```powershell
.\ui.bat            # or:  python -m jobscrape.app
```

Open http://127.0.0.1:5050 and set:

> Port 5050 is the default (5000 is often taken on Windows). Change it with
> `$env:JGS_PORT="8765"` before launching.

- **Keywords** — add/remove terms; tick *exact* to wrap a term in quotes
  (`"Software Engineer"`). Loose terms (e.g. `remote`) are added unquoted.
- **Freshness** — Past week / 72 hours / 24 hours (maps to Google's
  `tbs=qdr:w` / `qdr:d3` / `qdr:d`).
- **ATS sites** — toggle Greenhouse, Lever, Ashby, etc., or add a custom
  domain. One Google query is run per selected site.
- **Fetch provider** — ScrapingBee (recommended) or Playwright, plus the
  ScrapingBee API key.
- **Results database** — where the SQLite file is written.
- **Run options** — max results per query; headless on/off and inter-query
  delays (Playwright provider only).

The page shows a live preview of the exact Google URLs it will hit, and lets
you **Save & run now** to test immediately. Collected leads appear in the
*Recent leads* table (mark them new / applied / hidden).

The default config reproduces your reference search:

```
"Software Engineer" "San Francisco" site:greenhouse.io remote    (tbs=qdr:w)
```

Config is saved to `config.json`. The scraper reads the same file.

## Run from the command line

```powershell
.\.venv\Scripts\python.exe -m jobscrape.run
```

Prints a per-query log and a summary (provider / queries / found / new /
blocked). Exit code `2` means every query failed or was blocked.

### Playwright got blocked? Solve a CAPTCHA once

Only relevant to the `playwright` provider (ScrapingBee never blocks). Run:

```powershell
.\.venv\Scripts\python.exe -m jobscrape.warmup
```

A real Chromium window opens; solve the CAPTCHA / accept consent, then close it.
The session is saved to `.browser-profile/` and reused by later headless runs.

## Schedule it (Windows Task Scheduler)

`cron` doesn't exist on Windows — use Task Scheduler. `run.bat` handles paths
and logging (to `results\run.log`). To run every 3 hours:

```powershell
schtasks /Create /SC HOURLY /MO 3 /TN "JobGoogleScrape" ^
  /TR "C:\Users\alex7\Projects\job-google-scrape\run.bat" /F
```

Manage it later:

```powershell
schtasks /Run    /TN "JobGoogleScrape"     # run now
schtasks /Query  /TN "JobGoogleScrape"     # status
schtasks /Delete /TN "JobGoogleScrape" /F  # remove
```

For unattended headless runs, leave **Headless** on. (On a real `cron` host
you'd instead add a crontab line pointing at `python -m jobscrape.run`.)

## Layout

```
jobscrape/
  config.py      defaults, load/save config.json, freshness + ATS constants
  query.py       build Google search strings / URLs from the config
  scrapingbee.py ScrapingBee Google Search API backend
  scraper.py     provider dispatch + Playwright backend, parsing, storage loop
  warmup.py      headed browser to solve a CAPTCHA once (Playwright provider)
  storage.py     SQLite store, dedupe-by-URL, runs log
  run.py         CLI entry point (what the schedule runs)
  app.py         Flask config UI
  templates/index.html
run.bat          scheduled-task entry point (logs to results/run.log)
ui.bat           launch the config UI
config.example.json
```

## Querying your leads

It's plain SQLite:

```powershell
.\.venv\Scripts\python.exe -c "import sqlite3,sys; c=sqlite3.connect('results/jobs.db'); [print(r) for r in c.execute('select first_seen,ats,title,url from jobs order by first_seen desc limit 20')]"
```

## Notes / limits

- Google's result HTML changes periodically; if parsing returns 0 links on a
  page that clearly has results, the selectors in `scraper.py` (`RESULT_JS`)
  may need a refresh.
- `tbs=qdr:d3` (72h) relies on Google's numeric date suffix; if a future Google
  change drops it, fall back to `qdr:d` (24h) or `qdr:w` (week).
- This is a single-user local tool — no auth on the UI; bind stays on
  `127.0.0.1`.
