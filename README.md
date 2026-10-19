# job-google-scrape

A local job-lead pipeline for your job search. It runs crafted Google searches
against ATS job boards (Greenhouse, Lever, Ashby, Workday, …) on a schedule,
dedupes results into SQLite, and gives you a web UI that is both the **scraper
config** and a full **application tracker** — check off jobs as you apply,
dismiss dead links, take notes, and export your applied list to Excel.

It also has a **delegation mode**: a stripped-down, password-protected
assistant view you can expose through an ngrok tunnel so a virtual assistant
(or a friend) can apply to jobs on your behalf — with their applications
tracked separately from yours, so you know exactly who applied to what.

```
keyword sets × sources  →  Google search (ScrapingBee | Playwright)  →  SQLite (deduped by URL)
        (config UI)                                                        ↓
                                             tracker UI (you) + assistant UI (your VA) + Excel export
```

## Features

- **Crafted source queries, not bare domains** — modeled on
  [briansjobsearch.com](https://briansjobsearch.com). Simple ATS sources are
  `site:<domain>`; composites use OR-groups and wildcards, e.g. *Careers pages
  (any company)* → `(site:careers.* OR site:*/careers/* OR site:*/career/*)`,
  which surfaces postings on companies' own sites that no ATS search finds.
- **Multiple keyword sets** — e.g. *Software Engineer* / *Full Stack* /
  *Frontend*, each sharing location terms. Every set runs against every
  selected source.
- **Dedupe by URL** — re-seeing a posting bumps `last_seen`/`seen_count`
  instead of creating a duplicate, so a scheduled scrape stays clean.
- **Application tracker** — ✓ applied (auto-timestamps), ✕ not-a-real-link,
  status dropdown (new / applied / interviewing / offer / rejected), per-job
  notes, search/sort/filters.
- **Excel export** — one click downloads your applied positions
  (date, company, title, source, status, URL, notes, applied-by).
- **LLM title matching (local, free)** — after each scrape, a local
  [Ollama](https://ollama.com) model judges every new lead against the roles
  you're hunting; poor matches are hidden by default. No API key, no cost.
- **Assistant mode** — a minimal `/assistant` view behind a shared password;
  applications made there are stamped `applied_by = va` for billing/attribution.
- **Two fetch providers** — ScrapingBee API (never blocked) or local headless
  Playwright (free, but CAPTCHAs happen).

## Quick start

Requires Python 3.10+ (Windows-first; the core app is cross-platform).

```powershell
git clone https://github.com/califoxco/Google-Job-Scraper-1.git
cd Google-Job-Scraper-1
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
playwright install chromium      # only needed for the playwright provider

python -m jobscrape.app          # or: .\ui.bat
```

Open http://127.0.0.1:5050 — a default `config.json` is created on first run
(see `config.example.json` for the full shape). Configure your searches, hit
**Save & run now**, and leads appear in the tracker below.

> Port 5050 is the default (5000 is often taken on Windows). Change it with
> `$env:JGS_PORT="8765"` before launching.

## Fetch providers

Two interchangeable backends, picked in the UI or via `"provider"` in
`config.json`:

- **`scrapingbee`** (recommended) — calls the [ScrapingBee Google Search
  API](https://www.scrapingbee.com/documentation/google/): managed proxies,
  clean structured JSON, never blocked, so it's the right choice for an
  unattended schedule. Needs an API key — set it in the UI's **Fetch provider**
  panel (saved to `config.json`, which is gitignored) or via the
  `SCRAPINGBEE_API_KEY` env var (takes precedence). Each query spends credits:
  queries per run = keyword sets × sources.
- **`playwright`** — drives a real headless Chromium locally (no API cost).
  Uses a persistent browser profile (`.browser-profile/`) and randomized
  delays. Free, but scraping `google.com/search` violates Google's ToS and a
  single home IP **will** hit CAPTCHAs on a schedule. If blocked, solve one
  challenge by hand:

  ```powershell
  python -m jobscrape.warmup
  ```

  A real Chromium window opens; solve the CAPTCHA, close it, and the saved
  session unblocks later headless runs.

## Configuring searches (the UI)

- **Keyword sets** — each set is a group of terms (tick *exact* to quote a
  term, e.g. `"Software Engineer"`; loose terms like `remote` stay unquoted).
  Add one set per role; *duplicate* copies a set so shared terms carry over.
- **Freshness** — Past week / 72 hours / 24 hours (Google's `tbs=qdr:w` /
  `qdr:d3` / `qdr:d`).
- **Sources** — 30+ presets. Hover any toggle to see the exact Google query it
  contributes. Add any bare domain with **+ Add domain**.
- **Run options** — max results per query; headless + delays (Playwright only).

The page live-previews the exact Google URLs it will hit
(`N queries per run = sets × sources`).

## The tracker (admin view)

Every lead is a row: ✓ applied checkbox (stamps date+time), ✕
not-a-real-link, title link, source, found/applied dates, status dropdown,
notes. Search box, status filter, hide-applied / hide-dismissed toggles,
sortable columns. **⬇ Export applied (Excel)** in the header downloads
`applied-YYYY-MM-DD.xlsx` of everything you've applied to, newest first.

## LLM title matching (local Ollama)

Google queries return plenty of near-miss results — product managers, sales
engineers, "Careers at X" portal pages. After each scrape, unclassified leads
are sent (in batches, titles only) to a local Ollama model, which judges
whether each title matches the roles you're hunting — defined as the **first
keyword of every search set** (e.g. *Software Engineer*, *Full Stack*,
*Frontend*). Individual-contributor variants match (junior/senior, SWE/SDE,
"Developer"); management-track (Engineering Manager, Director, VP) and
Staff/Principal-level positions don't. Non-matching jobs get a red `✗ match`
pill and are hidden behind the default-on **hide poor matches** toggle in
both the tracker and the assistant view. In the admin view, clicking the pill
overrides a false negative.

Setup: install [Ollama](https://ollama.com), pull a model
(`ollama pull qwen2.5:14b` works well), and make sure the **LLM title
matching** toggle in the Fetch provider panel points at it. To classify an
existing backlog in one go:

```powershell
python -m jobscrape.matcher
```

If Ollama is unreachable, a deliberately lenient string-rule fallback takes
over so leads still get triaged: it only filters out titles containing
staff/director/manager and keeps everything else. Transient errors (Ollama up
but a call failed) leave rows unclassified for retry on the next run. Disable
the feature entirely with the UI toggle or `"llm_match_enabled": false`.

## Assistant mode (delegate your applications)

The app has two faces and lightweight role-based auth:

| Route | Who | Sees |
|---|---|---|
| `/admin` | you | everything: scraper config, API key, tracker, runs |
| `/assistant` | your VA | job table + filters + notes + Excel export only |

- **Direct localhost access is auto-admin** — no login at your own machine.
- Any request arriving through a proxy/tunnel (detected via `X-Forwarded-For`)
  must log in: the **VA password** opens `/assistant` only; the **admin
  password** opens everything. Both live in `config.json`
  (`va_password` / `admin_password` — **change the defaults** before exposing
  the app).
- Applications marked by the VA are stamped `applied_by = "va"` (yours are
  `"admin"`). The admin header shows *"N applied (M by VA)"*, VA-applied rows
  get a `VA` pill, and the Excel export has an **Applied by** column — so you
  can pay a VA per application with receipts.

### Hosting it for your VA (ngrok)

The app binds to `127.0.0.1` only; expose it with a tunnel:

```powershell
ngrok http 5050
# or with a reserved (permanent) domain — free ngrok accounts get one:
ngrok http --domain=your-domain.ngrok-free.dev 5050
```

Send your VA `https://<your-tunnel>/assistant` + the VA password. Notes from
the field:

- Free-tier browser visits see ngrok's one-click interstitial page first;
  that's normal (paid plans remove it).
- Some home networks TLS-block `*.ngrok-free.dev` — if the link won't open
  from your own LAN, verify from another network (e.g. phone on cellular)
  before assuming it's down; it usually works fine externally.

### One-click launcher (Windows)

`start_job_scrape.bat` (double-click, or put a shortcut in your Start Menu)
kills any previous instances, then starts the Flask UI, a
[sqlite-web](https://github.com/coleifer/sqlite-web) DB browser on
http://127.0.0.1:8081 (`pip install sqlite-web`), and — if ngrok is installed —
the tunnel, printing and clipboard-copying the assistant link. Set
`ngrok_domain` (and optionally `ngrok_authtoken`) in `config.json` to use your
reserved domain.

## Scraping Simplify's job search

Besides Google, the scraper can pull leads straight from
[Simplify's job search](https://simplify.jobs/jobs) — no login needed. Their
search runs on a public Typesense cluster; `jobscrape/simplify_search.py`
pages through your filtered search, resolves each posting's
`simplify.jobs/jobs/click/<id>` redirect to the **real external application
URL** (Greenhouse/Lever/Ashby/...), strips the iframe + tracking params, and
upserts into the same jobs table — so dedupe, LLM matching, the tracker, and
autoapply all work on these leads unchanged.

Configure in `config.json`: `simplify_filter` is a Typesense `filter_by`
string copied verbatim from the website's own search requests (open DevTools
on simplify.jobs/jobs with your filters set and copy it from the
`multi_search` call); `__SINCE__` is replaced with the epoch for
`simplify_posted_within_days` ago. Runs automatically as part of every
scrape, or standalone:

```powershell
python -m jobscrape.simplify_search
```

## Fan-out applying with Simplify (autoapply)

`autoapply/` batch-triggers the [Simplify](https://simplify.jobs) extension's
autofill across many job tabs, so the forms fill themselves while you make
coffee. You still review and submit each application yourself.

**How it works** (`cdp_apply.py`): a dedicated Chrome profile runs with the
DevTools protocol enabled (Chrome blocks CDP on your daily profile, and
extension UI can't be triggered by page scripts — both by design). The driver
opens each job in a tab, focuses it via Simplify's own `chrome.windows` API
(Simplify only fills *visible* pages), reveals the application form if the
page hides it behind an Apply button, then dispatches Simplify's own
`trigger-autofill` command handler inside its service worker — byte-identical
to pressing Simplify's Alt+Shift+F shortcut — and verifies by reading the
form fields. No synthetic mouse or keyboard anywhere.

One-time setup: run `autoapply\launch_chrome.cmd`, install Simplify Copilot
in that window and log in. Then:

```powershell
# single-URL verbose test
.venv\Scripts\python autoapply\cdp_apply.py probe <url>
# next 10 new+matched leads from jobs.db (skips login-gated platforms)
.venv\Scripts\python autoapply\cdp_apply.py run --from-db 10
```

Each job takes ~70s: ~8s for Simplify to detect + score, then the tab stays focused for a 40s dwell after Start Application so the fill (resume upload, questions) finishes before the next tab steals focus. The automation window raises itself per job — watch if you like, but
don't type during a run. Tabs stay open; review, finish, submit, then tick ✓
in the tracker. `autoapply.ps1` (calibrated screen-clicking) remains as a
legacy fallback.

## Run the scraper headlessly

```powershell
python -m jobscrape.run
```

Prints a per-query log and a summary (provider / queries / found / new /
blocked). Exit code `2` means every query failed or was blocked.

### Schedule it (Windows Task Scheduler)

`run.bat` handles paths and logging (to `results\run.log`). Every 3 hours:

```powershell
schtasks /Create /SC HOURLY /MO 3 /TN "JobGoogleScrape" ^
  /TR "C:\path\to\Google-Job-Scraper-1\run.bat" /F
```

Manage it later with `schtasks /Run|/Query|/Delete /TN "JobGoogleScrape"`.
(On Linux/macOS, add a crontab line pointing at `python -m jobscrape.run`.)

## Layout

```
jobscrape/
  config.py        defaults, load/save config.json, freshness + source presets
  query.py         build Google search strings/URLs from the config
  scrapingbee.py   ScrapingBee Google Search API backend
  scraper.py       provider dispatch + Playwright backend, parsing, storage loop
  warmup.py        headed browser to solve a CAPTCHA once (Playwright provider)
  storage.py       SQLite store: dedupe-by-URL, tracker state, runs log
  matcher.py       LLM title matching via local Ollama (hide poor matches)
  export.py        Excel (.xlsx) export of applied positions
  run.py           CLI entry point (what the schedule runs)
  app.py           Flask app: /admin, /assistant, login, tracker + export APIs
  templates/       index.html (admin) · assistant.html (VA) · login.html
run.bat            scheduled-task entry point (logs to results/run.log)
ui.bat             launch the web UI
start_job_scrape.bat / .ps1   one-click launcher (UI + DB viewer + ngrok)
config.example.json
```

`config.json`, `results/` (your database), and `.browser-profile/` are
gitignored — your keys, leads, and cookies never leave your machine.

## Security notes (read before exposing publicly)

This is a personal tool with deliberately lightweight auth: two static shared
passwords, sessions in signed cookies, localhost auto-admin. That's fine for
"me + one VA behind an unguessable tunnel URL", but it is not hardened
multi-user software. Change both default passwords, prefer the ScrapingBee env
var over the config field, and don't point it at anything you can't afford to
leak.

## Notes / limits

- Google's result HTML changes periodically; if the Playwright provider parses
  0 links on a page that clearly has results, the selectors in `scraper.py`
  (`RESULT_JS`) may need a refresh.
- `tbs=qdr:d3` (72h) relies on Google's numeric date suffix; if Google drops
  it, fall back to `qdr:d` (24h) or `qdr:w` (week).
- Scraping Google search with the Playwright provider violates Google's ToS —
  use ScrapingBee for anything scheduled, and keep volumes reasonable.

## License

MIT — see [LICENSE](LICENSE).

