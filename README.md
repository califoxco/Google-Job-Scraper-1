# job-google-scrape

A small, local Google scraper for finding fresh job leads on ATS job boards
(Greenhouse, Lever, Ashby, Workday, …). It runs a configurable Google search
per ATS site, dedupes the results into a SQLite database, and gives you a web
UI to tune the search and browse leads. Built to be driven by a scheduled task
that runs every few hours.

```
keywords + freshness + ATS sites  ->  Google search (headless Chromium)  ->  SQLite leads
                          (config UI)                                  (deduped by URL)
```

## ⚠️ Read this first

Scraping `google.com/search` is against Google's Terms of Service, and Google
actively detects automated traffic. To survive an unattended schedule this tool:

- drives a **real headless Chromium** (Playwright), not raw HTTP;
- uses a **persistent browser profile** (`.browser-profile/`) so cookies — and
  any CAPTCHA you solve once in headed mode — carry over to later runs;
- adds **randomized human-like delays** between queries.

Even so, expect occasional CAPTCHAs from a single home IP. Keep the query
volume low (a few ATS sites, every few hours). If you want zero blocking, the
clean alternative is Google's official **Custom Search JSON API** — ask and it
can be added as an optional backend.

## Setup

Requires Python 3.10+.

```powershell
cd C:\Users\alex7\Projects\job-google-scrape
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
playwright install chromium
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
- **Results database** — where the SQLite file is written.
- **Run options** — headless on/off, max results per query, inter-query delays.

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

Prints a per-query log and a summary (queries / found / new / blocked). Exit
code `2` means every query was blocked.

### First-run tip — solve a CAPTCHA once

If runs get blocked, uncheck **Headless** in the UI (or set `"headless": false`
in `config.json`) and run once. A Chromium window opens; solve any CAPTCHA /
consent prompt. It's saved to `.browser-profile/`, then switch headless back on.

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
  query.py       build Google search URLs from the config
  scraper.py     Playwright scraping, consent/CAPTCHA handling, parsing
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
